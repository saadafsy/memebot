"""Timezone-aware time helpers.

bot.py wrote naive local timestamps (no offset), so a deadline could shift by an hour across a
daylight-saving change. New timestamps carry their UTC offset (still readable by
datetime.fromisoformat). Old naive ones are read as wall-clock time in the configured zone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Toronto")


def set_tz(name: str) -> None:
    global _TZ
    _TZ = ZoneInfo(name)


def tz() -> ZoneInfo:
    return _TZ


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone(_TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(_TZ).isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    """Parse an ISO timestamp. Naive values are local wall time in the configured zone."""
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt


def today() -> str:
    return str(now().date())
