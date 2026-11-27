"""Settings and environment loading.

settings.json is Jawad's file and is never written by this program. His 15 keys are required
and validated; every key this project adds is optional and has a default, so his file works
byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODES = ("SIM", "SHEET", "LIVE")
LIVE_ACK_PHRASE = "I UNDERSTAND THIS TRADES REAL MONEY"

# Jawad's original keys: name -> (type, min, max)
REQUIRED_KEYS: dict[str, tuple[type, float, float]] = {
    "position_size_usd": (float, 0.01, 100_000),
    "take_profit_pct": (float, 0.01, 10_000),
    "stop_loss_pct": (float, 0.01, 99.99),
    "time_exit_hours": (float, 0.01, 24 * 365),
    "roundtrip_fee_usd": (float, 0, 10_000),
    "max_open_positions": (int, 1, 1000),
    "daily_loss_cap_usd": (float, 0.01, 1_000_000),
    "min_liquidity_usd": (float, 0, 1e12),
    "min_holders": (float, 0, 1e12),
    "max_top10_pct": (float, 0, 100),
    "max_price_mismatch_pct": (float, 0, 100),
    "poll_seconds": (float, 5, 86_400),
    "target_closed_trades": (int, 1, 1_000_000),
    "reentry_cooldown_hours": (float, 0, 24 * 365),
}

# Optional keys added by this project: name -> (default, type, min, max)
OPTIONAL_KEYS: dict[str, tuple[Any, type, float, float]] = {
    "lane": ("MAIN SIM", str, 0, 0),
    "timezone": ("America/Toronto", str, 0, 0),
    "exit_poll_seconds": (15, float, 3, 3600),
    "extreme_move_pct": (50, float, 1, 10_000),
    "time_exit_grace_minutes": (60, float, 0, 10_000),
    "slippage_bps": (300, int, 1, 5000),
    "creator_check": ("rugcheck", str, 0, 0),
    "live_max_open": (3, int, 1, 100),
    "live_max_price_impact_pct": (3.0, float, 0.01, 100),
    "live_max_roundtrip_loss_pct": (10.0, float, 0.1, 100),
    "live_wallet_budget_usd": (40.0, float, 0.01, 1_000_000),
    "live_min_sol_reserve": (0.02, float, 0, 1000),
    "live_require_approval": (True, bool, 0, 0),
    "live_approval_timeout_seconds": (120, float, 10, 3600),
    "live_sell_max_failures": (5, int, 1, 1000),
    "live_quote_mint": ("So11111111111111111111111111111111111111112", str, 0, 0),
    "pumpfun_audit": (True, bool, 0, 0),
    "live_requires_paper_target": (True, bool, 0, 0),
}


class SettingsError(ValueError):
    pass


@dataclass
class Settings:
    raw: dict[str, Any]
    sha256: str
    path: Path
    values: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, k: str) -> Any:
        return self.values[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self.values.get(k, default)

    @property
    def mode(self) -> str:
        return self.values["mode"]


def _coerce(name: str, v: Any, typ: type, lo: float, hi: float) -> Any:
    if typ is bool:
        if not isinstance(v, bool):
            raise SettingsError(f"{name} must be true or false, got {v!r}")
        return v
    if typ is str:
        if not isinstance(v, str) or not v.strip():
            raise SettingsError(f"{name} must be a non-empty string")
        return v.strip()
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SettingsError(f"{name} must be a number, got {v!r}")
    if not math.isfinite(v):
        raise SettingsError(f"{name} must be finite")
    if typ is int and float(v) != int(v):
        raise SettingsError(f"{name} must be a whole number, got {v!r}")
    if not (lo <= v <= hi):
        raise SettingsError(f"{name}={v} is outside the allowed range [{lo}, {hi}]")
    return int(v) if typ is int else float(v) if isinstance(v, float) else v


def load_settings(path: str | Path) -> Settings:
    p = Path(path)
    data = p.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SettingsError(f"{p} is not valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"{p} must contain a JSON object")
    vals: dict[str, Any] = {}
    mode = str(raw.get("mode", "SIM")).strip().upper()
    if mode not in MODES:
        raise SettingsError(f"mode must be one of {MODES}, got {raw.get('mode')!r}")
    vals["mode"] = mode
    for k, (typ, lo, hi) in REQUIRED_KEYS.items():
        if k not in raw:
            raise SettingsError(f"settings.json is missing required key {k!r}")
        vals[k] = _coerce(k, raw[k], typ, lo, hi)
    for k, (default, typ, lo, hi) in OPTIONAL_KEYS.items():
        vals[k] = _coerce(k, raw[k], typ, lo, hi) if k in raw else default
    unknown = set(raw) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS) - {"mode"}
    vals["_unknown_keys"] = sorted(unknown)
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(vals["timezone"])
    except Exception:
        raise SettingsError(f"timezone {vals['timezone']!r} is not a known IANA zone "
                            "(on Windows run: pip install tzdata)") from None
    if vals["creator_check"] not in ("rugcheck", "off"):
        raise SettingsError("creator_check must be 'rugcheck' or 'off'")
    return Settings(raw=raw, sha256=digest, path=p, values=vals)


# ---------------------------------------------------------------- environment

@dataclass(frozen=True)
class Env:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    private_key: str = ""
    jupiter_api_key: str = ""
    rpc_url: str = ""
    live_ack: str = ""
    panel_password: str = ""
    panel_host: str = "127.0.0.1"
    panel_port: int = 8787
    panel_allowed_hosts: tuple[str, ...] = ()
    jupiter_base: str = "https://api.jup.ag/swap/v2"

    def secrets(self) -> list[str]:
        return [s for s in (self.telegram_token, self.private_key, self.jupiter_api_key,
                            self.panel_password, self.rpc_url) if s and len(s) >= 6]


def load_env(environ: dict[str, str] | None = None) -> Env:
    e = os.environ if environ is None else environ
    port_s = (e.get("PANEL_PORT") or "8787").strip()
    try:
        port = int(port_s)
    except ValueError:
        raise SettingsError(f"PANEL_PORT must be a number, got {port_s!r}") from None
    hosts = tuple(h.strip().lower() for h in (e.get("PANEL_ALLOWED_HOSTS") or "").split(",") if h.strip())
    return Env(
        telegram_token=(e.get("ALERT_TELEGRAM_TOKEN") or "").strip(),
        telegram_chat_id=(e.get("ALERT_TELEGRAM_CHAT_ID") or "").strip(),
        private_key=(e.get("MEMEBOT_PRIVATE_KEY") or "").strip(),
        jupiter_api_key=(e.get("JUPITER_API_KEY") or "").strip(),
        rpc_url=(e.get("SOLANA_RPC_URL") or "").strip(),
        live_ack=(e.get("MEMEBOT_LIVE_ACK") or "").strip(),
        panel_password=e.get("PANEL_PASSWORD") or "",
        panel_host=(e.get("PANEL_HOST") or "127.0.0.1").strip(),
        panel_port=port,
        panel_allowed_hosts=hosts,
        jupiter_base=(e.get("JUPITER_BASE_URL") or "https://api.jup.ag/swap/v2").rstrip("/"),
    )


def key_in_files(*paths) -> list[str]:
    """Files that contain a non-empty MEMEBOT_PRIVATE_KEY line. The key must live only in the
    environment of the running process, never in a file (Jawad's rule)."""
    hits = []
    for p in paths:
        try:
            for line in Path(p).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                k, _, v = line.partition("=")
                if k.strip().removeprefix("export ").strip() == "MEMEBOT_PRIVATE_KEY" and v.strip().strip("'\""):
                    hits.append(str(p))
                    break
        except OSError:
            continue
    return hits


def live_gate_problems(settings: Settings, env: Env, env_files: tuple = ()) -> list[str]:
    """Everything that must be true before LIVE mode may sign anything. Empty list = OK."""
    probs = []
    for f in key_in_files(*env_files):
        probs.append(f"MEMEBOT_PRIVATE_KEY is written in {f}. Delete it from the file; set it only as an "
                     "environment variable in the terminal that starts the bot")
    if settings.mode != "LIVE":
        probs.append("settings.json mode is not LIVE")
    if not env.private_key:
        probs.append("MEMEBOT_PRIVATE_KEY is not set")
    if not env.jupiter_api_key:
        probs.append("JUPITER_API_KEY is not set")
    if not env.rpc_url:
        probs.append("SOLANA_RPC_URL is not set")
    if env.live_ack != LIVE_ACK_PHRASE:
        probs.append(f'MEMEBOT_LIVE_ACK must be exactly "{LIVE_ACK_PHRASE}"')
    if settings["creator_check"] != "rugcheck":
        probs.append("creator_check must be 'rugcheck' in LIVE (blind creator history blocks LIVE buys)")
    return probs
