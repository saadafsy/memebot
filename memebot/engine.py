"""Trading engine. One asyncio task owns all state; Telegram and the panel only call the small
control methods below (start/stop entries, pause, force-sell, approve/reject), never the files.

Loop: every exit_poll_seconds -> manage open positions; every poll_seconds -> scan for entries.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from . import timeutil
from .executors import UNKNOWN, BuyContext, BuyResult, SellResult
from .storage import LIVE_TRADE_FIELDS, SKIP_FIELDS, TRADE_FIELDS, DataDir
from .strategy import passes_filters, skip_key

log = logging.getLogger(__name__)


def _valid(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass
class Approval:
    id: str
    mint: str
    name: str
    row: dict
    verdict: dict
    created: Any
    status: str = "pending"  # pending | approved | rejected | expired | done
    note: str = ""


@dataclass
class Health:
    last_scan: str = ""
    last_manage: str = ""
    last_error: str = ""
    errors_in_row: int = 0
    started: str = field(default_factory=lambda: timeutil.iso(timeutil.now()))


class Engine:
    def __init__(self, cfg, data: DataDir, market, executor, notifier, label: str = ""):
        self.cfg = cfg
        self.data = data
        self.market = market
        self.ex = executor
        self.notify = notifier
        self.mode = executor.mode
        self.label = label or {"SIM": "PAPER", "LIVE": "REAL", "SHEET": "SHEET"}[self.mode]
        self.state = data.load_state()
        ctl = self.state.setdefault("control", {})
        # A fresh LIVE bot starts with entries OFF: press Start deliberately.
        ctl.setdefault("entries_on", self.mode != "LIVE")
        ctl.setdefault("paused", False)
        self.state.setdefault("sheets_printed", {})
        self.force_sell: set[str] = set()
        self.approvals: dict[str, Approval] = {}
        self.pending_extreme: dict[str, float] = {}
        self.health = Health()
        self.entry_guard = None  # callable -> str | None; a message blocks turning entries on
        self._next_scan = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ controls (thread: event loop)
    @property
    def entries_on(self) -> bool:
        return bool(self.state["control"]["entries_on"])

    @property
    def paused(self) -> bool:
        return bool(self.state["control"]["paused"])

    def set_entries(self, on: bool, who: str) -> str:
        if on and self.entry_guard is not None:
            blocked = self.entry_guard()
            if blocked:
                self.notify.log_line("CONTROL", f"START refused for {who}: {blocked}")
                return blocked
        self.state["control"]["entries_on"] = bool(on)
        if on:
            self.state["control"]["paused"] = False
        self.data.save_state(self.state)
        msg = "Entries ON: the bot may open new positions." if on else \
            "Entries OFF: no new positions. Open positions are still managed (TP/SL/time exit)."
        self.notify.alert("CONTROL", f"{'START' if on else 'STOP'} by {who}", {"state": msg})
        return msg

    def set_paused(self, paused: bool, who: str) -> str:
        self.state["control"]["paused"] = bool(paused)
        if paused:
            self.state["control"]["entries_on"] = False
        self.data.save_state(self.state)
        msg = ("PAUSED EVERYTHING: no entries and no automatic exits. Force-sell still works."
               if paused else "Resumed exit management. Entries stay OFF until you press Start.")
        self.notify.alert("CONTROL", f"{'PAUSE' if paused else 'RESUME'} by {who}", {"state": msg}, loud=paused)
        return msg

    def request_force_sell(self, key: str, who: str) -> str:
        key = key.strip()
        for p in self.state["open"]:
            if key and (p["address"] == key or p.get("name", "").lower() == key.lower()):
                self.force_sell.add(p["address"])
                self.notify.alert("CONTROL", f"FORCE SELL {p.get('name')} requested by {who}")
                return f"Force-sell queued for {p.get('name')}; it runs on the next exit check."
        return "No open position matches that name or address."

    def request_sell_all(self, who: str) -> str:
        for p in self.state["open"]:
            self.force_sell.add(p["address"])
        self.notify.alert("CONTROL", f"SELL ALL requested by {who}", {"positions": len(self.state['open'])})
        return f"Force-sell queued for {len(self.state['open'])} position(s)."

    def decide_approval(self, aid: str, approve: bool, who: str) -> str:
        a = self.approvals.get(aid)
        if not a or a.status != "pending":
            return "That request is no longer pending."
        if (timeutil.now() - a.created).total_seconds() > self.cfg["live_approval_timeout_seconds"]:
            a.status = "expired"
            return "Too late: the approval window expired. Nothing was bought."
        a.status = "approved" if approve else "rejected"
        self.notify.log_line("APPROVAL", f"{a.status.upper()} {a.name} by {who}")
        return (f"Approved {a.name}. Re-quoting and re-checking every safety rule before buying."
                if approve else f"Rejected {a.name}. Nothing bought.")

    # ------------------------------------------------------------------ main loop
    async def run(self, stop: asyncio.Event) -> None:
        if self.mode == "LIVE":
            await self.reconcile()
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            await self.tick(loop.time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg["exit_poll_seconds"])
            except asyncio.TimeoutError:
                pass
        self.data.save_state(self.state)

    async def tick(self, mono_now: float | None = None, force_scan: bool = False) -> None:
        async with self._lock:
            try:
                self.rollover_day()
                self.flush_trade_rows()
                if self.mode == "LIVE":
                    await self.reconcile_pending()
                await self.manage()
                await self.process_approvals()
                if force_scan or mono_now is None or mono_now >= self._next_scan:
                    await self.scan()
                    if mono_now is not None:
                        self._next_scan = mono_now + self.cfg["poll_seconds"]
                self.health.errors_in_row = 0
            except Exception as e:  # the loop must survive anything
                self.health.errors_in_row += 1
                self.health.last_error = f"{timeutil.iso(timeutil.now())} {type(e).__name__}: {e}"
                log.exception("tick failed")
                if self.health.errors_in_row in (3, 20, 100):
                    self.notify.alert("ERROR", f"{self.health.errors_in_row} failed ticks in a row",
                                      {"last": self.health.last_error[:300]}, loud=True)
            finally:
                try:
                    self.data.save_state(self.state)
                except OSError as e:
                    log.error("could not save state: %s", e)

    # ------------------------------------------------------------------ day rollover
    def rollover_day(self) -> None:
        s = self.state
        today = timeutil.today()
        if s.get("day") == today:
            return
        if s.get("day"):
            st = self.stats()
            self.notify.alert("DAILY_SUMMARY", f"Day {s.get('day')} Summary (all net of fees)", {
                "realized_net": f"${st['realized_pnl']:+.2f}", "today_net": f"${st['today_pnl']:+.2f}",
                "closed": f"{st['closed']}/{self.cfg['target_closed_trades']}",
                "W/L": f"{st['wins']}/{st['losses']}", "win_rate": f"{st['win_rate_pct']}%",
                "open": len(s["open"])})
        s["day"] = today
        s["day_pnl"] = 0.0
        s["skipped_today"] = []
        s["loss_cap_alerted_day"] = ""

    # ------------------------------------------------------------------ exits
    def _accept_price(self, pos: dict, price: float) -> bool:
        """A one-tick move bigger than extreme_move_pct (a possible bad print) is acted on only after a
        second consecutive read confirms the same direction. A real crash keeps falling, so it is
        confirmed on the next tick and the stop-loss fires; a glitch reverts and is ignored."""
        prev = pos.get("last_price") or pos["entry"]
        addr = pos["address"]
        move = abs(price / prev - 1) * 100 if prev > 0 else 0
        if move <= self.cfg["extreme_move_pct"]:
            self.pending_extreme.pop(addr, None)
            return True
        first = self.pending_extreme.get(addr)
        if first is not None and (price - prev) * (first - prev) > 0:
            self.pending_extreme.pop(addr, None)
            return True
        self.pending_extreme[addr] = price
        log.info("%s: %.0f%% move in one tick, waiting for a second read", pos.get("name"), move)
        return False

    async def manage(self) -> None:
        s = self.state
        if not s["open"]:
            return
        forced = set(self.force_sell)
        if self.paused and not forced:
            return
        prices = await self.market.prices([p["address"] for p in s["open"]])
        now = timeutil.now()
        self.health.last_manage = timeutil.iso(now)
        for pos in list(s["open"]):
            addr = pos["address"]
            if self.paused and addr not in forced:
                continue
            nxt = pos.get("next_sell_try_at")
            if nxt and timeutil.parse(nxt) > now and addr not in forced:
                continue
            live = prices.get(addr)
            price = None
            if live and self._accept_price(pos, live["price"]):
                price = live["price"]
                pos["last_price"] = price
                pos["last_price_at"] = timeutil.iso(now)
                pos["high_since_entry"] = max(pos.get("high_since_entry", price), price)
                pos["low_since_entry"] = min(pos.get("low_since_entry", price), price)
            reason = None
            try:
                deadline = timeutil.parse(pos["deadline"])
            except (KeyError, ValueError):
                deadline = now  # malformed deadline: fail safe, exit
            if addr in forced:
                reason = "manual"
            elif price is not None and price >= pos["tp"]:
                reason = "take_profit"
            elif price is not None and price <= pos["sl"]:
                reason = "stop_loss"
            elif now >= deadline:
                reason = "time_exit"
            if not reason:
                continue
            ref = price if price is not None else pos.get("last_price")
            if ref is None:
                # No price ever seen. LIVE can still sell (the swap gives the fill); SIM waits for the
                # grace period, then exits at entry so the position never gets stuck (bot.py bug 2).
                grace = timedelta(minutes=self.cfg["time_exit_grace_minutes"])
                if self.mode != "LIVE" and reason == "time_exit" and now < deadline + grace:
                    continue
                ref = pos["entry"]
                if self.mode != "LIVE":
                    self.notify.alert("WARN", f"{pos.get('name')}: no price available, time exit at entry price")
            await self._close(pos, ref, reason)

    async def _close(self, pos: dict, ref_price: float, reason: str) -> None:
        addr = pos["address"]
        res: SellResult = await self.ex.sell(pos, ref_price, reason)
        if not res.ok:
            n = int(pos.get("sell_failures", 0)) + 1
            pos["sell_failures"] = n
            delay = min(60, self.cfg["exit_poll_seconds"] * n)  # exits retry fast: 15 s, 30 s, 45 s, 60 s...
            pos["next_sell_try_at"] = timeutil.iso(timeutil.now() + timedelta(seconds=delay))
            log.warning("sell %s failed (%d): %s", pos.get("name"), n, res.error)
            if n == self.cfg["live_sell_max_failures"] or (n > self.cfg["live_sell_max_failures"] and n % 20 == 0):
                self.notify.alert("MANUAL_ACTION_NEEDED",
                                  f"Could not sell {pos.get('name')} after {n} tries. Sell it yourself in Jupiter "
                                  f"if this keeps failing.", {"mint": addr, "reason": reason, "error": res.error},
                                  loud=True)
            return
        self.force_sell.discard(addr)
        self.record_close(pos, res, reason)

    def record_close(self, pos: dict, res: SellResult, reason: str) -> None:
        s, cfg = self.state, self.cfg
        closed_at = timeutil.now()
        exit_price = res.fill_price
        if self.mode == "LIVE":
            spent = float(pos.get("usd_spent") or cfg["position_size_usd"])
            gross = res.usd_received - spent
            fees = float(pos.get("entry_fee_usd") or 0) + res.fee_usd
            net = gross - fees
        else:
            gross = cfg["position_size_usd"] * (exit_price / pos["entry"] - 1.0)
            net = gross - cfg["roundtrip_fee_usd"]
        row = {
            "name": pos["name"], "address": pos["address"], "opened": pos["opened"], "entry": pos["entry"],
            "tp": pos["tp"], "sl": pos["sl"], "liq_at_entry": pos.get("liq_at_entry", ""),
            "holders": pos.get("holders", ""), "top10_pct": pos.get("top10_pct", ""),
            "scout_price": pos.get("scout_price", ""), "pf_price": pos.get("pf_price", "blind"),
            "pf_mcap": pos.get("pf_mcap", "blind"), "pf_vs_dex_pct": pos.get("pf_vs_dex_pct", "blind"),
            "auditor_verdict": pos.get("auditor_verdict", "blind"),
            "auditor_verdict_time": pos.get("auditor_verdict_time", ""),
            "creator_history": pos.get("creator_history", "blind"),
            "high_since_entry": pos.get("high_since_entry", pos["entry"]),
            "low_since_entry": pos.get("low_since_entry", pos["entry"]),
            "closed": timeutil.iso(closed_at), "exit": exit_price, "reason": reason,
            "gross_pnl": round(gross, 2), "net_pnl": round(net, 2),
        }
        fields = list(TRADE_FIELDS)
        if self.mode == "LIVE":
            row.update({"mode": "LIVE", "buy_tx_sig": pos.get("buy_tx_sig", ""), "sell_tx_sig": res.tx_sig,
                        "actual_entry_price": pos["entry"], "actual_exit_price": exit_price,
                        "actual_fee_usd": round(float(pos.get("entry_fee_usd") or 0) + res.fee_usd, 6),
                        "slippage_bps_used": cfg["slippage_bps"], "usd_spent": round(float(pos.get("usd_spent") or 0), 6),
                        "usd_received": round(res.usd_received, 6),
                        "note": "; ".join(x for x in (pos.get("note", ""), res.note) if x)})
            fields += LIVE_TRADE_FIELDS
        s["day_pnl"] = round(s["day_pnl"] + net, 4)
        hrs = cfg["reentry_cooldown_hours"]
        if _valid(hrs) and hrs > 0:
            s.setdefault("cooldowns", {})[pos["address"]] = timeutil.iso(closed_at + timedelta(hours=hrs))
        s["open"] = [p for p in s["open"] if p["address"] != pos["address"]]
        s.setdefault("pending_trade_rows", []).append({"row": row, "fields": fields})
        self.data.save_state(s)  # the close is durable before anything else can fail
        self.flush_trade_rows()
        st = self.stats()
        self.notify.alert("CLOSE", f"{'GAIN' if net > 0 else 'LOSS'} {pos.get('name')} (${net:+.2f} net, {reason})", {
            "entry": pos["entry"], "exit": exit_price,
            "closed": f"{st['closed']}/{cfg['target_closed_trades']}",
            "W/L": f"{st['wins']}/{st['losses']} ({st['win_rate_pct']}%)",
            "realized_net": f"${st['realized_pnl']:+.2f}", "today_net": f"${s['day_pnl']:+.2f}",
            "open": len(s["open"]), **({"tx": res.tx_sig} if res.tx_sig else {})})
        if res.note:
            self.notify.alert("CHECK_TRADE", f"{pos.get('name')}: {res.note}. Check the wallet on Solscan.",
                              {"mint": pos["address"]}, loud=True)
        if res.tokens_left_raw:
            self.notify.alert("WARN", f"{pos.get('name')}: a small remainder was not sold",
                              {"raw_amount_left": res.tokens_left_raw})

    def flush_trade_rows(self) -> None:
        """Write queued closed-trade rows to trades.csv. If the file is locked (e.g. open in Excel on
        Windows) the rows stay queued in state.json and are retried every tick."""
        q = self.state.get("pending_trade_rows") or []
        while q:
            item = q[0]
            try:
                self.data.append_csv(self.data.trades_path, item["row"], item["fields"])
            except OSError as e:
                if not self.state.get("_csv_lock_alerted"):
                    self.notify.alert("WARN", f"trades.csv could not be written ({type(e).__name__}); is it open in "
                                      "Excel? The trade is saved and will be written when the file is free.")
                    self.state["_csv_lock_alerted"] = True
                return
            q.pop(0)
            self.state.pop("_csv_lock_alerted", None)
            self.data.save_state(self.state)

    # ------------------------------------------------------------------ entries
    def skip(self, addr: str, name: str, reason: str) -> None:
        key = skip_key(addr, reason)
        lst = self.state.setdefault("skipped_today", [])
        if key in lst:
            return
        lst.append(key)
        self.data.append_csv(self.data.skips_path, {"time": timeutil.iso(timeutil.now()), "address": addr,
                                                     "name": name, "reason": reason}, SKIP_FIELDS)

    def load_verdicts(self) -> dict[str, dict] | None:
        if not self.data.verdicts_path.exists():
            return None
        out: dict[str, dict] = {}
        for r in self.data.read_csv(self.data.verdicts_path):
            addr = (r.get("address") or "").strip()
            v = (r.get("verdict") or "").strip().upper()
            if addr and v:
                out[addr] = {"verdict": v, "time": (r.get("time") or "").strip()}
        return out

    def max_open(self) -> int:
        n = self.cfg["max_open_positions"]
        return min(n, self.cfg["live_max_open"]) if self.mode == "LIVE" else n

    def deployed_usd(self) -> float:
        return sum(float(p.get("usd_spent") or self.cfg["position_size_usd"]) for p in self.state["open"])

    async def scan(self) -> None:
        s, cfg = self.state, self.cfg
        self.health.last_scan = timeutil.iso(timeutil.now())
        now = timeutil.now()
        cooldowns = s.setdefault("cooldowns", {})
        for c in list(cooldowns):
            try:
                if timeutil.parse(cooldowns[c]) <= now:
                    del cooldowns[c]
            except (TypeError, ValueError):
                log.warning("bad cooldown timestamp for %s; keeping it blocked", c)
        if not self.entries_on or self.paused:
            return
        if self.mode != "LIVE" and self.data.count_closed() >= cfg["target_closed_trades"]:
            log.info("target of %s closed trades reached; not opening new positions", cfg["target_closed_trades"])
            return
        if s["day_pnl"] <= -cfg["daily_loss_cap_usd"]:
            if s.get("loss_cap_alerted_day") != s.get("day"):
                self.notify.alert("DAILY_LOSS_CAP_HIT", "DAILY LOSS CAP HIT — new entries paused until midnight rollover",
                                  {"today_net": f"${s['day_pnl']:+.2f}", "cap": f"-${cfg['daily_loss_cap_usd']:.2f}"},
                                  loud=True)
                s["loss_cap_alerted_day"] = s.get("day", "")
            return
        if self.mode == "LIVE" and self.data.unresolved_journal():
            log.warning("a real trade has an unknown outcome; no new real entries until the wallet confirms it")
            return
        if not self.data.watchlist_path.exists():
            log.info("watchlist.csv not found yet; skipping scan")
            return
        verdicts = self.load_verdicts()
        if verdicts is None:
            log.info("auditor_verdicts.csv not found; skipping scan (fail-closed)")
            return
        held = {p["address"] for p in s["open"]}
        pending = {a.mint for a in self.approvals.values() if a.status in ("pending", "approved")}
        rows = self.data.read_csv(self.data.watchlist_path)
        candidates = []
        for row in rows:
            addr = (row.get("address") or "").strip()
            name = (row.get("name") or addr[:8]).strip()
            if not addr or addr in held or addr in pending:
                continue
            if self.mode == "LIVE" and s.setdefault("live_blocked_today", {}).get(addr) == s.get("day"):
                continue  # the safety gate already refused it today; don't keep asking for approval
            if cooldowns.get(addr):
                self.skip(addr, name, f"re-entry cooldown active until {cooldowns[addr]}")
                continue
            v = verdicts.get(addr)
            if not v:
                self.skip(addr, name, "blind: no Auditor verdict")
                continue
            if v["verdict"] != "ENTRY OK":
                self.skip(addr, name, f"auditor verdict: {v['verdict']}")
                continue
            candidates.append((addr, name, row, v))
        if not candidates:
            return
        prices = await self.market.prices([c[0] for c in candidates])
        for addr, name, row, v in candidates:
            if not self.entries_on or self.paused:
                break  # Stop/Pause pressed while this scan was running
            if len(s["open"]) + self._pending_count() >= self.max_open():
                break
            live = prices.get(addr)
            if not live:
                self.skip(addr, name, "blind: no live price")
                continue
            why = passes_filters(cfg, row, live)
            if why:
                self.skip(addr, name, why)
                continue
            await self._enter(row, addr, name, live, v)

    def _pending_count(self) -> int:
        return sum(1 for a in self.approvals.values() if a.status in ("pending", "approved"))

    async def _enter(self, row: dict, addr: str, name: str, live: dict, verdict: dict) -> None:
        s = self.state
        if self.mode == "SHEET" and s["sheets_printed"].get(addr) == s.get("day"):
            return  # bot.py bug 4: the same sheet was printed every scan
        creator = "blind"
        if self.cfg["creator_check"] == "rugcheck":
            creator = await self.market.creator_history(addr)
        if self.mode == "LIVE" and not creator.startswith("rugcheck_ok"):
            self.skip(addr, name, f"blind: creator_history not allowed in LIVE mode ({creator.split(':')[0]})")
            return
        if self.mode == "LIVE" and self.cfg["live_require_approval"]:
            a = Approval(id=uuid.uuid4().hex[:10], mint=addr, name=name, row=row, verdict=verdict,
                         created=timeutil.now())
            self.approvals[a.id] = a
            self.notify.alert("APPROVAL_NEEDED", f"Buy {name} for ${self.cfg['position_size_usd']:.2f}?", {
                "mint": addr, "dex_price": live["price"], "liquidity": f"${live['liq']:,.0f}",
                "creator": creator, "expires_in": f"{int(self.cfg['live_approval_timeout_seconds'])}s"},
                buttons=("approval", a.id))
            return
        await self._buy(row, addr, name, live, verdict, creator)

    async def _buy(self, row, addr, name, live, verdict, creator) -> None:
        s = self.state
        ctx = BuyContext(mint=addr, name=name, live=live, creator_history=creator,
                         open_live_count=len(s["open"]), day_pnl=s["day_pnl"], deployed_usd=self.deployed_usd())
        if self.mode == "LIVE":
            res: BuyResult = await self.ex.buy(ctx, may_proceed=lambda: self.entries_on and not self.paused)
        else:
            res = await self.ex.buy(ctx)
        if self.mode == "SHEET":
            if res.sheet:
                s["sheets_printed"][addr] = s.get("day")
                self.data.append_csv(self.data.sheets_path, dict(time=timeutil.iso(timeutil.now()), **res.sheet))
                self.notify.alert("TRADE_SHEET", f"{name}: sign this yourself in Jupiter (not auto-traded)",
                                  res.sheet)
            return
        if not res.ok:
            self.skip(addr, name, res.error or "buy failed")
            if self.mode == "LIVE":
                if (res.error or "").startswith(UNKNOWN):
                    self.notify.alert("BUY_UNKNOWN", f"{name}: Jupiter did not confirm the buy. No new real entries "
                                      "until the wallet shows whether it landed (checked every tick).",
                                      {"mint": addr, "detail": res.error}, loud=True)
                else:
                    s.setdefault("live_blocked_today", {})[addr] = s.get("day")
                    self.notify.alert("BUY_BLOCKED", f"{name}: not bought", {"reason": res.error},
                                      loud="on-chain" in (res.error or ""))
            return
        pf = None
        if self.cfg["pumpfun_audit"]:
            try:
                pf = await self.market.pumpfun(addr)
            except Exception:
                pf = None
        self.open_position(row, live, res, pf=pf, verdict_info=verdict, creator=creator)

    def open_position(self, row, live, res: BuyResult, pf=None, verdict_info=None, creator="blind") -> dict:
        cfg = self.cfg
        entry = res.fill_price
        if pf and _valid(pf.get("pf_price")) and pf["pf_price"] > 0:
            pf_price, pf_mcap = pf["pf_price"], pf.get("pf_mcap", "blind")
            pf_vs = round(abs(pf_price - entry) / entry * 100.0, 4)
        else:
            pf_price = pf_mcap = pf_vs = "blind"
        now = timeutil.now()
        pos = {
            "address": row["address"].strip(), "name": (row.get("name") or row["address"][:8]).strip(),
            "entry": entry, "tp": entry * (1 + cfg["take_profit_pct"] / 100.0),
            "sl": entry * (1 - cfg["stop_loss_pct"] / 100.0), "opened": timeutil.iso(now),
            "deadline": timeutil.iso(now + timedelta(hours=cfg["time_exit_hours"])),
            "liq_at_entry": round(live["liq"], 2), "dex_pair": live.get("pair", ""),
            "holders": float(row["holders"]), "top10_pct": float(row["top10_pct"]),
            "scout_price": float(row["scout_price"]), "pf_price": pf_price, "pf_mcap": pf_mcap,
            "pf_vs_dex_pct": pf_vs, "auditor_verdict": (verdict_info or {}).get("verdict", "blind"),
            "auditor_verdict_time": (verdict_info or {}).get("time", ""), "creator_history": creator,
            "high_since_entry": entry, "low_since_entry": entry, "last_price": live["price"],
            "mode": self.mode,
        }
        if self.mode == "LIVE":
            pos.update({"buy_tx_sig": res.tx_sig, "tokens_received": res.tokens_received,
                        "tokens_raw": res.tokens_raw, "entry_fee_usd": res.fee_usd, "usd_spent": res.usd_spent,
                        "dex_price_at_entry": live["price"]})
        self.state["open"].append(pos)
        self.data.save_state(self.state)
        self.notify.alert("OPEN", f"{pos['name']} ({pos['address'][:8]}...)", {
            "mode": self.mode, "entry": pos["entry"], "tp": pos["tp"], "sl": pos["sl"],
            "liq_usd": pos["liq_at_entry"], "verdict": pos["auditor_verdict"],
            **({"tx": res.tx_sig} if res.tx_sig else {})})
        return pos

    async def process_approvals(self) -> None:
        now = timeutil.now()
        for a in list(self.approvals.values()):
            age = (now - a.created).total_seconds()
            if a.status == "pending" and age > self.cfg["live_approval_timeout_seconds"]:
                a.status = "expired"
                self.notify.log_line("APPROVAL", f"EXPIRED {a.name}")
            elif a.status == "approved":
                a.status = "done"
                if not self.entries_on or self.paused:
                    self.notify.alert("APPROVAL", f"{a.name}: entries are OFF now, so nothing was bought")
                    continue
                if self.data.unresolved_journal():
                    self.notify.alert("APPROVAL", f"{a.name}: an earlier real trade is still unconfirmed, so nothing "
                                      "was bought")
                    continue
                if any(p["address"] == a.mint for p in self.state["open"]) or len(self.state["open"]) >= self.max_open():
                    continue
                live = await self.market.price(a.mint)
                if not live:
                    self.notify.alert("APPROVAL", f"{a.name}: no live price after approval, nothing bought")
                    continue
                why = passes_filters(self.cfg, a.row, live)
                if why:
                    self.notify.alert("APPROVAL", f"{a.name}: no longer passes the rules ({why}), nothing bought")
                    continue
                creator = await self.market.creator_history(a.mint)
                await self._buy(a.row, a.mint, a.name, live, a.verdict, creator)
            if a.status in ("done", "expired", "rejected") and age > 3600:
                self.approvals.pop(a.id, None)

    # ------------------------------------------------------------------ LIVE crash / unknown-outcome recovery
    UNKNOWN_GRACE_SECONDS = 600

    async def reconcile_pending(self) -> None:
        """Resolve journal entries whose outcome is unknown (crash or unclear /execute) against the wallet.
        A buy whose tokens show up is adopted as a position; a sell whose tokens are gone is closed.
        After 10 minutes with no change, the trade is treated as not having happened."""
        ex = self.ex
        for e in self.data.unresolved_journal():
            mint = e.get("mint", "")
            try:
                raw, dec = await ex.rpc.token_balance(ex.owner, mint)
            except Exception as err:
                log.warning("reconcile: balance read failed for %s: %s", mint, type(err).__name__)
                continue
            try:
                age = (timeutil.now() - timeutil.parse(e.get("at", ""))).total_seconds()
            except ValueError:
                age = self.UNKNOWN_GRACE_SECONDS + 1
            held = next((p for p in self.state["open"] if p["address"] == mint), None)
            if e.get("side") == "buy" and raw > 0 and not held:
                live = await self.market.price(mint) or {"price": 0.0, "liq": 0.0}
                tokens = raw / 10 ** dec
                price = live["price"] or 0.0
                spent = self.cfg["position_size_usd"]
                res = BuyResult(ok=True, fill_price=(spent / tokens) if tokens else (price or 1e-12),
                                tokens_received=tokens, tokens_raw=raw, usd_spent=spent,
                                tx_sig=e.get("signature", "unknown"))
                row = {"address": mint, "name": e.get("name") or mint[:8], "holders": 0, "top10_pct": 0,
                       "scout_price": price}
                pos = self.open_position(row, live, res, creator="recovered")
                pos["note"] = "adopted from wallet after an unconfirmed buy; entry estimated from the planned spend"
                self.data.save_state(self.state)
                self.notify.alert("RECOVERED", f"The unconfirmed buy of {pos['name']} did land. It is now managed "
                                  "with the normal take-profit, stop-loss and time exit.", {"tokens": tokens}, loud=True)
                self.data.journal({"id": e["id"], "status": "reconciled", "token_raw_now": raw})
            elif e.get("side") == "sell" and raw == 0 and held:
                ref = held.get("last_price") or held["entry"]
                tokens = float(held.get("tokens_received") or 0)
                self.record_close(held, SellResult(ok=True, fill_price=ref, usd_received=tokens * ref,
                                                   note="sell was unconfirmed but the tokens are gone; amounts estimated"),
                                  "unconfirmed_sell")
                self.data.journal({"id": e["id"], "status": "reconciled", "token_raw_now": raw})
            elif age > self.UNKNOWN_GRACE_SECONDS:
                self.data.journal({"id": e["id"], "status": "reconciled", "token_raw_now": raw,
                                   "note": "no wallet change after 10 minutes; treated as not executed"})
                self.notify.alert("RECOVERED", f"Unconfirmed {e.get('side')} of {e.get('name')}: no wallet change after "
                                  "10 minutes, treated as not executed. Real entries can resume.")

    async def reconcile(self) -> None:
        """Startup check for LIVE: resolve the journal, then make sure every open position is in the wallet."""
        ex = self.ex
        try:
            await self.reconcile_pending()
            for p in self.state["open"]:
                raw, _ = await ex.rpc.token_balance(ex.owner, p["address"])
                if raw == 0:
                    self.notify.alert("MANUAL_ACTION_NEEDED", f"{p['name']} is open in state.json but the wallet "
                                      "holds none of it. Check the wallet.", {"mint": p["address"]}, loud=True)
        except Exception as e:
            log.exception("reconcile failed")
            self.notify.alert("ERROR", f"Startup wallet reconcile failed: {type(e).__name__}. Entries stay OFF.",
                              loud=True)
            self.state["control"]["entries_on"] = False

    # ------------------------------------------------------------------ reporting
    def stats(self) -> dict:
        rows = self.data.read_csv(self.data.trades_path)
        pnl = []
        for r in rows:
            try:
                v = float(r.get("net_pnl", ""))
                if math.isfinite(v):
                    pnl.append(v)
            except ValueError:
                pass
        wins = [x for x in pnl if x > 0]
        top3 = sum(sorted(wins, reverse=True)[:3])
        return {"closed": len(rows), "wins": len(wins), "losses": len(pnl) - len(wins),
                "win_rate_pct": round(100 * len(wins) / len(pnl), 1) if pnl else 0.0,
                "realized_pnl": round(sum(pnl), 2), "without_top3": round(sum(pnl) - top3, 2),
                "today_pnl": round(float(self.state.get("day_pnl", 0.0)), 2)}

    def status(self) -> dict:
        st = self.stats()
        size = self.cfg["position_size_usd"]
        open_rows = []
        for p in self.state["open"]:
            lp = p.get("last_price")
            unreal = None
            if _valid(lp) and p["entry"] > 0:
                unreal = round(size * (lp / p["entry"] - 1) - (self.cfg["roundtrip_fee_usd"] if self.mode != "LIVE" else 0), 2)
            open_rows.append({"name": p["name"], "address": p["address"], "entry": p["entry"], "tp": p["tp"],
                              "sl": p["sl"], "last_price": lp, "unrealized_net": unreal, "opened": p["opened"],
                              "deadline": p["deadline"], "sell_failures": p.get("sell_failures", 0)})
        return {
            "mode": self.mode, "label": self.label, "entries_on": self.entries_on, "paused": self.paused,
            "state": "PAUSED" if self.paused else ("ON" if self.entries_on else "OFF"),
            "open": open_rows, "open_count": len(open_rows), "max_open": self.max_open(),
            "closed": st["closed"], "target_closed": self.cfg["target_closed_trades"],
            "today_pnl": st["today_pnl"], "realized_pnl": st["realized_pnl"], "wins": st["wins"],
            "losses": st["losses"], "win_rate_pct": st["win_rate_pct"], "without_top3": st["without_top3"],
            "daily_loss_cap": self.cfg["daily_loss_cap_usd"], "last_scan": self.health.last_scan,
            "last_manage": self.health.last_manage, "last_error": self.health.last_error,
            "started": self.health.started, "settings_sha256": self.cfg.sha256[:8],
            "pending_approvals": [{"id": a.id, "name": a.name} for a in self.approvals.values() if a.status == "pending"],
        }

    def recent_trades(self, limit: int = 25) -> list[dict]:
        rows = self.data.read_csv(self.data.trades_path)
        return list(reversed(rows[-limit:]))
