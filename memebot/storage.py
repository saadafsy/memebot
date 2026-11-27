"""Data directory: single-instance lock, crash-safe state, Jawad's CSV formats, LIVE journal.

File formats match bot.py so an existing sim-bot data folder works as-is:
  state.json, trades.csv (23 columns; LIVE adds columns at the end), skipped.csv,
  watchlist.csv, auditor_verdicts.csv, alerts.log
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout

from . import timeutil

log = logging.getLogger(__name__)

TRADE_FIELDS = [
    "name", "address", "opened", "entry", "tp", "sl", "liq_at_entry", "holders", "top10_pct",
    "scout_price", "pf_price", "pf_mcap", "pf_vs_dex_pct", "auditor_verdict", "auditor_verdict_time",
    "creator_history", "high_since_entry", "low_since_entry", "closed", "exit", "reason",
    "gross_pnl", "net_pnl",
]
LIVE_TRADE_FIELDS = [
    "mode", "buy_tx_sig", "sell_tx_sig", "actual_entry_price", "actual_exit_price",
    "actual_fee_usd", "slippage_bps_used", "usd_spent", "usd_received", "note",
]
SKIP_FIELDS = ["time", "address", "name", "reason"]


class StateCorrupt(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def default_state() -> dict[str, Any]:
    return {"open": [], "day": timeutil.today(), "day_pnl": 0.0, "skipped_today": [],
            "cooldowns": {}, "loss_cap_alerted_day": ""}


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str, backup: bool = False) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if backup and path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError as e:
            log.warning("could not write backup of %s: %s", path.name, e)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class DataDir:
    def __init__(self, root: str | Path, inputs_from: str | Path | None = None):
        """inputs_from: read watchlist.csv / auditor_verdicts.csv from another folder (the real-money
        engine keeps its own state and trades in data/live but uses the Scout/Auditor files from data/)."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        inputs = Path(inputs_from).resolve() if inputs_from else self.root
        self.state_path = self.root / "state.json"
        self.trades_path = self.root / "trades.csv"
        self.skips_path = self.root / "skipped.csv"
        self.watchlist_path = inputs / "watchlist.csv"
        self.verdicts_path = inputs / "auditor_verdicts.csv"
        self.alerts_log = self.root / "alerts.log"
        self.journal_path = self.root / "live_journal.jsonl"
        self.sheets_path = self.root / "trade_sheets.csv"
        self.settings_history = self.root / "settings_hash_history.log"
        self._lock = FileLock(str(self.root / ".memebot.lock"))

    # ------------------------------------------------------------ lock
    def acquire_lock(self) -> None:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise AlreadyRunning(
                f"Another memebot is already running on {self.root}. Two copies would double-trade "
                "and corrupt state. Stop the other one first.") from None

    def release_lock(self) -> None:
        try:
            self._lock.release()
        except Exception:
            pass

    # ------------------------------------------------------------ state
    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return default_state()
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            if not isinstance(s, dict) or not isinstance(s.get("open", []), list):
                raise ValueError("state.json must be an object with an 'open' list")
            for p in s.get("open", []):
                if not isinstance(p, dict) or not p.get("address") or "entry" not in p:
                    raise ValueError("an open position is missing address/entry")
        except (ValueError, OSError) as e:
            bak = self.state_path.with_name("state.json.bak")
            raise StateCorrupt(
                f"state.json is unreadable ({e}). Refusing to start, because starting fresh would forget "
                f"open positions. Fix it by hand or restore the last good copy: {bak}") from None
        s.setdefault("open", [])
        s.setdefault("day", timeutil.today())
        s.setdefault("day_pnl", 0.0)
        s.setdefault("skipped_today", [])
        s.setdefault("cooldowns", {})
        s.setdefault("loss_cap_alerted_day", "")
        return s

    def save_state(self, s: dict[str, Any]) -> None:
        atomic_write_text(self.state_path, json.dumps(s, indent=2, default=str), backup=True)

    # ------------------------------------------------------------ csv
    def read_csv(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip(): (v if v is not None else "") for k, v in r.items()}
                    for r in csv.DictReader(f)]

    def append_csv(self, path: Path, row: dict[str, Any], fields: Iterable[str] | None = None) -> None:
        """Append a row. Same behavior as bot.py's append(): a new column triggers an atomic
        rewrite with the merged header, existing rows get blanks for the new columns."""
        fields = list(fields or row)
        row = {k: row.get(k, "") for k in fields}
        if not path.exists() or path.stat().st_size == 0:
            buf = _csv_text([row], fields, header=True)
            atomic_write_text(path, buf)
            return
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            existing = list(reader.fieldnames or [])
            missing = [k for k in fields if k not in existing]
            rows = list(reader) if missing else None
        if rows is None:
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=existing, restval="", extrasaction="ignore").writerow(row)
                f.flush()
                os.fsync(f.fileno())
            return
        merged = existing + missing
        atomic_write_text(path, _csv_text(rows + [row], merged, header=True))

    def count_closed(self, mode: str | None = None) -> int:
        rows = self.read_csv(self.trades_path)
        if mode is None:
            return len(rows)
        return sum(1 for r in rows if (r.get("mode") or "SIM") == mode)

    # ------------------------------------------------------------ journal (LIVE)
    def journal(self, entry: dict[str, Any]) -> None:
        entry = dict(entry, at=timeutil.iso(timeutil.now()))
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_journal(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        out = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("ignoring a torn line in live_journal.jsonl")
        return out

    def unresolved_journal(self) -> list[dict[str, Any]]:
        """Journal intents ('sent') with no matching 'done'/'failed' entry."""
        by_id: dict[str, dict[str, Any]] = {}
        for e in self.read_journal():
            jid = e.get("id")
            if not jid:
                continue
            if e.get("status") == "sent":
                by_id[jid] = e
            elif e.get("status") in ("done", "failed", "reconciled"):
                by_id.pop(jid, None)
        return list(by_id.values())


def _csv_text(rows: list[dict[str, Any]], fields: list[str], header: bool) -> str:
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, restval="", extrasaction="ignore", lineterminator="\r\n")
    if header:
        w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
