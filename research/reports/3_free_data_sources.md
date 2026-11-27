# Free Solana memecoin history sources - notes (2026-11-27)
Verification method: WebSearch summaries of official docs only. WebFetch and curl to geckoterminal/coingecko docs were blocked in this research sandbox.
- GT public: base https://api.geckoterminal.com/api/v2, no key. 30/min per GT FAQ; newer CoinGecko keyless doc says ~10/min (dynamic). 429 -> back off.
- GT new_pools: 20/page, max 10 pages free (about 200 pools), only past 48h. That is a rolling window, so the collector must run continuously.
- GT ohlcv: limit max 1000 (default 100), before_timestamp in seconds, 6-month max range per call.
- GT trades: last 300 trades within 24h; trade_volume_in_usd_greater_than filter. Larger windows and cursors need a paid plan.
- DexScreener: profiles 60/min, pairs 300/min; no OHLCV.
- Dune: free accounts are view-only after a 14-day trial. The API and CSV export need a trial or a paid plan.
- Birdeye Standard: $0, 30k CU/month, 1 rps; /defi/ohlcv costs 40 CU; v3 caps at 5000 candles per call.
- Solana Tracker free: 2,500 requests/month, 3 rps; /chart/{token}[/{pool}].
- Helius free: 1M credits, 10 rps RPC; archival getSignaturesForAddress included.
- Bitquery: 1K-10K trial points; about 30 days of history in the realtime cube.
- Flipside free: 500 API query-seconds/month; possible sale to SonarX (unverified).
