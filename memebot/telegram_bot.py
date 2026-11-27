"""Telegram control bot (python-telegram-bot 22).

Only the chat in ALERT_TELEGRAM_CHAT_ID is answered; messages from anyone else are ignored
without a reply. Paper trading always runs; real trading is a separate switch (/liveon) that only
exists when real money was armed on the computer (mode LIVE in settings.json + key + acknowledgement
in environment variables). Telegram can NOT change the mode, edit settings.json, or withdraw.
"""
from __future__ import annotations

import html
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .notify import chunk_text

log = logging.getLogger(__name__)

HELP = (
    "<b>memebot</b>\n"
    "/status  state and P/L of paper and real\n"
    "/positions  open positions\n"
    "/pnl  today and realized P/L\n"
    "/closed  last closed trades\n"
    "/on, /off  paper entries on/off\n"
    "/liveon, /liveoff  REAL-money entries on/off (only if armed on the computer; each buy still needs Approve)\n"
    "/pause  pause everything (no entries, no auto exits)\n"
    "/resume  resume exit management\n"
    "/sell NAME  force-sell one position (asks to confirm)\n"
    "/sellall  force-sell everything (asks to confirm)\n"
    "/settings  read-only settings\n"
    "Mode and settings can only be changed in settings.json on the computer, then restart."
)


def main_keyboard(has_live: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("▶ Paper on", callback_data="ctl:on:P"),
             InlineKeyboardButton("■ Paper off", callback_data="ctl:off:P")]]
    if has_live:
        rows.append([InlineKeyboardButton("▶ REAL on", callback_data="ctl:on:L"),
                     InlineKeyboardButton("■ REAL off", callback_data="ctl:off:L")])
    rows.append([InlineKeyboardButton("⏸ Pause everything", callback_data="ctl:pause:A"),
                 InlineKeyboardButton("⏯ Resume", callback_data="ctl:resume:A")])
    rows.append([InlineKeyboardButton("Status", callback_data="ctl:status:A")])
    return InlineKeyboardMarkup(rows)


def fmt_status(st: dict) -> str:
    e = html.escape
    lines = [
        f"<b>{e(st['label'])}</b> ({e(st['mode'])}) · entries <b>{e(st['state'])}</b>",
        f"Open: {st['open_count']}/{st['max_open']}   Closed: {st['closed']}/{st['target_closed']}",
        f"Today P/L: ${st['today_pnl']:+.2f} (cap -${st['daily_loss_cap']:.2f})",
        f"Realized P/L: ${st['realized_pnl']:+.2f}   W/L {st['wins']}/{st['losses']} ({st['win_rate_pct']}%)",
        f"Last scan: {e(st['last_scan'] or 'not yet')}",
    ]
    if st.get("last_error"):
        lines.append(f"Last error: <code>{e(st['last_error'][:200])}</code>")
    if st.get("pending_approvals"):
        lines.append("Waiting for approval: " + ", ".join(e(a["name"]) for a in st["pending_approvals"]))
    return "\n".join(lines)


def fmt_positions(st: dict) -> str:
    head = f"<b>{html.escape(st['label'])}</b>"
    if not st["open"]:
        return head + ": no open positions."
    out = [head]
    for p in st["open"]:
        u = p["unrealized_net"]
        out.append(f"<b>{html.escape(p['name'])}</b> entry {p['entry']:.8g} · now "
                   f"{(p['last_price'] or 0):.8g} · {('$%+.2f' % u) if u is not None else 'unpriced'}\n"
                   f"  TP {p['tp']:.8g} / SL {p['sl']:.8g} · exits by {html.escape(p['deadline'])}"
                   + (f"\n  ⚠ sell failures: {p['sell_failures']}" if p["sell_failures"] else ""))
    return "\n".join(out)


def fmt_closed(label: str, rows: list[dict]) -> str:
    head = f"<b>{html.escape(label)}</b>"
    if not rows:
        return head + ": no closed trades yet."
    return head + "\n" + "\n".join(
        f"{html.escape(r.get('name', '?'))}: {html.escape(r.get('reason', ''))} "
        f"net ${float(r.get('net_pnl') or 0):+.2f} ({html.escape(r.get('closed', '')[:16])})" for r in rows)


