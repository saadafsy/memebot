"""Alerts: every alert is appended to alerts.log in the same line format as Jawad's alerts.py,
then (if Telegram is configured) queued for delivery. Delivery problems never reach the engine.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Awaitable, Callable

from . import timeutil

log = logging.getLogger(__name__)

Sender = Callable[[str, Any], Awaitable[None]]
MAX_TG = 4096


def chunk_text(text: str, limit: int = MAX_TG) -> list[str]:
    """Split on line boundaries so each piece fits Telegram's 4096-character limit."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


class Outbox:
    """One Telegram delivery queue shared by every engine (paper and real)."""

    def __init__(self):
        self.sender: Sender | None = None
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=500)

    def push(self, text: str, buttons: Any = None) -> None:
        try:
            self.queue.put_nowait((text, buttons))
        except asyncio.QueueFull:
            log.warning("telegram queue full; dropping a message (still in alerts.log)")

    async def run_sender(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                text, buttons = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if self.sender is None:
                continue
            for i, part in enumerate(chunk_text(text)):
                for attempt in range(3):
                    try:
                        await self.sender(part, buttons if i == 0 else None)
                        break
                    except Exception as e:  # never let Telegram problems escape
                        log.warning("telegram send failed (%s), attempt %d", type(e).__name__, attempt + 1)
                        await asyncio.sleep(2 * (attempt + 1))


class Notifier:
    def __init__(self, data, lane: str = "MAIN SIM", outbox: Outbox | None = None, tag: str = ""):
        self.data = data
        lane_u = str(lane).strip().upper()
        self.lane = lane_u if lane_u in ("FAST LANE", "LIVE", "SHEET") else "MAIN SIM"
        self.outbox = outbox or Outbox()
        self.tag = tag
        self.history: list[str] = []

    def log_line(self, event: str, title: str, details: dict | None = None) -> str:
        line = f"[{timeutil.iso(timeutil.now())}] [{self.lane}] [{event}] {title}"
        if details:
            line += " — " + " | ".join(f"{k}={v}" for k, v in details.items())
        try:
            with open(self.data.alerts_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("alerts.log write failed: %s", type(e).__name__)
        self.history = (self.history + [line])[-50:]
        return line

    def alert(self, event: str, title: str, details: dict | None = None, *, buttons: Any = None,
              loud: bool = False) -> None:
        line = self.log_line(event, title, details)
        (log.warning if loud else log.info)("ALERT %s", line)
        text = f"<b>{html.escape(event)}</b> {html.escape(title)}"
        if details:
            text += "\n" + "\n".join(f"{html.escape(str(k))}: <code>{html.escape(str(v))}</code>"
                                     for k, v in details.items())
        if loud:
            text = "⚠️ " + text
        self.push(text, buttons)

    def push(self, text: str, buttons: Any = None) -> None:
        self.outbox.push((f"<b>[{html.escape(self.tag)}]</b> " if self.tag else "") + text, buttons)
