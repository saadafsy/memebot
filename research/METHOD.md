# Backtest method

How the strategy matrix is tested, and why each rule is there. Code: `collect.py`, `backtest.py`,
`strategies.py`, `run_backtest.py`. Engine tests: `tests/test_backtest.py`.

## Data

* **Forward collection.** `collect.py` records every new Solana pool from GeckoTerminal `new_pools`
  as it appears, including pools that die minutes later, snapshots DexScreener liquidity and
  buy/sell counts every few minutes, lets the pools age, then fetches 1-minute candles. Lists of pools
  that exist *today* only contain survivors, which is the most common way memecoin backtests lie.
* **Backfill sample.** Older PumpSwap/Raydium pools are fetched separately and labelled
  survivorship-biased; they are reported apart from the main results and never mixed in.
* **Limits.** About 2-5 hours of launches; the free API caps `new_pools` at the newest ~200 pools per
  poll, so this is a sample of launches, not all of them. Minutes with no trades have no candle and are
  treated as untradeable.

## Execution model (conservative on purpose)

* Signal on candle *t*'s close; fill at candle *t+1*'s open. No fill if the next candle is more than
  2 minutes later.
* Same-candle stop and target: the stop wins. A candle opening through the stop fills at the open.
* Trailing stops use only highs from earlier candles.
* A trade still open when the data ends exits at the last traded price (for a dead pool, the post-rug
  price). Nothing is dropped.
* Skip any trade larger than 1% of the pool's liquidity.
* Costs per side: 0.8% (pool fee + Jupiter fee) + size/liquidity impact; +1% extra on stop, trail and
  data-end exits; $0.06 network fees per round trip. This is calibrated on the measured Jupiter quotes
  (mean 2.0% round trip). Every result is also run at 2x costs.

## Selection (no peeking)

1. Pools sorted by creation time: first 60% = TRAIN, last 40% = TEST.
2. One entry per pool per strategy.
3. Candidates: TRAIN trades ≥ 30, ranked by TRAIN t-statistic; the top 25 go to TEST.
4. A strategy **passes** only if, on TEST:
   * at least 30 trades and mean P/L > 0;
   * bootstrap one-sided p-value Holm-corrected across the 25 candidates < 0.05;
   * mean P/L still > 0 at 2x costs;
   * mean P/L still > 0 after dropping the best 1% of trades;
   * it beats at least 95% of 20 random-entry seeds that use the **same exit**.
5. The Probability of Backtest Overfitting (CSCV over 8 time blocks) is reported for the whole matrix.
   PBO near 0.5 or above means in-sample ranking says nothing about out-of-sample performance.

## What a pass would and would not mean

With a few hundred to a few thousand pools, a pass is a *candidate worth paper-trading forward*, not a
proven edge: the statistics report shows a +2% per-trade edge needs thousands of trades to confirm. The
only way to confirm one is the live paper bot (`paper_fee_mode` and `log_quoted_fee` already record the
real costs), run forward on coins it has never seen.
