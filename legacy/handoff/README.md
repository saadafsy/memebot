# Solana Memecoin SIM Bot — Architecture & Live-Executor Interface

This project is a strictly **paper-trading (`SIM`)** research bot for Solana memecoins (`pump.fun` / DexScreener).
It contains **zero wallet, private-key, seed-phrase, or transaction-signing code**.

After the SIM bot completes **100 closed paper trades** and the owner explicitly approves live trading in writing, a separate local executor (`live_trader.py`, built independently in Claude Code on the owner's machine) plugs into exactly two functions in `bot.py`: `open_position(...)` and `close_position(...)`.

---

## 1. Directory & File Map (`/workspace/sim-bot/`)

| File | Owner | Role |
|---|---|---|
| `settings.json` | Owner only | Fixed strategy parameters. **Never modified without owner's written approval.** |
| `bot.py` | Trader Joe | Core SIM runner (`--once` or polling loop; `report` subcommand). |
| `bot.py.orig` | Read-only backup | Verbatim original `bot.py` baseline. Never modified. |
| `watchlist.csv` | Scout | Candidate coins with verified `holders`, `top10_pct`, `scout_price`. |
| `auditor_verdicts.csv` | Auditor | Independent pre-entry gate (`address,verdict,time`). Stage 3 requires `ENTRY OK` (latest row per address wins; fail-closed if missing). |
| `state.json` | `bot.py` (atomic write) | Current open positions, daily P/L (`day_pnl`), and daily skip deduplication (`skipped_today`). |
| `trades.csv` | `bot.py` (append) | Closed paper trades with entry/exit prices, exit reason, gross P/L, and net P/L (after `$1.80` round-trip fee). |
| `skipped.csv` | `bot.py` (append) | Every rejected candidate with timestamp and exact skip reason. |
| `audit_log.csv` | Auditor | Independent audit trail of code reviews, entries, exits, and math checks. |
| `alerts.py` | Trader Joe | Optional Stage 5 Telegram alert module. Reads `ALERT_TELEGRAM_TOKEN` and `ALERT_TELEGRAM_CHAT_ID` from env vars only (always HTTPS, 5s timeout, never logs token/URL, wrapped in `_safe_alert` so alert errors never crash the loop). |
| `alerts.log` | `alerts.py` (append) | Local append-only log of all `OPEN`, `CLOSE`, `DAILY_SUMMARY`, and `DAILY_LOSS_CAP_HIT` alerts labeled by lane (`MAIN SIM` / `FAST LANE`). |

---

## 2. Configuration Schema (`settings.json`)

```json
{
  "mode": "SIM",
  "position_size_usd": 20,
  "take_profit_pct": 30,
  "stop_loss_pct": 15,
  "time_exit_hours": 24,
  "roundtrip_fee_usd": 1.8,
  "max_open_positions": 5,
  "daily_loss_cap_usd": 15,
  "min_liquidity_usd": 20000,
  "min_holders": 500,
  "max_top10_pct": 30,
  "max_price_mismatch_pct": 3,
  "poll_seconds": 300,
  "target_closed_trades": 100,
  "reentry_cooldown_hours": 24
}
```

---

## 3. Plug-In Interface for `live_trader.py`

All buy/sell execution is isolated in two functions in `bot.py` (Stage 3, currently live). In `SIM` mode they record paper positions and P/L. A future `live_trader.py` plugs in at these two exact boundaries.

### A. `open_position(s, row, live, pf=None, verdict_info=None)`

**Called by:** `scan(s)` only after ALL of the following pass:
1. Closed trade count `< target_closed_trades` (`100`).
2. `s["day_pnl"] > -daily_loss_cap_usd` (`-$15`).
3. Open positions `< max_open_positions` (`5`), `address` is not already open, and `address` is not on re-entry cooldown (`reentry_cooldown_hours`: `24` hours after close; malformed cooldown timestamps fail closed and stay blocked).
4. `auditor_verdicts.csv` latest verdict for `address` is `"ENTRY OK"` (Stage 3).
5. Live DexScreener Solana pair exists (`price > 0`, `liq >= min_liquidity_usd`).
6. `passes_filters(row, live)` returns `None` (`holders >= 500`, `top10_pct < 30`, `|live_price - scout_price| / scout_price <= 3%`).

**Inputs:**
- `s: dict` — mutable state loaded from `state.json` (`open`, `day`, `day_pnl`, `skipped_today`).
- `row: dict` — one `watchlist.csv` row (`address`, `name`, `holders`, `top10_pct`, `scout_price`).
- `live: dict` — best Solana DexScreener pair by liquidity:
  ```python
  {"price": float, "liq": float, "pair": str, "dex": str}
  ```
- `pf: dict | None` — optional `pump.fun/coin/<mint>` snapshot (Stage 2+):
  ```python
  {"pf_price": float, "pf_mcap": float | "blind"}
  ```
- `verdict_info: dict | None` — latest `auditor_verdicts.csv` entry (Stage 3+):
  ```python
  {"verdict": "ENTRY OK", "time": str}
  ```

**Outputs / Side Effects (SIM mode):**
Appends a `pos` dict to `s["open"]`:
```json
{
  "address": "string (Solana SPL mint)",
  "name": "string (token symbol/name)",
  "entry": 0.004764,
  "tp": 0.0061932,
  "sl": 0.0040494,
  "opened": "2026-11-27T12:57:46",
  "deadline": "2026-11-28T12:57:46",
  "liq_at_entry": 172069.18,
  "dex_pair": "string (Solana pool address)",
  "holders": 20150.0,
  "top10_pct": 5.16,
  "scout_price": 0.00475948,
  "pf_price": 0.0047626,
  "pf_mcap": 4702985.82,
  "pf_vs_dex_pct": 0.0294,
  "auditor_verdict": "ENTRY OK",
  "auditor_verdict_time": "2026-11-27 12:43:50 EST",
  "creator_history": "blind",
  "high_since_entry": 0.004764,
  "low_since_entry": 0.004764
}
```

**Live-Executor Plug-In Contract (for `live_trader.py`):**
- Input needed for a real buy: `pos["address"]` (mint), `CFG["position_size_usd"]`, `live["price"]` (reference quote), `CFG["max_price_mismatch_pct"]` (slippage/quote guard).
- Expected return from `live_trader.buy(...)` when plugged in later:
  `{"ok": bool, "fill_price": float, "tokens_received": float, "fee_usd": float, "tx_sig": str, "error": str | None}`
- If `ok` is `True`, recompute `tp` and `sl` from `fill_price`, store `tx_sig` / `tokens_received` / `entry_fee_usd` on `pos`, and append `pos` to `s["open"]`. If `False`, log to `skipped.csv` and do not open.
- **Safety gate:** `bot.py` blocks `LIVE` mode if `creator_history == "blind"`.

---

### B. `close_position(s, pos, price, reason)`

**Called by:** `manage(s)` when an open position meets one of three exit triggers:
- `price >= pos["tp"]` → `reason = "take_profit"`
- `price <= pos["sl"]` → `reason = "stop_loss"`
- `now() >= datetime.fromisoformat(pos["deadline"])` → `reason = "time_exit"`

**Inputs:**
- `s: dict` — mutable state (`state.json`).
- `pos: dict` — the open position dict from `s["open"]`.
- `price: float` — current live DexScreener USD price that triggered the exit.
- `reason: str` — one of `"take_profit"`, `"stop_loss"`, `"time_exit"`.

**Outputs / Side Effects (SIM mode):**
1. Computes:
   - `gross = position_size_usd * (price / pos["entry"] - 1.0)`
   - `net = gross - roundtrip_fee_usd`
   - Logged as `gross_pnl = round(gross, 2)` and `net_pnl = round(net, 2)` (fee deducted before rounding)
2. Appends one row to `trades.csv` with all entry snapshot fields plus `closed`, `exit`, `reason`, `gross_pnl`, `net_pnl`.
3. Updates `s["day_pnl"] = round(s["day_pnl"] + net, 4)` and removes `pos` from `s["open"]`.

**Live-Executor Plug-In Contract (for `live_trader.py`):**
- Input needed for a real sell: `pos["address"]` (mint), `pos["tokens_received"]`, `price` (reference quote), `reason`.
- Expected return from `live_trader.sell(...)` when plugged in later:
  `{"ok": bool, "fill_price": float, "usd_received": float, "fee_usd": float, "tx_sig": str, "error": str | None}`
- If `ok` is `True`, compute real `gross_pnl` / `net_pnl` from actual fill & fees, append to `trades.csv`, update `s["day_pnl"]`, and remove `pos` from `s["open"]`. If `False`, keep `pos` in `s["open"]` and alert/retry on next tick.

---

## 4. CSV Schemas

- **`watchlist.csv`:** `name,address,holders,top10_pct,scout_price` (extra columns ignored by `bot.py`).
- **`auditor_verdicts.csv`:** `address,verdict,time` (`verdict` is `ENTRY OK`, `ENTRY FAIL`, or `ENTRY BLIND`).
- **`skipped.csv`:** `time,address,name,reason`
- **`trades.csv` (Stage 3):** `name,address,opened,entry,tp,sl,liq_at_entry,holders,top10_pct,scout_price,pf_price,pf_mcap,pf_vs_dex_pct,auditor_verdict,auditor_verdict_time,creator_history,high_since_entry,low_since_entry,closed,exit,reason,gross_pnl,net_pnl`

---

## 5. Running & Testing

```bash
cd /workspace/sim-bot
python3 bot.py --once    # Run one manage + scan + report cycle
python3 bot.py report    # Print closed trade summary only
```

## 6. Hard Security Invariants
- Never commit, log, print, or store private keys, seed phrases, or API keys in any file in `/workspace/sim-bot/`.
- Keep all live signing and key custody strictly on the owner's local machine inside `live_trader.py`.
- Never alter `settings.json` or `bot.py` without explicit written approval from the owner.
---

## 7. Notes for the live executor

- The paper bot checks prices only every 300 seconds, so paper stop-loss fills are optimistic. A live executor needs faster exit checks.
- Before every real buy, confirm a sell route exists. If not, skip and log it.
