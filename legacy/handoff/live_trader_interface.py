"""
Plug-in contract for Jawad's future local live_trader.py (to be built in Claude Code
ONLY AFTER 100 closed paper trades and Jawad's explicit written decision).

STRICT SECURITY INVARIANT:
- Zero wallet, private key, seed phrase, or signing code lives here.
- Jawad keeps full custody of the $60 CAD Jupiter wallet and signs every live trade.
"""
from dataclasses import dataclass
from typing import Literal, Optional, Protocol, TypedDict


class PositionDict(TypedDict):
    address: str
    name: str
    entry: float
    tp: float
    sl: float
    opened: str
    deadline: str
    liq_at_entry: float
    dex_pair: str
    holders: float
    top10_pct: float
    scout_price: float
    pf_price: object
    pf_mcap: object
    pf_vs_dex_pct: object
    auditor_verdict: str
    auditor_verdict_time: str
    creator_history: str
    high_since_entry: float
    low_since_entry: float


@dataclass(frozen=True)
class LiveSafetyConfig:
    wallet_cap_cad: float = 60.0
    max_open_live: int = 3
    daily_loss_cap_usd: float = 15.0
    max_quote_mismatch_pct: float = 3.0
    max_price_impact_pct: float = 3.0


@dataclass(frozen=True)
class SafetyCheckResult:
    ok: bool
    reason: str


def validate_pre_trade_safety(
    *,
    expected_mint: str,
    quote_mint: str,
    pumpfun_price: float,
    jupiter_price: float,
    price_impact_pct: float,
    open_live_count: int,
    day_pnl_usd: float,
    creator_history: str,
    sell_route_verified: bool,
    cfg: LiveSafetyConfig = LiveSafetyConfig(),
) -> SafetyCheckResult:
    """Pure fail-closed safety gate before generating a live Jupiter trade sheet."""
    if expected_mint.strip() != quote_mint.strip():
        return SafetyCheckResult(False, "DO NOT TRADE: contract address mismatch between pump.fun and Jupiter")
    if creator_history == "blind":
        return SafetyCheckResult(False, "DO NOT TRADE: creator_history is blind (blocked in LIVE mode)")
    if not sell_route_verified:
        return SafetyCheckResult(False, "DO NOT TRADE: sell route / round-trip quote not verified")
    if open_live_count >= cfg.max_open_live:
        return SafetyCheckResult(False, f"DO NOT TRADE: max {cfg.max_open_live} open live positions reached")
    if day_pnl_usd <= -cfg.daily_loss_cap_usd:
        return SafetyCheckResult(False, f"DO NOT TRADE: daily loss cap hit (${day_pnl_usd:.2f})")
    if pumpfun_price <= 0 or jupiter_price <= 0:
        return SafetyCheckResult(False, "DO NOT TRADE: invalid live price")
    diff_pct = abs(jupiter_price - pumpfun_price) / pumpfun_price * 100.0
    if diff_pct > cfg.max_quote_mismatch_pct:
        return SafetyCheckResult(False, f"DO NOT TRADE: price mismatch {diff_pct:.2f}% > {cfg.max_quote_mismatch_pct}%")
    if price_impact_pct > cfg.max_price_impact_pct:
        return SafetyCheckResult(False, f"DO NOT TRADE: price impact {price_impact_pct:.2f}% > {cfg.max_price_impact_pct}%")
    return SafetyCheckResult(True, "PRE-TRADE SAFETY PASS (awaiting user signature in Jupiter)")


class LiveExecutorProtocol(Protocol):
    """Interface that Jawad's local live_trader.py will implement in Claude Code."""

    def open_position(
        self,
        state: dict,
        row: dict,
        live: dict,
        pf: Optional[dict] = None,
        verdict_info: Optional[dict] = None,
    ) -> Optional[PositionDict]:
        ...

    def close_position(
        self,
        state: dict,
        pos: PositionDict,
        price: float,
        reason: Literal["take_profit", "stop_loss", "time_exit", "manual"],
    ) -> None:
        ...
