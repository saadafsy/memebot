# Live Executor Interface Contract (`live_trader.py` <-> `bot.py`)

Source of truth: `/workspace/sim-bot/bot.py` (`sha256: 256ba30653b5118c0b01581575f65b6661ff766a0d8c31db9cd55b1413a01887`).
Security boundary: `bot.py` contains **zero** wallet, signing, private-key, or API-key code. `live_trader.py` is built separately on Jawad's own machine and plugs in at two exact boundaries.

---

## 1. Entry Boundary: `open_position(s, row, live, pf=None, verdict_info=None)`

**Location in `bot.py`:** lines 198–236

### Inputs
- `s` (`dict`): loaded `state.json` object (`{"open": [...], "day": "YYYY-MM-DD", "day_pnl": float, "skipped_today": [...]}`).
- `row` (`dict`): row from `watchlist.csv` with keys: `address`, `name`, `holders`, `top10_pct`, `scout_price`.
- `live` (`dict`): highest-liquidity Solana pair from DexScreener:
  - `price` (`float`, USD)
  - `liq` (`float`, USD liquidity)
  - `pair` (`str`, pairAddress)
  - `dex` (`str`, dexId)
- `pf` (`dict | None`): optional pump.fun HTML audit snapshot (`{"pf_price": float, "pf_mcap": float | "blind"}`). Non-blocking audit field only.
- `verdict_info` (`dict | None`): latest row from `auditor_verdicts.csv` (`{"verdict": "ENTRY OK", "time": str}`).

### Position Object (`pos`) Constructed by `open_position`
```python
pos = {
    "address": str,               # Solana token mint
    "name": str,                  # Token symbol/name
    "entry": float,               # Entry price USD (live["price"] in SIM; actual fill price in LIVE)
    "tp": float,                  # entry * (1 + CFG["take_profit_pct"] / 100.0)
    "sl": float,                  # entry * (1 - CFG["stop_loss_pct"] / 100.0)
    "opened": str,                # ISO-8601 timestamp (timespec="seconds")
    "deadline": str,              # ISO-8601 timestamp = opened + time_exit_hours
    "liq_at_entry": float,        # round(live["liq"], 2)
    "dex_pair": str,              # live.get("pair", "")
    "holders": float,             # from watchlist.csv
    "top10_pct": float,           # from watchlist.csv
    "scout_price": float,         # from watchlist.csv
    "pf_price": float | "blind",
    "pf_mcap": float | "blind",
    "pf_vs_dex_pct": float | "blind",
    "auditor_verdict": str,       # "ENTRY OK" (required before entry)
    "auditor_verdict_time": str,
    "creator_history": str,       # "blind" in SIM; MUST NOT be "blind" in LIVE
    "high_since_entry": float,    # initialized to entry
    "low_since_entry": float,     # initialized to entry
}
```

### Hard Invariants for `LIVE` Mode
1. **No blind creator history in LIVE:** `if pos["creator_history"] == "blind"` -> skip and log; **never** buy.
2. **Pre-buy sell-route check:** Before every real buy, confirm a sell route exists via Jupiter Swap V2 quote/order. If no sell route exists, skip and log to `skipped.csv`.
3. **Environment variables only:** Private key and Jupiter API key must be read strictly from environment variables — never stored in `settings.json`, `state.json`, CSVs, or logs, and never printed.
4. **Record actual fills & tx signature:** In `LIVE` mode, set `entry` to the actual executed fill price (and recompute `tp`/`sl` from actual fill), store `buy_tx_sig` and actual token amount acquired, and append `pos` to `s["open"]` **only after** on-chain confirmation.

---

## 2. Exit Boundary: `close_position(s, pos, price, reason)`

**Location in `bot.py`:** lines 239–271

### Inputs
- `s` (`dict`): loaded `state.json` object.
- `pos` (`dict`): an open position dict from `s["open"]`.
- `price` (`float`): trigger price USD (DexScreener live price in SIM; actual realized exit fill price in LIVE).
- `reason` (`str`): one of `"take_profit"`, `"stop_loss"`, `"time_exit"`.

### Required Side Effects
1. In `LIVE` mode: execute sell via Jupiter Swap V2 (get order, sign, execute), wait for confirmation, log `sell_tx_sig` and actual realized fill price + actual fees.
2. Compute P/L (fee deducted **before** rounding):
   - `gross = CFG["position_size_usd"] * (actual_exit_price / pos["entry"] - 1.0)`
   - `net = gross - actual_roundtrip_fee_usd` (or `CFG["roundtrip_fee_usd"]` in SIM)
   - Write `gross_pnl = round(gross, 2)` and `net_pnl = round(net, 2)` to `trades.csv`.
3. Update `s["day_pnl"] = round(s["day_pnl"] + net, 4)` and remove `pos` from `s["open"]`.

---

## 3. Loop Timing Note for `LIVE` Mode
- Paper bot polls every `300` seconds (`poll_seconds: 300`), so paper stop-loss fills are optimistic.
- `live_trader.py` should use faster exit monitoring (e.g., 5–15 seconds or WebSocket price feed) while respecting API rate limits.
