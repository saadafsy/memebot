import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

try:
    import alerts
except ImportError:
    print("[warn] alerts.py not found; Telegram/file alerts disabled.", file=sys.stderr)
    alerts = None

with open("settings.json", "r") as _f:
    CFG = json.load(_f)

LANE = CFG.get("lane", "MAIN SIM")


def _safe_alert(label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[warn] alert hook {label} failed: {type(e).__name__}", file=sys.stderr)
        return False

STATE_FILE = "state.json"
TRADES = "trades.csv"
SKIPS = "skipped.csv"
WATCHLIST = "watchlist.csv"
VERDICTS = "auditor_verdicts.csv"
HEADERS_JSON = {"User-Agent": "Mozilla/5.0 (compatible; TraderJoeSimBot/1.0)", "Accept": "application/json"}
HEADERS_HTML = {"User-Agent": "Mozilla/5.0 (compatible; TraderJoeSimBot/1.0)", "Accept": "text/html"}


def now():
    return datetime.now()


def _is_valid_num(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def fetch_price(address):
    """Public DexScreener lookup. Returns dict or None."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    req = urllib.request.Request(url, headers=HEADERS_JSON)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pairs = json.load(r).get("pairs") or []
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[warn] DexScreener fetch failed for {address}: {e}", file=sys.stderr)
        return None
    sol_pairs = []
    for p in pairs:
        if p.get("chainId") != "solana" or not p.get("priceUsd"):
            continue
        try:
            price = float(p["priceUsd"])
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if _is_valid_num(price) and price > 0 and _is_valid_num(liq) and liq >= 0:
            sol_pairs.append({
                "price": price,
                "liq": liq,
                "pair": p.get("pairAddress", ""),
                "dex": p.get("dexId", ""),
            })
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda x: x["liq"])


def fetch_pumpfun_price(address):
    """Read-only pump.fun/coin/<mint> HTML lookup (Stage 2 audit field only, non-blocking)."""
    url = f"https://pump.fun/coin/{address}"
    req = urllib.request.Request(url, headers=HEADERS_HTML)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"[warn] pump.fun fetch failed for {address}: {e}", file=sys.stderr)
        return None
    if address not in html:
        print(f"[warn] pump.fun page did not contain mint {address}", file=sys.stderr)
        return None
    m_price = re.search(r'\\?"price_usd\\?":\s*([0-9.eE+-]+)', html)
    if not m_price:
        return None
    try:
        pf_price = float(m_price.group(1))
    except (TypeError, ValueError):
        return None
    if not (_is_valid_num(pf_price) and pf_price > 0):
        return None
    pf_mcap = "blind"
    m_mc = re.search(r'\\?"usd_market_cap\\?":\s*([0-9.eE+-]+)', html)
    if m_mc:
        try:
            mc = float(m_mc.group(1))
            if _is_valid_num(mc) and mc > 0:
                pf_mcap = round(mc, 2)
        except (TypeError, ValueError):
            pass
    return {"pf_price": pf_price, "pf_mcap": pf_mcap}


def load_verdicts(path=VERDICTS):
    """Load Auditor verdicts (address,verdict,time). Latest row per address wins."""
    if not os.path.exists(path):
        return None
    verdicts = {}
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            addr = (row.get("address") or "").strip()
            verdict = (row.get("verdict") or "").strip().upper()
            ts = (row.get("time") or "").strip()
            if addr and verdict:
                verdicts[addr] = {"verdict": verdict, "time": ts}
    return verdicts


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"open": [], "day": str(now().date()), "day_pnl": 0.0, "skipped_today": [], "cooldowns": {}, "loss_cap_alerted_day": ""}
    with open(STATE_FILE, "r") as f:
        s = json.load(f)
    s.setdefault("skipped_today", [])
    s.setdefault("cooldowns", {})
    s.setdefault("loss_cap_alerted_day", "")
    return s


def save_state(s):
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)


def count_closed_trades():
    if not os.path.exists(TRADES):
        return 0
    with open(TRADES, "r", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def append(path, row):
    fields = list(row)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerow(row)
        return
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        if existing_fields == fields:
            existing_rows = None
        else:
            existing_rows = list(reader)
    if existing_rows is None:
        with open(path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writerow(row)
    else:
        merged_fields = existing_fields + [k for k in fields if k not in existing_fields]
        tmp = f"{path}.tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=merged_fields, restval="")
            w.writeheader()
            w.writerows(existing_rows)
            w.writerow(row)
        os.replace(tmp, path)


def skip(s, addr, name, reason):
    key = f"{addr}|{reason}"
    if key in s.get("skipped_today", []):
        return
    s.setdefault("skipped_today", []).append(key)
    append(SKIPS, {"time": now().isoformat(timespec="seconds"), "address": addr, "name": name, "reason": reason})


def passes_filters(row, live):
    try:
        holders = float(row.get("holders", ""))
        top10 = float(row.get("top10_pct", ""))
        if not (_is_valid_num(holders) and _is_valid_num(top10)):
            return "blind: holders/top10 not provided"
    except Exception:
        return "blind: holders/top10 not provided"
    if holders < CFG["min_holders"]:
        return f"holders below minimum ({holders:.0f} < {CFG['min_holders']})"
    if top10 >= CFG["max_top10_pct"]:
        return f"top-10 too high ({top10:.2f}% >= {CFG['max_top10_pct']}%)"
    if live["liq"] < CFG["min_liquidity_usd"]:
        return f"liquidity below minimum (${live['liq']:,.0f} < ${CFG['min_liquidity_usd']:,.0f})"
    try:
        sp = float(row.get("scout_price", ""))
        if not _is_valid_num(sp) or sp <= 0:
            return "blind: no Scout price to compare"
        diff_scout = abs(live["price"] - sp) / sp * 100.0
        if diff_scout > CFG["max_price_mismatch_pct"]:
            return f"live price differs from Scout snapshot ({diff_scout:.2f}% > {CFG['max_price_mismatch_pct']}%)"
    except Exception:
        return "blind: no Scout price to compare"
    return None


def open_position(s, row, live, pf=None, verdict_info=None):
    entry = live["price"]
    if pf and _is_valid_num(pf.get("pf_price")) and pf["pf_price"] > 0:
        pf_price = pf["pf_price"]
        pf_mcap = pf.get("pf_mcap", "blind")
        pf_vs_dex_pct = round(abs(pf_price - entry) / entry * 100.0, 4)
    else:
        pf_price = "blind"
        pf_mcap = "blind"
        pf_vs_dex_pct = "blind"
    pos = {
        "address": row["address"].strip(),
        "name": row["name"].strip(),
        "entry": entry,
        "tp": entry * (1 + CFG["take_profit_pct"] / 100.0),
        "sl": entry * (1 - CFG["stop_loss_pct"] / 100.0),
        "opened": now().isoformat(timespec="seconds"),
        "deadline": (now() + timedelta(hours=CFG["time_exit_hours"])).isoformat(timespec="seconds"),
        "liq_at_entry": round(live["liq"], 2),
        "dex_pair": live.get("pair", ""),
        "holders": float(row["holders"]),
        "top10_pct": float(row["top10_pct"]),
        "scout_price": float(row["scout_price"]),
        "pf_price": pf_price,
        "pf_mcap": pf_mcap,
        "pf_vs_dex_pct": pf_vs_dex_pct,
        "auditor_verdict": (verdict_info or {}).get("verdict", "blind"),
        "auditor_verdict_time": (verdict_info or {}).get("time", ""),
        "creator_history": "blind",
        "high_since_entry": entry,
        "low_since_entry": entry,
    }
    if CFG["mode"] == "LIVE":
        if pos["creator_history"] == "blind":
            skip(s, pos["address"], pos["name"], "blind: creator_history not allowed in LIVE mode")
            return
        print(f"TRADE SHEET (you sign this yourself in Jupiter — not auto-opened): {pos}")
        return
    s["open"].append(pos)
    if alerts is not None:
        _safe_alert("open", alerts.alert_open, pos, lane=LANE)


def close_position(s, pos, price, reason):
    closed_at = now()
    gross = CFG["position_size_usd"] * (price / pos["entry"] - 1.0)
    net = gross - CFG["roundtrip_fee_usd"]
    append(
        TRADES,
        {
            "name": pos["name"],
            "address": pos["address"],
            "opened": pos["opened"],
            "entry": pos["entry"],
            "tp": pos["tp"],
            "sl": pos["sl"],
            "liq_at_entry": pos["liq_at_entry"],
            "holders": pos.get("holders", ""),
            "top10_pct": pos.get("top10_pct", ""),
            "scout_price": pos.get("scout_price", ""),
            "pf_price": pos.get("pf_price", "blind"),
            "pf_mcap": pos.get("pf_mcap", "blind"),
            "pf_vs_dex_pct": pos.get("pf_vs_dex_pct", "blind"),
            "auditor_verdict": pos.get("auditor_verdict", "blind"),
            "auditor_verdict_time": pos.get("auditor_verdict_time", ""),
            "creator_history": pos.get("creator_history", "blind"),
            "high_since_entry": pos.get("high_since_entry", pos["entry"]),
            "low_since_entry": pos.get("low_since_entry", pos["entry"]),
            "closed": closed_at.isoformat(timespec="seconds"),
            "exit": price,
            "reason": reason,
            "gross_pnl": round(gross, 2),
            "net_pnl": round(net, 2),
        },
    )
    s["day_pnl"] = round(s["day_pnl"] + net, 4)
    cooldown_hrs = CFG.get("reentry_cooldown_hours", 24)
    if _is_valid_num(cooldown_hrs) and cooldown_hrs > 0:
        s.setdefault("cooldowns", {})[pos["address"]] = (
            closed_at + timedelta(hours=cooldown_hrs)
        ).isoformat(timespec="seconds")
    s["open"].remove(pos)
    if alerts is not None:
        _safe_alert(
            "close",
            alerts.alert_close,
            pos,
            price,
            reason,
            round(net, 2),
            s,
            trades_path=TRADES,
            target_closed=CFG["target_closed_trades"],
            lane=LANE,
        )


def rollover_day(s):
    today = str(now().date())
    if s.get("day") != today:
        if alerts is not None and s.get("day"):
            try:
                live_prices = {}
                for pos in s.get("open", []):
                    lp = fetch_price(pos["address"])
                    if lp and _is_valid_num(lp.get("price")):
                        live_prices[pos["address"]] = lp["price"]
                _safe_alert(
                    "daily_summary",
                    alerts.alert_daily_summary,
                    s,
                    trades_path=TRADES,
                    target_closed=CFG["target_closed_trades"],
                    position_size_usd=CFG["position_size_usd"],
                    roundtrip_fee_usd=CFG["roundtrip_fee_usd"],
                    live_prices=live_prices,
                    lane=LANE,
                )
            except Exception as e:
                print(f"[warn] daily summary hook failed: {type(e).__name__}", file=sys.stderr)
        s["day"] = today
        s["day_pnl"] = 0.0
        s["skipped_today"] = []
        s["loss_cap_alerted_day"] = ""


def manage(s):
    for pos in list(s["open"]):
        live = fetch_price(pos["address"])
        if not live:
            continue
        p = live["price"]
        pos["high_since_entry"] = max(pos.get("high_since_entry", p), p)
        pos["low_since_entry"] = min(pos.get("low_since_entry", p), p)
        if p >= pos["tp"]:
            close_position(s, pos, p, "take_profit")
        elif p <= pos["sl"]:
            close_position(s, pos, p, "stop_loss")
        elif now() >= datetime.fromisoformat(pos["deadline"]):
            close_position(s, pos, p, "time_exit")


def scan(s, watchlist_path=WATCHLIST, verdicts_path=VERDICTS):
    if count_closed_trades() >= CFG["target_closed_trades"]:
        print(f"Target reached ({CFG['target_closed_trades']} closed trades). Not opening new positions.")
        return
    if s["day_pnl"] <= -CFG["daily_loss_cap_usd"]:
        print(f"Daily loss cap hit (${s['day_pnl']:.2f} <= -${CFG['daily_loss_cap_usd']}). Skipping scan.")
        if alerts is not None and s.get("loss_cap_alerted_day") != s.get("day"):
            _safe_alert("daily_loss_cap", alerts.alert_daily_loss_cap, s["day_pnl"], CFG["daily_loss_cap_usd"], lane=LANE)
            s["loss_cap_alerted_day"] = s.get("day", "")
        return
    if not os.path.exists(watchlist_path):
        print(f"[info] {watchlist_path} not found yet; skipping scan.")
        return
    verdicts = load_verdicts(verdicts_path)
    if verdicts is None:
        print(f"[info] {verdicts_path} not found; skipping scan (fail-closed).")
        return
    held = {p["address"] for p in s["open"]}
    current_time = now()
    cooldowns = s.setdefault("cooldowns", {})
    for c_addr in list(cooldowns):
        try:
            if datetime.fromisoformat(cooldowns[c_addr]) <= current_time:
                del cooldowns[c_addr]
        except (TypeError, ValueError) as e:
            print(f"[warn] Cooldown parse error for {c_addr} ({cooldowns[c_addr]!r}): {e} — keeping coin blocked", file=sys.stderr)
    with open(watchlist_path, "r", newline="") as f:
        for row in csv.DictReader(f):
            if len(s["open"]) >= CFG["max_open_positions"]:
                break
            addr = (row.get("address") or "").strip()
            name = (row.get("name") or addr[:8]).strip()
            if not addr or addr in held:
                continue
            until_str = cooldowns.get(addr)
            if until_str:
                skip(s, addr, name, f"re-entry cooldown active until {until_str}")
                continue
            v = verdicts.get(addr)
            if not v:
                skip(s, addr, name, "blind: no Auditor verdict")
                continue
            if v["verdict"] != "ENTRY OK":
                skip(s, addr, name, f"auditor verdict: {v['verdict']}")
                continue
            live = fetch_price(addr)
            if not live:
                skip(s, addr, name, "blind: no live price")
                continue
            why = passes_filters(row, live)
            if why:
                skip(s, addr, name, why)
                continue
            pf = fetch_pumpfun_price(addr)
            open_position(s, row, live, pf=pf, verdict_info=v)
            held.add(addr)


def report():
    if not os.path.exists(TRADES):
        print("No closed trades yet.")
        return
    with open(TRADES, "r", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No closed trades yet.")
        return
    pnl = [float(r["net_pnl"]) for r in rows]
    wins = [x for x in pnl if x > 0]
    top3 = sum(sorted(wins, reverse=True)[:3]) if len(wins) >= 3 else sum(wins)
    print(
        f"Closed: {len(rows)}/{CFG['target_closed_trades']} | Win rate: {len(wins)/len(rows):.0%} | "
        f"Net P/L: {sum(pnl):.2f} | Without top 3 winners: {sum(pnl)-top3:.2f}"
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        return report()
    once = "--once" in sys.argv
    while True:
        s = load_state()
        rollover_day(s)
        manage(s)
        scan(s)
        save_state(s)
        report()
        if once:
            break
        time.sleep(CFG["poll_seconds"])


if __name__ == "__main__":
    main()
