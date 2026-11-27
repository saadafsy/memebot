"""
Stage 5 optional Telegram alert module (REVISED DRAFT — NOT APPLIED).
Zero hardcoded secrets, zero wallet/key/signing code.
Reads ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID from environment variables ONLY.
Always appends to local alerts.log; Telegram delivery is best-effort (5s timeout) and never raises.
Error messages NEVER print the URL, token, or raw exception text that could contain them.
"""
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

ALERTS_LOG = "alerts.log"
TOKEN_ENV = "ALERT_TELEGRAM_TOKEN"
CHAT_ID_ENV = "ALERT_TELEGRAM_CHAT_ID"
TELEGRAM_API_BASE = "https://api.telegram.org"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TraderJoeAlerts/1.0)", "Content-Type": "application/json"}


def _ts():
    return datetime.now().isoformat(timespec="seconds")


def _is_valid_num(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def compute_running_stats(
    trades_path="trades.csv",
    state=None,
    target_closed=100,
    position_size_usd=20.0,
    roundtrip_fee_usd=1.8,
    live_prices=None,
):
    """Compute running P/L stats from trades.csv + state. All figures are net of fees."""
    state = state or {}
    live_prices = live_prices or {}
    rows = []
    if trades_path and os.path.exists(trades_path):
        try:
            with open(trades_path, "r", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        net = float(r.get("net_pnl", ""))
                    except (TypeError, ValueError):
                        continue
                    if _is_valid_num(net):
                        rows.append({"name": (r.get("name") or "?").strip(), "net": net})
        except OSError as e:
            print(f"[warn] alerts.py could not read {trades_path}: {type(e).__name__}", file=sys.stderr)

    closed_count = len(rows)
    wins = [r for r in rows if r["net"] > 0]
    losses = [r for r in rows if r["net"] <= 0]
    win_rate_pct = round(100.0 * len(wins) / closed_count, 1) if closed_count else 0.0
    realized_pnl = round(sum(r["net"] for r in rows), 2)

    if rows:
        best_r = max(rows, key=lambda r: r["net"])
        worst_r = min(rows, key=lambda r: r["net"])
        best_trade = f"{best_r['name']} (${best_r['net']:+.2f})"
        worst_trade = f"{worst_r['name']} (${worst_r['net']:+.2f})"
    else:
        best_trade = "n/a"
        worst_trade = "n/a"

    open_positions = state.get("open") or []
    open_count = len(open_positions)
    unrealized_sum = 0.0
    priced_open = 0
    blind_open = 0
    for pos in open_positions:
        addr = pos.get("address", "")
        entry = pos.get("entry")
        cur_price = live_prices.get(addr)
        if _is_valid_num(entry) and entry > 0 and _is_valid_num(cur_price) and cur_price > 0:
            gross = position_size_usd * (cur_price / entry - 1.0)
            unrealized_sum += gross - roundtrip_fee_usd
            priced_open += 1
        else:
            blind_open += 1

    if open_count == 0:
        unrealized_str = "$0.00"
    elif priced_open == 0:
        unrealized_str = f"blind ({blind_open} open unpriced)"
    elif blind_open > 0:
        unrealized_str = f"${unrealized_sum:+.2f} (partial; {blind_open} blind)"
    else:
        unrealized_str = f"${unrealized_sum:+.2f}"

    try:
        today_pnl = round(float(state.get("day_pnl", 0.0)), 2)
    except (TypeError, ValueError):
        today_pnl = 0.0

    return {
        "closed_count": closed_count,
        "target_closed": target_closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate_pct,
        "realized_pnl": realized_pnl,
        "unrealized_str": unrealized_str,
        "today_pnl": today_pnl,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "open_count": open_count,
    }


def send_alert(event_type, title, details=None, lane="MAIN SIM"):
    """Append alert to local alerts.log and optionally send via Telegram Bot API (HTTPS)."""
    details = details or {}
    lane_label = "FAST LANE" if str(lane).strip().upper() == "FAST LANE" else "MAIN SIM"
    payload_text = f"[{_ts()}] [{lane_label}] [{event_type}] {title}"
    if details:
        kv = " | ".join(f"{k}={v}" for k, v in details.items())
        payload_text = f"{payload_text} — {kv}"

    try:
        with open(ALERTS_LOG, "a", encoding="utf-8") as f:
            f.write(payload_text + "\n")
    except OSError as e:
        print(f"[warn] alerts.log write failed: {type(e).__name__}", file=sys.stderr)

    token = (os.environ.get(TOKEN_ENV) or "").strip()
    chat_id = (os.environ.get(CHAT_ID_ENV) or "").strip()
    if not token or not chat_id:
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": payload_text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        print(f"[warn] Telegram alert failed ({event_type}): HTTP {e.code}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[warn] Telegram alert failed ({event_type}): {type(e).__name__}", file=sys.stderr)
        return False


def alert_open(pos, lane="MAIN SIM"):
    return send_alert(
        "OPEN",
        f"{pos.get('name', '?')} ({pos.get('address', '')[:8]}...)",
        {
            "entry": pos.get("entry"),
            "tp": pos.get("tp"),
            "sl": pos.get("sl"),
            "liq_usd": pos.get("liq_at_entry"),
            "verdict": pos.get("auditor_verdict", "blind"),
        },
        lane=lane,
    )


def alert_close(
    pos,
    exit_price,
    reason,
    net_pnl,
    state,
    trades_path="trades.csv",
    target_closed=100,
    lane="MAIN SIM",
):
    outcome = "GAIN" if net_pnl > 0 else "LOSS"
    st = compute_running_stats(trades_path=trades_path, state=state, target_closed=target_closed)
    return send_alert(
        "CLOSE",
        f"{outcome} {pos.get('name', '?')} (${net_pnl:+.2f} net, {reason})",
        {
            "entry": pos.get("entry"),
            "exit": exit_price,
            "closed": f"{st['closed_count']}/{st['target_closed']}",
            "W/L": f"{st['wins']}/{st['losses']} ({st['win_rate_pct']}%)",
            "realized_net": f"${st['realized_pnl']:+.2f}",
            "today_net": f"${st['today_pnl']:+.2f}",
            "open": st["open_count"],
        },
        lane=lane,
    )


def alert_daily_summary(
    state,
    trades_path="trades.csv",
    target_closed=100,
    position_size_usd=20.0,
    roundtrip_fee_usd=1.8,
    live_prices=None,
    lane="MAIN SIM",
):
    st = compute_running_stats(
        trades_path=trades_path,
        state=state,
        target_closed=target_closed,
        position_size_usd=position_size_usd,
        roundtrip_fee_usd=roundtrip_fee_usd,
        live_prices=live_prices,
    )
    return send_alert(
        "DAILY_SUMMARY",
        f"Day {state.get('day', '?')} Summary (all net of fees)",
        {
            "realized_net": f"${st['realized_pnl']:+.2f}",
            "unrealized_net": st["unrealized_str"],
            "today_net": f"${st['today_pnl']:+.2f}",
            "closed": f"{st['closed_count']}/{st['target_closed']}",
            "W/L": f"{st['wins']}/{st['losses']}",
            "win_rate": f"{st['win_rate_pct']}%",
            "best": st["best_trade"],
            "worst": st["worst_trade"],
            "open": st["open_count"],
        },
        lane=lane,
    )


def alert_daily_loss_cap(day_pnl, daily_loss_cap_usd=15.0, lane="MAIN SIM"):
    return send_alert(
        "DAILY_LOSS_CAP_HIT",
        "DAILY LOSS CAP HIT — new entries paused until midnight rollover",
        {
            "today_net": f"${day_pnl:+.2f}",
            "cap": f"-${daily_loss_cap_usd:.2f}",
        },
        lane=lane,
    )
