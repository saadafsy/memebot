"""Read-only market data: DexScreener prices, pump.fun audit snapshot, RugCheck report.

Fixes vs bot.py:
  * a pair only counts if pair.baseToken.address == the mint we asked for. DexScreener returns
    every pair that contains the token, including pairs where it is the QUOTE token; their
    priceUsd is the other coin's price.
  * prices for all open positions are fetched in batches of up to 30 mints per request.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

import httpx

from .net import FetchError, request_json

log = logging.getLogger(__name__)

DEX_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
BATCH = 30


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def best_pairs(pairs: list[dict], mints: set[str]) -> dict[str, dict]:
    """Pick the highest-liquidity Solana pair per mint, where the mint is the BASE token."""
    out: dict[str, dict] = {}
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("chainId") != "solana":
            continue
        base = ((p.get("baseToken") or {}).get("address") or "").strip()
        if base not in mints:
            continue
        price = _num(p.get("priceUsd"))
        liq = _num((p.get("liquidity") or {}).get("usd")) or 0.0
        if price is None or price <= 0 or liq < 0:
            continue
        cand = {"price": price, "liq": liq, "pair": p.get("pairAddress", ""), "dex": p.get("dexId", ""),
                "symbol": (p.get("baseToken") or {}).get("symbol", "")}
        if base not in out or liq > out[base]["liq"]:
            out[base] = cand
    return out


class Market:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def prices(self, mints: list[str]) -> dict[str, dict]:
        """Live price info for many mints. Missing mints are simply absent from the result."""
        mints = [m for m in dict.fromkeys(m.strip() for m in mints) if m]
        out: dict[str, dict] = {}
        for i in range(0, len(mints), BATCH):
            chunk = mints[i:i + BATCH]
            try:
                data = await request_json(self.client, "GET", f"{DEX_BASE}/tokens/v1/solana/{','.join(chunk)}",
                                          label="dexscreener batch")
                pairs = data if isinstance(data, list) else (data.get("pairs") or [])
            except FetchError as e:
                log.warning("%s; trying the per-token endpoint", e)
                pairs = []
                for m in chunk:
                    try:
                        d = await request_json(self.client, "GET", f"{DEX_BASE}/latest/dex/tokens/{m}",
                                               label="dexscreener token")
                        pairs.extend(d.get("pairs") or [])
                    except FetchError as e2:
                        log.warning("%s", e2)
            out.update(best_pairs(pairs, set(chunk)))
        return out

    async def price(self, mint: str) -> dict | None:
        return (await self.prices([mint])).get(mint)

    async def pumpfun(self, mint: str) -> dict | None:
        """Audit-only pump.fun price, scraped from the coin page (same approach as bot.py)."""
        try:
            r = await self.client.get(f"https://pump.fun/coin/{mint}", headers={"Accept": "text/html"})
            html = r.text if r.status_code == 200 else ""
        except (httpx.HTTPError, OSError):
            return None
        if mint not in html:
            return None
        m = re.search(r'\\?"price_usd\\?":\s*([0-9.eE+-]+)', html)
        price = _num(m.group(1)) if m else None
        if price is None or price <= 0:
            return None
        mc = re.search(r'\\?"usd_market_cap\\?":\s*([0-9.eE+-]+)', html)
        mcap = _num(mc.group(1)) if mc else None
        return {"pf_price": price, "pf_mcap": round(mcap, 2) if mcap and mcap > 0 else "blind"}

    async def creator_history(self, mint: str) -> str:
        """RugCheck summary -> 'rugcheck_ok(score=N)' or 'rugcheck_danger:<risks>' or 'blind'."""
        try:
            d = await request_json(self.client, "GET", f"{RUGCHECK_BASE}/tokens/{mint}/report/summary",
                                   label="rugcheck", retries=2)
        except FetchError as e:
            log.info("%s", e)
            return "blind"
        return summarize_rugcheck(d)


def summarize_rugcheck(d: Any) -> str:
    if not isinstance(d, dict) or "risks" not in d:
        return "blind"
    risks = d.get("risks") or []
    danger = [str(r.get("name", "?")) for r in risks if isinstance(r, dict)
              and str(r.get("level", "")).lower() == "danger"]
    score = d.get("score_normalised", d.get("score"))
    if danger:
        return "rugcheck_danger:" + ";".join(danger)[:200]
    return f"rugcheck_ok(score={score})"