class TelegramControl:
    def __init__(self, token: str, chat_id: str, engines: list):
        self.chat_id = int(chat_id)
        self.engines = engines
        self.live = next((e for e in engines if e.mode == "LIVE"), None)
        self.paperish = [e for e in engines if e.mode != "LIVE"]
        self.app = Application.builder().token(token).build()
        only_me = filters.Chat(chat_id=self.chat_id)
        cmds = {
            "start": self.c_help, "help": self.c_help, "status": self.c_status, "positions": self.c_positions,
            "pnl": self.c_pnl, "closed": self.c_closed, "on": self.c_on, "off": self.c_off,
            "liveon": self.c_liveon, "liveoff": self.c_liveoff, "pause": self.c_pause, "resume": self.c_resume,
            "sell": self.c_sell, "sellall": self.c_sellall, "settings": self.c_settings, "mode": self.c_status,
        }
        for name, fn in cmds.items():
            self.app.add_handler(CommandHandler(name, fn, filters=only_me))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_error_handler(self.on_error)

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

    async def stop(self) -> None:
        for step in (self.app.updater.stop, self.app.stop, self.app.shutdown):
            try:
                await step()
            except Exception as e:
                log.debug("telegram shutdown step failed: %s", type(e).__name__)

    async def send(self, text: str, buttons=None) -> None:
        markup = None
        if isinstance(buttons, tuple) and buttons and buttons[0] == "approval":
            aid = buttons[1]
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve REAL buy", callback_data=f"ap:{aid}:y"),
                                            InlineKeyboardButton("❌ Reject", callback_data=f"ap:{aid}:n")]])
        await self.app.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=markup)

    async def reply(self, update: Update, text: str, markup=None) -> None:
        parts = chunk_text(text)
        for i, part in enumerate(parts):
            await update.effective_chat.send_message(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                                     reply_markup=markup if i == len(parts) - 1 else None)

    async def on_error(self, update, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.warning("telegram handler error: %s", type(context.error).__name__)

    # ---------------------------------------------------------------- actions (shared by commands and buttons)
    def act(self, verb: str, target: str) -> str:
        if target == "L" and self.live is None:
            return ("Real trading is not armed. It can only be armed on the computer: mode LIVE in settings.json, "
                    "the wallet key and acknowledgement in environment variables, then restart.")
        group = {"P": self.paperish, "L": [self.live], "A": self.engines}[target]
        msgs = []
        for e in group:
            if verb == "on":
                msgs.append(f"{e.label}: " + e.set_entries(True, "telegram"))
            elif verb == "off":
                msgs.append(f"{e.label}: " + e.set_entries(False, "telegram"))
            elif verb == "pause":
                msgs.append(f"{e.label}: " + e.set_paused(True, "telegram"))
            elif verb == "resume":
                msgs.append(f"{e.label}: " + e.set_paused(False, "telegram"))
        return "\n".join(msgs)

    def status_text(self) -> str:
        return "\n\n".join(fmt_status(e.status()) for e in self.engines)

    # ---------------------------------------------------------------- commands
    async def c_help(self, update: Update, ctx) -> None:
        await self.reply(update, HELP, main_keyboard(self.live is not None))

    async def c_status(self, update: Update, ctx) -> None:
        await self.reply(update, self.status_text(), main_keyboard(self.live is not None))

    async def c_positions(self, update: Update, ctx) -> None:
        await self.reply(update, "\n\n".join(fmt_positions(e.status()) for e in self.engines))

    async def c_pnl(self, update: Update, ctx) -> None:
        parts = []
        for e in self.engines:
            st = e.status()
            parts.append(f"<b>{html.escape(st['label'])}</b>\nToday: ${st['today_pnl']:+.2f}\n"
                         f"Realized: ${st['realized_pnl']:+.2f}\nWithout top 3 winners: ${st['without_top3']:+.2f}\n"
                         f"Closed {st['closed']}/{st['target_closed']}")
        await self.reply(update, "\n\n".join(parts))

    async def c_closed(self, update: Update, ctx) -> None:
        await self.reply(update, "\n\n".join(fmt_closed(e.label, e.recent_trades(10)) for e in self.engines))

    async def c_on(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("on", "P")))

    async def c_off(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("off", "P")))

    async def c_liveon(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("on", "L")))

    async def c_liveoff(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("off", "L")))

    async def c_pause(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("pause", "A")))

    async def c_resume(self, update: Update, ctx) -> None:
        await self.reply(update, html.escape(self.act("resume", "A")))

    def _find(self, key: str):
        for e in ([self.live] if self.live else []) + self.paperish:
            for p in e.state["open"]:
                if key and (p["address"] == key or p.get("name", "").lower() == key.lower()):
                    return e, p
        return None, None

    async def c_sell(self, update: Update, ctx) -> None:
        key = " ".join(ctx.args or []).strip()
        e, p = self._find(key)
        if not p:
            await self.reply(update, "Usage: /sell NAME (see /positions)")
            return
        tag = "L" if e.mode == "LIVE" else "P"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Confirm: sell {p['name']} ({e.label})",
                                                         callback_data=f"fs:{tag}:{p['address']}")]])
        await self.reply(update, f"Sell <b>{html.escape(p['name'])}</b> ({e.label}) now at market?", kb)

    async def c_sellall(self, update: Update, ctx) -> None:
        n = sum(len(e.state["open"]) for e in self.engines)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Confirm: sell ALL (paper and real)", callback_data="sa:yes")]])
        await self.reply(update, f"Sell all {n} open position(s) at market?", kb)

    async def c_settings(self, update: Update, ctx) -> None:
        cfg = self.engines[0].cfg
        body = json.dumps(cfg.raw, indent=2)
        await self.reply(update, f"settings.json (read-only, sha256 {cfg.sha256[:8]}):\n<pre>{html.escape(body)}</pre>")

    # ---------------------------------------------------------------- buttons
    async def on_button(self, update: Update, ctx) -> None:
        q = update.callback_query
        if not update.effective_chat or update.effective_chat.id != self.chat_id:
            return  # silently ignore other chats
        data = q.data or ""
        msg = "?"
        if data.startswith("ctl:"):
            _, verb, target = (data.split(":") + ["", ""])[:3]
            if verb == "status":
                await q.answer()
                await self.reply(update, self.status_text(), main_keyboard(self.live is not None))
                return
            msg = self.act(verb, target if target in ("P", "L", "A") else "A")
        elif data.startswith("fs:"):
            _, tag, addr = (data.split(":", 2) + ["", ""])[:3]
            eng = self.live if tag == "L" else None
            for e in ([eng] if eng else self.paperish):
                msg = e.request_force_sell(addr, "telegram")
                if not msg.startswith("No open"):
                    break
        elif data == "sa:yes":
            msg = "\n".join(f"{e.label}: " + e.request_sell_all("telegram") for e in self.engines)
        elif data.startswith("ap:"):
            _, aid, yn = (data.split(":") + ["", ""])[:3]
            msg = self.live.decide_approval(aid, yn == "y", "telegram") if self.live else "Real trading is not armed."
            try:
                await q.edit_message_reply_markup(None)
            except Exception:
                pass
        await q.answer(msg[:190])
        await self.reply(update, html.escape(msg))
