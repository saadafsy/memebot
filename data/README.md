# research-data: memecoin collection run, 2026-11-27

Data only. No code lives on this branch. The code is on `main` (`research/collect.py`, `backtest.py`, `run_backtest.py`).
`status.json` and `MANIFEST.json` are regenerated on every push. This README was last written at 23:11 UTC.

## Files
| File | What it is | Bias |
|---|---|---|
| `mbdata-partial.tgz` | Forward collection: every new Solana pool from GeckoTerminal `new_pools` (`pools.jsonl`), DexScreener liquidity and buys/sells snapshots (`snapshots.jsonl`), then 1-min OHLCV (`ohlcv/`) after the pools have aged | survivorship-free (pools recorded at creation) |
| `mbdata-early.tgz` | Early look: the 109 forward pools created before 21:00 UTC, with candles, the 4 backtest summaries and `dq_early.json` | survivorship-free, tiny sample |
| `mbdata-backfill.tgz` | Older PumpSwap and Raydium pools from GeckoTerminal's dex pool lists, with their candles | **SURVIVORSHIP-BIASED** (only pools that still exist and rank today) |
| `mbdata-pumpportal.tgz` | PumpPortal websocket since 22:15 UTC: every pump.fun `create` (creator, initial SOL buy, bonding-curve state) and `migrate` event. Format: one JSON per line, `recv_ts` = unix seconds; lines with only a `message` key are subscription acks | free stream; trades need a funded key, so they are NOT included |
| `mb-parquet.tgz` | The same data validated, deduped (pool+timestamp, tx signature) and compacted to Parquet (zstd), queryable with DuckDB | derived |
| `MANIFEST.json` | row counts, sha256 hashes, code commit for each Parquet table | |
| `status.json` | live health check (PASS/FAIL), progress counters, GeckoTerminal limiter state | |

## Timeline (UTC)
- 20:56: forward collector started (2 h window, then 2 h for the youngest pools to age, then candle fetch).
- 21:3x-22:12: container reboots on every idle period killed all jobs several times, leaving gaps in new_pools coverage (21:38-41, ~21:46-53, ~22:00-05, 22:10-12). Fixed by a watchdog that keeps the session alive plus an idempotent relaunch.
- 22:15: PumpPortal recorder started.
- 22:52: early look re-run with no min-candle filter.
- 22:57: new-pool window closed (1,503 pools); now settling until about 00:56, then fetching candles.
- Final results are expected around 06:00 UTC on the free API, or about 1 h after candle fetching starts once a working CoinGecko key is available.

## What we learned
1. **GeckoTerminal keyless allows about 4-5 calls/min in practice**, not the 30/min in older docs. We saw frequent 429s with `Retry-After: 0` and no rate headers. All GeckoTerminal callers now share one cross-process limiter (file-locked token bucket, priorities, shared 429 backoff, AIMD, jitter).
2. **CoinGecko without a key is 2 calls/min** (`x-ratelimit-limit: 2`). A CoinGecko Demo key is free, but in this run it never reached requests: `/api/v3/key` returns 401. It has to be saved as a network secret (host `api.coingecko.com`, header `x-cg-demo-api-key`), and a new session picks it up.
3. **Most fresh pump.fun launches die within minutes.** In the early look, the 84 pump.fun curves had a median of 1-2 traded minutes and $144 total volume, confirmed by DexScreener buy counts. Graduated PumpSwap pools: median volume $223k.
4. **A min-candles filter reintroduces survivorship bias.** Fixed on main (ee1fc94): coins that die count as losses.
5. **The free PumpPortal stream is not a complete record of launches.** From 22:14 to 22:51 it saw 293 creates against 411 pump.fun pools from GeckoTerminal, with only 61 matching mints.

## Early look (109 pools, all EXPLORATORY, 27,874 variants, main ee1fc94)
| Universe | Pools | Died | Random-entry mean P/L per $20 | Candidates | Passed |
|---|---|---|---|---|---|
| all venues | 109 | 89 | -$0.95 | 0 | none |
| pumpswap | 5 | 0 | +$5.68 | 0 | none |
| pump-fun curves | 84 | 78 | **-$8.18 (-41%)** | 0 | none |
| meteora/raydium | 20 | 11 | +$0.71 | 0 | none |
No strategy reached the 30 training trades it needs. This is a pipeline test, not evidence either way.

## Current status
```
{
 "utc": "2026-11-27T23:11:21Z",
 "result": "PASS",
 "forward": {
  "pools": 1503,
  "snapshots": 9753,
  "ohlcv": 0,
  "snap_age_s": 64,
  "done": false
 },
 "backfill": {
  "pools": 239,
  "ohlcv": 63
 },
 "pumpportal": {
  "events": 491,
  "last_write_age_s": 9
 },
 "gt": {
  "interval_s": 17.2,
  "calls": 20,
  "n429": 5
 }
}
```
