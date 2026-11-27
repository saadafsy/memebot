"""live_trader.py: Jawad's live-executor interface (handoff/INTERFACE_CONTRACT.md), built on the same
Jupiter Swap V2 executor the bot uses (memebot/executors.py: LiveExecutor).

    import live_trader
    t = live_trader.LiveTrader.from_env(data_dir="data")      # fails closed unless LIVE is armed
    r = t.buy(mint, position_size_usd=20, ref_price=0.004764, max_price_mismatch_pct=3)
    # {"ok": bool, "fill_price": float, "tokens_received": float, "fee_usd": float, "tx_sig": str, "error": str|None}
    s = t.sell(mint, tokens_received=r["tokens_received"], ref_price=0.0061, reason="take_profit")
    # {"ok": bool, "fill_price": float, "usd_received": float, "fee_usd": float, "tx_sig": str, "error": str|None}

Every buy runs the full fail-closed safety gate first: RugCheck creator history must be clean (never
"blind"), a sell route must exist, round-trip quote loss, price impact, Jupiter vs reference price
mismatch, the max open live positions, daily loss cap, wallet budget and SOL reserve. Every send is
written to <data>/live/live_journal.jsonl before it goes out. Keys come only from environment variables.

The normal way to trade is still `python -m memebot run`, which adds the Telegram Approve tap, the
paper shadow, exit management and crash recovery around these same calls. Use this module directly
only for a hand-run tiny test.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memebot.config import Env, Settings, live_gate_problems, load_env, load_settings
from memebot.executors import BuyContext, LiveExecutor
from memebot.jupiter import Jupiter
from memebot.market import Market
from memebot.net import make_client
from memebot.storage import DataDir

__all__ = ["LiveTrader", "NotArmed"]


class NotArmed(RuntimeError):
    pass


class LiveTrader:
    def __init__(self, cfg: Settings, executor: LiveExecutor, market: Market, *, open_live_count=lambda: 0,
                 day_pnl=lambda: 0.0, deployed_usd=lambda: 0.0):
        self.cfg, self.ex, self.market = cfg, executor, market
        self._open, self._day, self._deployed = open_live_count, day_pnl, deployed_usd

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, data_dir: str | Path = "data", env: Env | None = None) -> "LiveTrader":
        from memebot.wallet import Rpc, load_keypair

        env = env or load_env()
        data = DataDir(data_dir)
        cfg = load_settings(data.root / "settings.json")
        probs = live_gate_problems(cfg, env, (Path.cwd() / ".env", data.root / ".env"))
        if probs:
            raise NotArmed("live_trader is not armed:\n  - " + "\n  - ".join(probs))
        client = make_client()
        live_dir = DataDir(data.root / "live", inputs_from=data.root)
        state = live_dir.load_state()
        ex = LiveExecutor(cfg, Jupiter(client, env.jupiter_api_key, env.jupiter_base), Rpc(client, env.rpc_url),
                          load_keypair(env.private_key), live_dir)
        return cls(cfg, ex, Market(client), open_live_count=lambda: len(state["open"]),
                   day_pnl=lambda: float(state.get("day_pnl", 0.0)),
                   deployed_usd=lambda: sum(float(p.get("usd_spent") or 0) for p in state["open"]))

    # ------------------------------------------------------------------ async API
    async def abuy(self, mint: str, position_size_usd: float | None = None, ref_price: float | None = None,
                   max_price_mismatch_pct: float | None = None, name: str = "") -> dict[str, Any]:
        if position_size_usd is not None and position_size_usd != self.cfg["position_size_usd"]:
            return _buy_dict(False, error="position_size_usd must match settings.json (it sets the budget checks)")
        if max_price_mismatch_pct is not None and max_price_mismatch_pct > self.cfg["max_price_mismatch_pct"]:
            return _buy_dict(False, error="max_price_mismatch_pct cannot be looser than settings.json")
        live = await self.market.price(mint)
        if not live:
            return _buy_dict(False, error="no DexScreener price for this mint")
        if ref_price:
            live = dict(live, price=float(ref_price))
        creator = await self.market.creator_history(mint)
        if not creator.startswith("rugcheck_ok"):
            return _buy_dict(False, error=f"DO NOT TRADE: creator_history is {creator.split(':')[0]} (blocked in LIVE)")
        ctx = BuyContext(mint=mint, name=name or mint[:8], live=live, creator_history=creator,
                         open_live_count=self._open(), day_pnl=self._day(), deployed_usd=self._deployed())
        r = await self.ex.buy(ctx)
        return _buy_dict(r.ok, r.fill_price, r.tokens_received, r.fee_usd, r.tx_sig, r.error)

    async def asell(self, mint: str, tokens_received: float | None = None, ref_price: float = 0.0,
                    reason: str = "manual", tokens_raw: int | None = None) -> dict[str, Any]:
        pos = {"address": mint, "name": mint[:8], "tokens_received": tokens_received or 0,
               "tokens_raw": tokens_raw or 0, "entry": ref_price or 1.0}
        r = await self.ex.sell(pos, ref_price, reason)
        return {"ok": r.ok, "fill_price": r.fill_price, "usd_received": r.usd_received, "fee_usd": r.fee_usd,
                "tx_sig": r.tx_sig, "error": r.error if not r.note else (r.error or r.note)}

    # ------------------------------------------------------------------ sync API (as in the contract)
    def buy(self, *a, **k) -> dict[str, Any]:
        return asyncio.run(self.abuy(*a, **k))

    def sell(self, *a, **k) -> dict[str, Any]:
        return asyncio.run(self.asell(*a, **k))


def _buy_dict(ok: bool, fill_price: float = 0.0, tokens_received: float = 0.0, fee_usd: float = 0.0,
              tx_sig: str = "", error: str | None = None) -> dict[str, Any]:
    return {"ok": ok, "fill_price": fill_price, "tokens_received": tokens_received, "fee_usd": fee_usd,
            "tx_sig": tx_sig, "error": error}
