"""Jupiter Swap V2 client: GET /order (quote, and an unsigned transaction when `taker` is set),
then POST /execute with the signed transaction. Plus token decimals and SOL/USD price.

Field names follow Jupiter's order/execute responses; parsing is defensive (several known
spellings are accepted) and anything unexpected fails closed: no trade.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .net import FetchError, request_json

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
LITE = "https://lite-api.jup.ag"


@dataclass
class Order:
    request_id: str
    in_amount: int
    out_amount: int
    price_impact_pct: float  # positive number, in percent (1.5 == 1.5%)
    transaction: str | None  # base64, only when taker was given
    slippage_bps: int | None
    raw: dict

    @property
    def fee_bps(self) -> int:
        return _int(self.raw.get("feeBps")) or 0

    @property
    def network_fee_lamports(self) -> int:
        """Signature + priority + rent fees Jupiter quoted for this swap (0 when no taker was given)."""
        return sum(_int(self.raw.get(k)) or 0 for k in ("signatureFeeLamports", "prioritizationFeeLamports",
                                                         "rentFeeLamports"))


def _int(x: Any) -> int | None:
    try:
        v = int(str(x))
    except (TypeError, ValueError):
        return None
    return v


def price_impact_percent(d: dict) -> float | None:
    """Jupiter has used both 'priceImpact' (percent) and 'priceImpactPct' (fraction, as a string)."""
    if d.get("priceImpact") is not None:
        try:
            v = abs(float(d["priceImpact"]))
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    if d.get("priceImpactPct") is not None:
        try:
            v = abs(float(d["priceImpactPct"])) * 100.0
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    return None


def parse_order(d: Any, taker_set: bool = False) -> Order:
    if not isinstance(d, dict):
        raise FetchError("jupiter order: unexpected response")
    if taker_set and not d.get("transaction"):
        raise FetchError(f"jupiter order: no transaction for this wallet "
                         f"({d.get('errorCode')}: {d.get('errorMessage') or d.get('error') or 'not built'})")
    if d.get("errorCode") or d.get("errorMessage") or d.get("error"):
        raise FetchError(f"jupiter order: {d.get('errorMessage') or d.get('error') or d.get('errorCode')}")
    rid = d.get("requestId") or ""
    ina = _int(d.get("inAmount"))
    outa = _int(d.get("outAmount"))
    if not ina or not outa or ina <= 0 or outa <= 0:
        raise FetchError("jupiter order: no route (missing in/out amount)")
    pi = price_impact_percent(d)
    if pi is None:
        raise FetchError("jupiter order: price impact missing; refusing to trade blind")
    return Order(request_id=str(rid), in_amount=ina, out_amount=outa, price_impact_pct=pi,
                 transaction=d.get("transaction") or None, slippage_bps=_int(d.get("slippageBps")), raw=d)


@dataclass
class ExecResult:
    ok: bool
    signature: str
    in_amount: int | None
    out_amount: int | None
    error: str | None
    raw: dict


def parse_execute(d: Any) -> ExecResult:
    if not isinstance(d, dict):
        return ExecResult(False, "", None, None, "unexpected execute response", {})
    status = str(d.get("status", "")).lower()
    sig = str(d.get("signature") or "")
    ina = _int(d.get("inputAmountResult", d.get("totalInputAmount")))
    outa = _int(d.get("outputAmountResult", d.get("totalOutputAmount")))
    code = d.get("code", 0)
    ok = status == "success" and bool(sig) and bool(outa) and code in (0, "0", None)
    err = None if ok else f"{d.get('error') or 'failed'} (status={d.get('status')}, code={code})"
    return ExecResult(ok, sig, ina, outa, err, d)


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, api_key: str = "", base: str = "https://api.jup.ag/swap/v2"):
        self.client = client
        self.api_key = api_key
        self.base = base.rstrip("/")
        self._decimals: dict[str, int] = {SOL_MINT: 9}
        self._last_usd: dict[str, float] = {}
        self._usd_at: dict[str, float] = {}
        self._gate = asyncio.Lock()
        self._next_ok = 0.0
        # Keyless Jupiter allows about 0.5 requests per second; pace calls instead of collecting 429s.
        self.min_interval = 0.0 if api_key else 2.1

    async def _pace(self) -> None:
        if not self.min_interval:
            return
        async with self._gate:
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok = time.monotonic() + self.min_interval

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def order(self, input_mint: str, output_mint: str, amount: int, *, taker: str | None = None,
                    slippage_bps: int | None = None) -> Order:
        if amount <= 0:
            raise FetchError("jupiter order: amount must be positive")
        params: dict[str, Any] = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount))}
        if taker:
            params["taker"] = taker
        if slippage_bps:
            params["slippageBps"] = str(int(slippage_bps))
        await self._pace()
        d = await request_json(self.client, "GET", f"{self.base}/order", params=params,
                               headers=self._headers(), label="jupiter order", retries=2)
        return parse_order(d, taker_set=bool(taker))

    async def execute(self, signed_tx_b64: str, request_id: str, last_valid_block_height: Any = None) -> ExecResult:
        # Never retry /execute blindly at the HTTP layer: a timeout may still have landed on-chain.
        body = {"signedTransaction": signed_tx_b64, "requestId": request_id}
        if last_valid_block_height not in (None, ""):
            body["lastValidBlockHeight"] = str(last_valid_block_height)
        d = await request_json(self.client, "POST", f"{self.base}/execute", json_body=body,
                               headers=self._headers(), label="jupiter execute", retries=0)
        return parse_execute(d)

    async def _get(self, path: str, params: dict, label: str) -> Any:
        last: FetchError | None = None
        for base in ("https://api.jup.ag", LITE):  # lite-api is being phased out; keep it as a fallback
            await self._pace()
            try:
                return await request_json(self.client, "GET", f"{base}{path}", params=params,
                                          headers=self._headers() if base != LITE else {}, label=label, retries=2)
            except FetchError as e:
                last = e
        raise last or FetchError(label)

    async def _price_entry(self, mint: str) -> dict | None:
        try:
            d = await self._get("/price/v3", {"ids": mint}, "jupiter price")
        except FetchError as e:
            log.info("%s", e)
            return None
        e = d.get(mint) if isinstance(d, dict) else None
        return e if isinstance(e, dict) else None

    async def decimals(self, mint: str) -> int | None:
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            d = await self._get("/tokens/v2/search", {"query": mint}, "jupiter tokens")
        except FetchError as e:
            log.info("%s", e)
            d = []
        for t in d if isinstance(d, list) else []:
            if isinstance(t, dict) and (t.get("id") or t.get("address")) == mint:
                dec = _int(t.get("decimals"))
                if dec is not None and 0 <= dec <= 18:
                    self._decimals[mint] = dec
                    return dec
        pe = await self._price_entry(mint)  # the price API also carries decimals
        dec = _int((pe or {}).get("decimals"))
        if dec is not None and 0 <= dec <= 18:
            self._decimals[mint] = dec
            return dec
        return None

    async def usd_price(self, mint: str, max_age: float = 30.0) -> float | None:
        if mint in self._last_usd and time.monotonic() - self._usd_at.get(mint, 0) < max_age:
            return self._last_usd[mint]  # SOL's price barely moves within 30 s; saves a call per quote
        pe = await self._price_entry(mint)
        try:
            v = float((pe or {}).get("usdPrice"))
        except (TypeError, ValueError):
            return self._last_usd.get(mint)  # last good price (or None) beats blocking an exit
        if math.isfinite(v) and v > 0:
            self._last_usd[mint] = v
            self._usd_at[mint] = time.monotonic()
            return v
        return self._last_usd.get(mint)
