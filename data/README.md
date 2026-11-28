# research-data: memecoin collection run, 2026-11-27/28 (STOPPED)

**Status: stopped at 03:27 UTC on 2026-11-28 at the owner's request. No jobs or scheduled check-ins are running.**
This branch holds data only. The code lives on `main` (`research/collect.py`, `backtest.py`, `run_backtest.py`, `strategies.py`; last commit used: `ee1fc94`).

## Bottom line
- **No strategy passed.** The pass rule is: TEST mean > 0, Holm-corrected bootstrap p < 0.05, still positive at 2x costs, and enough TEST trades.
- **Buying fresh pump.fun launches loses heavily.** In the unbiased forward sample, random entries on pump.fun bonding curves averaged **-$10.63 per $20 trade (about -53%)** across 283 pools. 271 of those 283 pools died: their candles stop and liquidity collapses.
- The best-looking rules (buy and hold, +150% take-profit) only appear in the **survivorship-biased** backfill. They rest on 7-13 test trades, so treat them as unproven.
- All results are EXPLORATORY: about 6.5 h of forward data. A pass would only justify forward paper trading.

## Files
| File | What it is | Bias |
|---|---|---|
| `mbdata-final-partial.tgz` | **The final dataset at stop time.** Forward collection of 1,503 new Solana pools (GeckoTerminal `new_pools`, 20:56-22:57 UTC), DexScreener snapshots, and 1-min candles for a **seeded random 367 of them** (Random(42) shuffle, so they are an unbiased sample of the window). Also contains the 4 stop-time backtest summaries, the backfill summaries and `dq_final.json` | survivorship-free |
| `mbdata-partial.tgz` | Same forward data, as last pushed by the running collector | survivorship-free |
| `mbdata-early.tgz` | Early look: 109 pools created before 21:00, with candles and summaries | survivorship-free, tiny |
| `mbdata-backfill.tgz` | 239 older PumpSwap/Raydium pools, 107 with candles | **SURVIVORSHIP-BIASED** |
| `mbdata-pumpportal.tgz` | PumpPortal pump.fun `create`/`migrate` events, 22:15-03:27 UTC (about 1,800 launches). One JSON per line; `recv_ts` = unix seconds; lines with only a `message` key are acks | partial coverage (see below) |
| `mb-parquet.tgz`, `MANIFEST.json` | All of the above validated, deduped and in Parquet (DuckDB-queryable), with row counts and hashes | derived |
| `status.json` | Last health snapshot | |

## Results at stop time (forward sample, 367 random pools with candles, 27,874 variants)
| Universe | Pools | Dead | Candidates | PBO | Random-entry mean per $20 | Passed |
|---|---|---|---|---|---|---|
| pump-fun curves | 283 | 271 | 1,267 | 0.31 | **-$10.63** | none |
| meteora/raydium | 65 | 34 | 0 | 0.21 | -$3.17 | none |
| pumpswap | 14 | 7 | 0 | n/a | *invalid (bad candle data, see bug 1)* | none |
| all venues | 367 | 316 | 1,267 | 0.36 | *invalid (bug 1)* | none |

The top pump-fun candidates (`early(...)` entries with a 20% stop and a 5-min exit) lost money in TEST (-$7.33 per trade, n=44).

**Backfill (SURVIVORSHIP-BIASED, 91 pools at its last run):** 3,982 candidates, PBO 0.30, random entries +$0.90 per trade, none passed. The best was `baseline(0,20000)` with `tp1.5_sl0.4_t4320` (TEST +$12.32 over only 7 trades). The positive random control is the survivor effect.

## Data quality
- Forward: 1,503 pools seen (pump-fun 1,187, meteora-damm-v2 140, meteora-dbc 70, pumpswap 63, raydium-launchlab 30, others). 367 have candles. Candles per pool: median 3, p10 1, p90 12. Candle span 20:55-03:22 UTC.
- Most fresh pump.fun coins trade for only a few minutes. This is real: DexScreener buy counts confirm it.
- **PumpPortal's free stream is not a complete record of launches.** From 22:14 to 03:27 it saw 1,827 creates. Over the same window GeckoTerminal listed 568 pump.fun pools, and only 90 mints appear in both. Neither source is complete; to get every launch, record it on-chain.
- Collection gaps, when the container rebooted on idle: about 21:38-41, ~21:46-53, ~22:00-05 and 22:10-12. Candle fetching also lost time around 01:57-02:20.

## Known bugs / issues (for whoever picks this up)
1. **Bad GeckoTerminal candles break the P/L numbers.** Some candles carry absurd wicks: one pumpswap pool has a high of 32,850 against a 4.7e-6 open, and another has a 199 high against a 4.5e-7 open. That is why the pumpswap and all-venues random control reads about -$1e8. **Fix in `backtest.py`:** drop or clamp candles whose high/low is more than ~20x the max/min of their open/close, or whose price jumps more than 100x from the neighbouring candles. Then re-run.
2. The CoinGecko key never reached requests: `/api/v3/key` returned 401 all along. Either the network secret was not saved for this environment, or only a new session picks it up. Keyless CoinGecko on-chain allows only 2 calls/min.
3. GeckoTerminal keyless really allows about 3-5 calls/min, not 30. That is the bottleneck.

## Next steps (if work resumes)
1. Fix bug 1 (the candle outlier filter) on `main`. Re-run `python -m research.run_backtest --data <mbdata_final> --pp <mbdata_pp>` for the 4 universes.
2. Finish fetching candles for the remaining ~1,136 forward pools. Use a working CoinGecko Demo key, set up as a network secret on host `api.coingecko.com` with header `x-cg-demo-api-key`, in a NEW session with `CG_VIA_PROXY=1`. At about 30 calls/min this takes about 1 h. Resume with `python research/collect.py --ohlcv-only --out <dir>` on `mbdata-final-partial.tgz`; it skips the pools that already have candles.
3. More data before believing any result: run several more 2-hour forward windows at different times of day, aiming for 1,000+ pools with candles per venue.
4. For a complete list of pump.fun launches: record creates on-chain (a Helius/Shyft stream, or Bitquery/Dune history) rather than relying on PumpPortal's free stream.
5. Only if a strategy passes on fresh, unbiased data: forward paper trading with the bot in SIM or SHEET mode before any real money.
