"""Jawad's strategy rules, unchanged from bot.py, plus the LIVE pre-trade safety gate.

passes_filters() returns the same reason strings as bot.py.
pre_trade_gate() mirrors validate_pre_trade_safety() in his live_trader_interface.py and adds
the hard caps for real money (wallet budget, SOL reserve, round-trip loss).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


def _valid(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def passes_filters(cfg, row: dict, live: dict) -> str | None:
    try:
        holders = float(row.get("holders", ""))
        top10 = float(row.get("top10_pct", ""))
        if not (_valid(holders) and _valid(top10)):
            return "blind: holders/top10 not provided"
    except Exception:
        return "blind: holders/top10 not provided"
    if holders < cfg["min_holders"]:
        return f"holders below minimum ({holders:.0f} < {cfg['min_holders']})"
    if top10 >= cfg["max_top10_pct"]:
        return f"top-10 too high ({top10:.2f}% >= {cfg['max_top10_pct']}%)"
    if live["liq"] < cfg["min_liquidity_usd"]:
        return f"liquidity below minimum (${live['liq']:,.0f} < ${cfg['min_liquidity_usd']:,.0f})"
    try:
        sp = float(row.get("scout_price", ""))
        if not _valid(sp) or sp <= 0:
            return "blind: no Scout price to compare"
        diff = abs(live["price"] - sp) / sp * 100.0
        if diff > cfg["max_price_mismatch_pct"]:
            return f"live price differs from Scout snapshot ({diff:.2f}% > {cfg['max_price_mismatch_pct']}%)"
    except Exception:
        return "blind: no Scout price to compare"
    return None


_NUM = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def skip_key(addr: str, reason: str) -> str:
    """Dedupe key for skipped.csv: numbers are stripped so 'liquidity below minimum ($19,000 < ...)'
    and '($18,500 < ...)' count as the same skip for the day. bot.py kept the numbers, so the
    once-a-day filter never matched and skipped.csv grew every scan."""
    return f"{addr}|{_NUM.sub('#', reason)}"


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str


def pre_trade_gate(*, expected_mint: str, quote_mint: str, ref_price: float, jupiter_price: float,
                   price_impact_pct: float, open_live_count: int, day_pnl_usd: float, creator_history: str,
                   sell_route_verified: bool, roundtrip_loss_pct: float, deployed_usd: float,
                   size_usd: float, sol_after_buy: float | None, cfg) -> GateResult:
    """Fail-closed gate. Every check must pass before anything is signed (LIVE) or printed (SHEET)."""
    if expected_mint.strip() != quote_mint.strip():
        return GateResult(False, "DO NOT TRADE: contract address mismatch between watchlist and Jupiter")
    if not creator_history.startswith("rugcheck_ok"):
        return GateResult(False, f"DO NOT TRADE: creator_history is {creator_history.split(':')[0]} (blocked in LIVE)")
    if not sell_route_verified:
        return GateResult(False, "DO NOT TRADE: sell route / round-trip quote not verified")
    if open_live_count >= cfg["live_max_open"]:
        return GateResult(False, f"DO NOT TRADE: max {cfg['live_max_open']} open live positions reached")
    if day_pnl_usd <= -cfg["daily_loss_cap_usd"]:
        return GateResult(False, f"DO NOT TRADE: daily loss cap hit (${day_pnl_usd:.2f})")
    if not (_valid(ref_price) and _valid(jupiter_price)) or ref_price <= 0 or jupiter_price <= 0:
        return GateResult(False, "DO NOT TRADE: invalid live price")
    diff = abs(jupiter_price - ref_price) / ref_price * 100.0
    if diff > cfg["max_price_mismatch_pct"]:
        return GateResult(False, f"DO NOT TRADE: price mismatch {diff:.2f}% > {cfg['max_price_mismatch_pct']}%")
    if price_impact_pct > cfg["live_max_price_impact_pct"]:
        return GateResult(False, f"DO NOT TRADE: price impact {price_impact_pct:.2f}% > {cfg['live_max_price_impact_pct']}%")
    if roundtrip_loss_pct > cfg["live_max_roundtrip_loss_pct"]:
        return GateResult(False, f"DO NOT TRADE: round-trip quote loses {roundtrip_loss_pct:.2f}% "
                                 f"> {cfg['live_max_roundtrip_loss_pct']}%")
    if deployed_usd + size_usd > cfg["live_wallet_budget_usd"]:
        return GateResult(False, f"DO NOT TRADE: wallet budget ${cfg['live_wallet_budget_usd']:.2f} would be exceeded "
                                 f"(${deployed_usd:.2f} already in positions)")
    if sol_after_buy is not None and sol_after_buy < cfg["live_min_sol_reserve"]:
        return GateResult(False, f"DO NOT TRADE: would leave {sol_after_buy:.4f} SOL, below the "
                                 f"{cfg['live_min_sol_reserve']} SOL reserve for fees")
    return GateResult(True, "PRE-TRADE SAFETY PASS")
