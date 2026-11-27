"""Jupiter Swap V2 client: GET /order (quote, and an unsigned transaction when `taker` is set),
then POST /execute with the signed transaction. Plus token decimals and SOL/USD price.

Field names follow Jupiter's order/execute responses; parsing is defensive (several known
spellings are accepted) and anything unexpected fails closed: no trade.
"""
from __future__ import annotations

import logging
import math
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


def parse_order(d: Any) -> Order:
    if not isinstance(d, dict):
        raise FetchError("jupiter order: unexpected response")
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
    ok = status == "success" and bool(sig) and bool(outa)
    err = None if ok else str(d.get("error") or d.get("code") or f"status={d.get('status')}")
    return ExecResult(ok, sig, ina, outa, err, d)


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, api_key: str = "", base: str = "https://api.jup.ag/swap/v2"):
        self.client = client
        self.api_key = api_key
        self.base = base.rstrip("/")
        self._decimals: dict[str, int] = {SOL_MINT: 9}
        self._last_usd: dict[str, float] = {}

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
        d = await request_json(self.client, "GET", f"{self.base}/order", params=params,
                               headers=self._headers(), label="jupiter order", retries=2)
        return parse_order(d)

    async def execute(self, signed_tx_b64: str, request_id: str) -> ExecResult:
        # Never retry /execute blindly at the HTTP layer: a timeout may still have landed on-chain.
        d = await request_json(self.client, "POST", f"{self.base}/execute",
                               json_body={"signedTransaction": signed_tx_b64, "requestId": request_id},
                               headers=self._headers(), label="jupiter execute", retries=0)
        return parse_execute(d)

    async def decimals(self, mint: str) -> int | None:
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            d = await request_json(self.client, "GET", f"{LITE}/tokens/v2/search", params={"query": mint},
                                   label="jupiter tokens", retries=2)
        except FetchError as e:
            log.info("%s", e)
            return None
        for t in d if isinstance(d, list) else []:
            if isinstance(t, dict) and (t.get("id") or t.get("address")) == mint:
                dec = _int(t.get("decimals"))
                if dec is not None and 0 <= dec <= 18:
                    self._decimals[mint] = dec
                    return dec
        return None

    async def usd_price(self, mint: str) -> float | None:
        base = "https://api.jup.ag" if self.api_key else LITE
        try:
            d = await request_json(self.client, "GET", f"{base}/price/v3", params={"ids": mint},
                                   headers=self._headers(), label="jupiter price", retries=2)
        except FetchError as e:
            log.info("%s", e)
            return self._last_usd.get(mint)  # last good price (or None) beats blocking an exit
        try:
            v = float((d.get(mint) or {}).get("usdPrice"))
        except (TypeError, ValueError, AttributeError):
            return self._last_usd.get(mint)
        if math.isfinite(v) and v > 0:
            self._last_usd[mint] = v
            return v
        return self._last_usd.get(mint)
