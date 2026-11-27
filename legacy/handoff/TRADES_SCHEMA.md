# Data & Log File Schemas (`/workspace/sim-bot/`)

## 1. `trades.csv` (23 columns in SIM; append extra LIVE columns at the end)

| # | Column | Type | Description |
|---|--------|------|-------------|
| 1 | `name` | string | Token symbol/name |
| 2 | `address` | string | Solana token mint address |
| 3 | `opened` | ISO-8601 | Entry timestamp (`YYYY-MM-DDTHH:MM:SS`, box-local America/Toronto) |
| 4 | `entry` | float | Entry price in USD |
| 5 | `tp` | float | Take-profit trigger price (`entry * (1 + take_profit_pct/100)`) |
| 6 | `sl` | float | Stop-loss trigger price (`entry * (1 - stop_loss_pct/100)`) |
| 7 | `liq_at_entry` | float | DexScreener pool liquidity USD at entry (rounded to 2 decimals) |
| 8 | `holders` | float | Holder count from Scout/watchlist |
| 9 | `top10_pct` | float | Top-10 holder concentration (%) |
| 10 | `scout_price` | float | Scout snapshot price USD |
| 11 | `pf_price` | float or `"blind"` | pump.fun HTML price USD at entry (audit field) |
| 12 | `pf_mcap` | float or `"blind"` | pump.fun HTML market cap USD at entry (audit field) |
| 13 | `pf_vs_dex_pct` | float or `"blind"` | Absolute % diff between pump.fun and DexScreener price |
| 14 | `auditor_verdict` | string | Must be `"ENTRY OK"` |
| 15 | `auditor_verdict_time` | string | Timestamp of Auditor verdict |
| 16 | `creator_history` | string | `"blind"` in SIM; verified status required in LIVE |
| 17 | `high_since_entry` | float | Highest observed price while open |
| 18 | `low_since_entry` | float | Lowest observed price while open |
| 19 | `closed` | ISO-8601 | Exit timestamp (`YYYY-MM-DDTHH:MM:SS`) |
| 20 | `exit` | float | Exit price in USD |
| 21 | `reason` | string | `"take_profit"`, `"stop_loss"`, or `"time_exit"` |
| 22 | `gross_pnl` | float | `round(position_size_usd * (exit / entry - 1.0), 2)` |
| 23 | `net_pnl` | float | `round(gross - roundtrip_fee_usd, 2)` (fee subtracted before rounding) |

**Recommended additional columns for `LIVE` mode** (bot's `append()` auto-migrates new columns safely via atomic temp-file rewrite):
- `buy_tx_sig`, `sell_tx_sig`, `actual_entry_price`, `actual_exit_price`, `actual_fee_usd`, `slippage_bps_used`

## 2. `skipped.csv`
Columns: `time`, `address`, `name`, `reason`

## 3. `watchlist.csv`
Columns: `name`, `address`, `holders`, `top10_pct`, `scout_price`

## 4. `auditor_verdicts.csv`
Columns: `time`, `address`, `name`, `verdict`, `notes`
(`bot.py` uses the latest row per `address`, case-normalized; fail-closed if file missing or verdict != `ENTRY OK`.)

## 5. `state.json`
Keys: `open` (list of open `pos` dicts), `day` (`YYYY-MM-DD`), `day_pnl` (`float`), `skipped_today` (list of `address|reason` strings).
