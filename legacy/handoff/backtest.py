"""
Read-only historical backtester for Trader Joe SIM rules.
Fetches real 1-hour OHLCV candles from public GeckoTerminal API for the 5 screened
Solana pools in /workspace/sim-bot/state.json and replays sequential non-overlapping
trades with a 24h post-exit cooldown under exact SIM rules:
  - $20 position size, +30% TP, -15% SL, $1.80 round-trip fee, 24h max hold.
  - Conservative intrabar rule: if a 1h candle touches both SL and TP, SL wins.
Never modifies anything in /workspace/sim-bot/.
"""
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone

SIZE = 20.0
TP_MULT = 1.30
SL_MULT = 0.85
FEE = 1.80
HOLD_HOURS = 24
COOLDOWN_HOURS = 24
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TraderJoeBacktest/1.0)", "Accept": "application/json"}


def fetch_hourly_ohlcv(pool_addr, limit=1000):
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit={limit}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    raw = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    # API returns newest-first: [ts, open, high, low, close, volume]
    candles = sorted(raw, key=lambda c: c[0])
    return candles


def simulate_pool(name, mint, pool, candles):
    trades = []
    i = 0
    n = len(candles)
    while i < n - 1:
        ts_open, o, h, l, c, vol = candles[i]
        if o <= 0:
            i += 1
            continue
        entry = o
        tp = entry * TP_MULT
        sl = entry * SL_MULT
        exit_idx = None
        exit_price = None
        reason = None

        # Scan up to HOLD_HOURS candles starting at entry candle i
        end_idx = min(i + HOLD_HOURS, n)
        for j in range(i, end_idx):
            _, _, ch, cl, cc, _ = candles[j]
            # Conservative tie-breaker: SL checked before TP if both hit in same 1h bar
            if cl <= sl:
                exit_idx = j
                exit_price = sl
                reason = "stop_loss"
                break
            if ch >= tp:
                exit_idx = j
                exit_price = tp
                reason = "take_profit"
                break

        if exit_idx is None:
            exit_idx = end_idx - 1
            exit_price = candles[exit_idx][4]  # close of final bar
            reason = "time_exit"

        gross = SIZE * (exit_price / entry - 1.0)
        net = gross - FEE
        trades.append({
            "name": name,
            "address": mint,
            "pool": pool,
            "opened_utc": datetime.fromtimestamp(candles[i][0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "closed_utc": datetime.fromtimestamp(candles[exit_idx][0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry": round(entry, 10),
            "exit": round(exit_price, 10),
            "reason": reason,
            "gross_pnl": round(gross, 2),
            "net_pnl": round(net, 2),
        })
        # Advance past exit + cooldown
        i = exit_idx + 1 + COOLDOWN_HOURS
    return trades


def main():
    with open("/workspace/sim-bot/state.json", "r") as f:
        state = json.load(f)
    all_trades = []
    per_coin = {}
    for pos in state["open"]:
        name = pos["name"]
        mint = pos["address"]
        pool = pos["dex_pair"]
        candles = fetch_hourly_ohlcv(pool, limit=1000)
        t = simulate_pool(name, mint, pool, candles)
        all_trades.extend(t)
        pnl_c = [x["net_pnl"] for x in t]
        wins_c = [x for x in pnl_c if x > 0]
        per_coin[name] = {
            "candles": len(candles),
            "trades": len(t),
            "wins": len(wins_c),
            "win_rate_pct": round(100.0 * len(wins_c) / len(t), 1) if t else 0.0,
            "net_pnl": round(sum(pnl_c), 2),
        }
        time.sleep(0.3)

    with open("/workspace/handoff/backtest_results.csv", "w", newline="") as f:
        if all_trades:
            w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            w.writeheader()
            w.writerows(all_trades)

    pnls = [x["net_pnl"] for x in all_trades]
    wins = [x for x in pnls if x > 0]
    top3 = sum(sorted(wins, reverse=True)[:3]) if len(wins) >= 3 else sum(wins)
    reasons = {}
    for x in all_trades:
        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1

    summary = {
        "total_trades": len(all_trades),
        "wins": len(wins),
        "win_rate_pct": round(100.0 * len(wins) / len(all_trades), 1) if all_trades else 0.0,
        "total_gross_pnl": round(sum(x["gross_pnl"] for x in all_trades), 2),
        "total_fees": round(len(all_trades) * FEE, 2),
        "total_net_pnl": round(sum(pnls), 2),
        "net_pnl_without_top3": round(sum(pnls) - top3, 2),
        "reasons": reasons,
        "per_coin": per_coin,
    }
    with open("/workspace/handoff/backtest_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
