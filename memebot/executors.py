"""Executors: how a buy or sell actually happens in each mode.

SIM   paper fills at the DexScreener price (bot.py behaviour, same P/L math).
SHEET quotes Jupiter both ways, runs the full safety gate, and prints a trade sheet for Jawad to
      sign himself. Never holds a key, never opens a position.
LIVE  same quotes and gate, then signs Jupiter's transaction with the env-var key and executes.
      Every send is written to live_journal.jsonl first, so a crash mid-trade is reconciled
      against the wallet on the next start.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from .jupiter import SOL_MINT, Jupiter
from .net import FetchError
from .strategy import GateResult, pre_trade_gate

log = logging.getLogger(__name__)


@dataclass
class BuyResult:
    ok: bool
    fill_price: float = 0.0
    tokens_received: float = 0.0
    tokens_raw: int = 0
    fee_usd: float = 0.0
    usd_spent: float = 0.0
    tx_sig: str = ""
    error: str | None = None
    sheet: dict | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SellResult:
    ok: bool
    fill_price: float = 0.0
    usd_received: float = 0.0
    fee_usd: float = 0.0
    tx_sig: str = ""
    error: str | None = None
    tokens_left_raw: int = 0


@dataclass
class BuyContext:
    mint: str
    name: str
    live: dict           # DexScreener price info
    creator_history: str
    open_live_count: int
    day_pnl: float
    deployed_usd: float


class SimExecutor:
    mode = "SIM"

    def __init__(self, cfg):
        self.cfg = cfg

    async def buy(self, ctx: BuyContext) -> BuyResult:
        p = ctx.live["price"]
        return BuyResult(ok=True, fill_price=p, tokens_received=self.cfg["position_size_usd"] / p,
                         usd_spent=self.cfg["position_size_usd"])

    async def sell(self, pos: dict, ref_price: float, reason: str) -> SellResult:
        return SellResult(ok=True, fill_price=ref_price,
                          usd_received=self.cfg["position_size_usd"] * ref_price / pos["entry"])


class _QuotingExecutor:
    """Shared quote + gate logic for SHEET and LIVE."""

    def __init__(self, cfg, jup: Jupiter):
        self.cfg = cfg
        self.jup = jup
        self.quote_mint = cfg["live_quote_mint"]

    async def _quote_mint_usd(self) -> float | None:
        if self.quote_mint != SOL_MINT:
            return await self.jup.usd_price(self.quote_mint) or None
        return await self.jup.usd_price(SOL_MINT)

    async def plan_buy(self, ctx: BuyContext, *, taker: str | None, sol_balance: float | None):
        """Returns (GateResult, buy_order|None, details dict)."""
        cfg = self.cfg
        q_usd = await self._quote_mint_usd()
        q_dec = await self.jup.decimals(self.quote_mint)
        t_dec = await self.jup.decimals(ctx.mint)
        if not q_usd or q_dec is None or t_dec is None:
            return GateResult(False, "DO NOT TRADE: could not read SOL price or token decimals"), None, {}
        in_raw = int(cfg["position_size_usd"] / q_usd * 10 ** q_dec)
        try:
            buy = await self.jup.order(self.quote_mint, ctx.mint, in_raw, taker=taker,
                                       slippage_bps=cfg["slippage_bps"])
        except FetchError as e:
            return GateResult(False, f"DO NOT TRADE: no buy route ({e})"), None, {}
        try:
            sell = await self.jup.order(ctx.mint, self.quote_mint, buy.out_amount, slippage_bps=cfg["slippage_bps"])
            sell_ok = sell.out_amount > 0
        except FetchError as e:
            sell, sell_ok = None, False
            log.info("sell-route check failed for %s: %s", ctx.name, e)
        tokens = buy.out_amount / 10 ** t_dec
        jup_price = (buy.in_amount / 10 ** q_dec) * q_usd / tokens if tokens > 0 else 0.0
        rt_loss = 100.0 * (1 - sell.out_amount / buy.in_amount) if sell_ok else 100.0
        sol_after = None
        if sol_balance is not None and self.quote_mint == SOL_MINT:
            sol_after = sol_balance - buy.in_amount / 1e9
        gate = pre_trade_gate(
            expected_mint=ctx.mint, quote_mint=str(buy.raw.get("outputMint", ctx.mint)),
            ref_price=ctx.live["price"], jupiter_price=jup_price,
            price_impact_pct=max(buy.price_impact_pct, sell.price_impact_pct if sell else 0.0),
            open_live_count=ctx.open_live_count, day_pnl_usd=ctx.day_pnl,
            creator_history=ctx.creator_history, sell_route_verified=sell_ok,
            roundtrip_loss_pct=rt_loss, deployed_usd=ctx.deployed_usd, size_usd=cfg["position_size_usd"],
            sol_after_buy=sol_after, cfg=cfg)
        details = {
            "in_raw": buy.in_amount, "in_decimals": q_dec, "quote_usd": q_usd, "out_raw": buy.out_amount,
            "token_decimals": t_dec, "tokens": tokens, "jupiter_price": jup_price,
            "dex_price": ctx.live["price"], "buy_price_impact_pct": buy.price_impact_pct,
            "sell_price_impact_pct": sell.price_impact_pct if sell else None,
            "roundtrip_loss_pct": round(rt_loss, 3), "slippage_bps": cfg["slippage_bps"],
            "request_id": buy.request_id,
        }
        return gate, buy, details


class SheetExecutor(_QuotingExecutor):
    mode = "SHEET"

    async def buy(self, ctx: BuyContext) -> BuyResult:
        gate, _order, d = await self.plan_buy(ctx, taker=None, sol_balance=None)
        sheet = {
            "mint": ctx.mint, "name": ctx.name, "verdict": gate.reason,
            "spend_usd": self.cfg["position_size_usd"],
            "expected_tokens": round(d.get("tokens", 0), 6), "jupiter_price": d.get("jupiter_price"),
            "dex_price": ctx.live["price"], "price_impact_pct": d.get("buy_price_impact_pct"),
            "roundtrip_loss_pct": d.get("roundtrip_loss_pct"), "slippage_bps": self.cfg["slippage_bps"],
            "tp_price": (d.get("jupiter_price") or 0) * (1 + self.cfg["take_profit_pct"] / 100),
            "sl_price": (d.get("jupiter_price") or 0) * (1 - self.cfg["stop_loss_pct"] / 100),
            "jupiter_link": f"https://jup.ag/swap/SOL-{ctx.mint}",
            "creator_history": ctx.creator_history,
        }
        return BuyResult(ok=False, error="SHEET mode: printed only", sheet=sheet)

    async def sell(self, pos: dict, ref_price: float, reason: str) -> SellResult:  # pragma: no cover
        return SellResult(ok=False, error="SHEET mode never holds positions")


class LiveExecutor(_QuotingExecutor):
    mode = "LIVE"

    def __init__(self, cfg, jup: Jupiter, rpc, keypair, data):
        super().__init__(cfg, jup)
        self.rpc = rpc
        self.kp = keypair
        self.owner = str(keypair.pubkey())
        self.data = data

    async def _sign_and_execute(self, order, side: str, mint: str, name: str):
        from .wallet import sign_transaction_b64

        if not order.transaction:
            raise FetchError("jupiter order returned no transaction for our wallet")
        signed = sign_transaction_b64(order.transaction, self.kp)
        jid = uuid.uuid4().hex[:12]
        self.data.journal({"id": jid, "status": "sent", "side": side, "mint": mint, "name": name,
                           "request_id": order.request_id, "in_raw": order.in_amount,
                           "expected_out_raw": order.out_amount})
        try:
            res = await self.jup.execute(signed, order.request_id)
        except FetchError as e:
            # Unknown outcome: leave the journal entry 'sent' so restart reconciliation checks the wallet.
            log.error("execute %s %s: %s (outcome unknown, will reconcile)", side, name, e)
            raise
        self.data.journal({"id": jid, "status": "done" if res.ok else "failed", "side": side, "mint": mint,
                           "signature": res.signature, "in_raw": res.in_amount, "out_raw": res.out_amount,
                           "error": res.error})
        return res

    async def _fee_usd(self, sig: str, q_usd: float) -> float:
        lam = await self.rpc.tx_fee_lamports(sig) if sig else None
        return (lam or 0) / 1e9 * q_usd

    async def buy(self, ctx: BuyContext, *, approved_plan: dict | None = None) -> BuyResult:
        try:
            sol = await self.rpc.sol_balance(self.owner)
        except FetchError as e:
            return BuyResult(ok=False, error=f"could not read wallet balance: {e}")
        gate, order, d = await self.plan_buy(ctx, taker=self.owner, sol_balance=sol)
        if not gate.ok:
            return BuyResult(ok=False, error=gate.reason, extra=d)
        try:
            res = await self._sign_and_execute(order, "buy", ctx.mint, ctx.name)
        except Exception as e:
            return BuyResult(ok=False, error=f"buy failed: {e}", extra=d)
        if not res.ok:
            return BuyResult(ok=False, error=f"buy failed on-chain: {res.error}", tx_sig=res.signature, extra=d)
        q_dec, t_dec, q_usd = d["in_decimals"], d["token_decimals"], d["quote_usd"]
        spent_ui = (res.in_amount or order.in_amount) / 10 ** q_dec
        tokens_ui = res.out_amount / 10 ** t_dec
        usd_spent = spent_ui * q_usd
        fee = await self._fee_usd(res.signature, q_usd)
        return BuyResult(ok=True, fill_price=usd_spent / tokens_ui, tokens_received=tokens_ui,
                         tokens_raw=res.out_amount, fee_usd=fee, usd_spent=usd_spent, tx_sig=res.signature,
                         extra=d)

    async def sell(self, pos: dict, ref_price: float, reason: str) -> SellResult:
        try:
            raw, dec = await self.rpc.token_balance(self.owner, pos["address"])
        except FetchError as e:
            return SellResult(ok=False, error=f"could not read token balance: {e}")
        if raw <= 0:
            return SellResult(ok=False, error="wallet holds none of this token (sold elsewhere?)")
        q_usd = await self._quote_mint_usd()
        q_dec = await self.jup.decimals(self.quote_mint)
        if not q_usd or q_dec is None:
            return SellResult(ok=False, error="could not read SOL price")
        try:
            order = await self.jup.order(pos["address"], self.quote_mint, raw, taker=self.owner,
                                         slippage_bps=self.cfg["slippage_bps"])
            res = await self._sign_and_execute(order, "sell", pos["address"], pos.get("name", ""))
        except Exception as e:
            return SellResult(ok=False, error=f"sell failed: {e}")
        if not res.ok:
            return SellResult(ok=False, error=f"sell failed on-chain: {res.error}", tx_sig=res.signature)
        sold_ui = (res.in_amount or raw) / 10 ** dec
        usd = res.out_amount / 10 ** q_dec * q_usd
        fee = await self._fee_usd(res.signature, q_usd)
        left = max(0, raw - (res.in_amount or raw))
        return SellResult(ok=True, fill_price=usd / sold_ui if sold_ui else 0.0, usd_received=usd,
                          fee_usd=fee, tx_sig=res.signature, tokens_left_raw=left)
