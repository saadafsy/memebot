# Build report (Nov 27, 2026)

For Saad and Jawad. What was built, what was checked and how, what was not tested, and the risks.

## Bottom line

* The bot works end to end in **paper mode** on real market data, and is safe to run 24/7 that way.
* **Real-money mode is built but has never sent a real transaction.** No money was spent, as Jawad
  asked. Everything up to signing was checked against Jupiter's live API; the signing and the
  wallet logic were tested with a fake wallet.
* **The strategy has not shown an edge.** Jawad's own backtest: 100 trades, 8 wins, -$17.71
  before fees and -$197.71 after. 85 of the 100 ended at the 24-hour time exit. Paper-trade it to
  the 100-trade target before risking anything, and treat any real money as money you can lose.

## What Jawad asked for, and status

| Ask (source) | Status |
|---|---|
| Bot that trades for him, controlled from Telegram (Snapchat) | Done: paper always on; real money separate, armed only on the computer |
| Live executor on Jupiter Swap V2: order, sign, execute; keys from env; sell route before every buy; slippage and fees from settings; log signatures and fills (CLAUDE_CODE_PROMPT.md) | Built and mock-tested; live API shapes verified; **no real transaction sent**. `live_trader.py` exposes `buy()`/`sell()` with the exact return dicts from his INTERFACE_CONTRACT.md |
| Test on tiny amounts first | Left to Jawad (no money spent by request) |
| Prompt A: print-only trade sheet with tests | Done: SHEET mode, verified on 10 real coins |
| Prompt B: Start/Stop app, status, recent trades, read-only settings, localhost, README | Done (FastAPI rather than Flask, so one program runs everything) |
| Prompt C: /status /positions /pnl /closed, inline buttons, only his chat | Done; message delivery to Telegram not tested from the cloud |
| Email: phone app (PWA), password before exposing it | Done |
| Doc answers: paper always shadows; real is his own switch, only after 100 paper trades; Approve taps; key never in a file; check the $1.80; keep $1.80 and log the quoted cost beside it | All done |

## How it was checked

* **107 automated tests** (`python -m pytest -q`), plus **Jawad's own 14 tests**, unchanged, against
  his untouched `bot.py` (`python legacy/run_legacy_tests.py`). His original files are byte-identical
  to his manifest.
* **Bug reproduction:** tests show 5 of the bot.py bugs reported earlier happen in his original file
  (crash on a dropped connection, positions never time-exiting without a price, reading SOL's price
  for the coin, skip log growing every scan, timezone-less deadlines). The other two (repeated trade
  sheets, 5-minute exit checks) are visible in the code and settings.
* **Independent code review** (a separate reviewer, not the author): 10 real issues found, all
  fixed with a regression test each. The worst: a timed-out real buy could be bought twice; a fast
  rug pull could block the stop-loss; a trades.csv open in Excel could leave a sold position stuck.
* **Live read-only verification** (a separate session with read-only network access, no wallet, no
  keys): Jupiter Swap V2, price and token APIs, DexScreener and RugCheck match the code. End-to-end
  run on 10 real coins in paper and trade-sheet modes, plus the panel, with no errors. Its 6 smaller
  findings were fixed.

## The 7 bot.py bugs: fixed, and the test for each

Each row has a test proving the bug exists in Jawad's untouched `legacy/sim-bot/bot.py`
(`tests/test_legacy_bugs.py`) where it can be reproduced in code, and a test proving the new bot
handles it.

| # | bot.py bug | Shown in the original by | Fixed and tested by |
|---|---|---|---|
| 1 | A dropped connection (reset / remote disconnect) crashes the bot; the loop has no error handling | `test_bug1_connection_reset_escapes_fetch_price` | `test_network_down_never_raises_raw_exception`, `test_retry_on_429_with_retry_after_and_reset`, `test_loop_survives_exceptions` |
| 2 | No price means the 24 h time exit never fires; the position stays open forever | `test_bug2_time_exit_never_fires_without_price` | `test_bug2_time_exit_fires_even_without_price`, `test_bug2_time_exit_waits_for_grace_when_unpriced` |
| 3 | Pairs where the coin is the quote token are used, so it can read another coin's price | `test_bug3_quote_token_pair_price_used` | `test_best_pairs_requires_base_token` |
| 4 | LIVE reprints the same trade sheet every scan | visible in `open_position()`: it prints and returns with no record | `test_bug4_sheet_printed_once_per_day`, `test_sheet_blocked_verdict_gets_no_sign_alert` |
| 5 | Skip reasons with changing numbers break the once-a-day dedupe; skipped.csv grows every scan | `test_bug5_skip_dedupe_breaks_on_changing_numbers` | `test_bug5_skip_dedupe_ignores_changing_numbers` |
| 6 | Naive local timestamps, so deadlines shift across a daylight-saving change | `test_bug6_naive_timestamps` | `test_naive_legacy_timestamps_and_dst`, `test_legacy_state_with_naive_timestamps` |
| 7 | Exits checked only every 300 s, so paper stop-loss fills look better than real ones | `poll_seconds: 300` drives both scans and exits in `main()` | `test_bug7_exits_checked_every_exit_poll_not_every_scan` (exits every 15 s, scans every 300 s) |

## Carrying on the existing paper runs

The new bot does not have to start at 0/100. Stop the old bot, then:

```bash
python -m memebot import --from /path/to/sim-bot  --data data        # MAIN SIM
python -m memebot import --from /path/to/sim-fast --data data-fast   # FAST LANE (4 h exit)
```

This copies settings.json, state.json (open positions, cooldowns, today's P/L), trades.csv, skipped.csv,
watchlist.csv, auditor_verdicts.csv, alerts.log and audit_log.csv. The source folders are only read.
It refuses to overwrite a folder that already has trades or state, and refuses a corrupt state.json.
Old timestamps without a timezone are read as Toronto time. Tested in `tests/test_import.py`.

## The $1.80 fee

Measured with live Jupiter quotes on 10 real coins ($21k to $300k liquidity): a $20 round trip costs
$0.15 to $0.61 in quotes, about **$0.45 to $0.90 all-in** with network fees and token-account rent
(2% to 4.5%). $1.80 (9%) is 2 to 4 times too high, but works as a safety buffer. Per Jawad, $1.80 stays
for the current run, and every paper trade also logs `quoted_fee_usd` and `net_pnl_quoted` so both can
be compared. The backtest loses money before any fee, so fees are not the main problem.
The raw quotes (amounts, price impact, fee bps, router and request id for each coin, both directions)
are saved in `research/fee_quotes_2026-11-27.json`, with a readable table in
`research/fee_check_2026-11-27.md`.

## Not tested

* Signing and sending a real transaction, and the real wallet reads (RPC). Tested with fakes only.
* Telegram delivery and buttons against Telegram's servers (the cloud build machine can't reach them).
  The command handlers, the chat-ID filter, message splitting and HTML escaping are unit-tested.
* Windows: start-windows.bat and the code were written for Windows, but nothing was run on Windows.
* The phone app on a real phone.

## Risks with real money

* Memecoins can drop 90% in seconds or become unsellable (no buyers, a freeze authority, a honeypot).
  The bot checks a sell route and RugCheck before buying, but neither can see the future.
* Stop-losses are not guaranteed fills. In a crash the sell can fill far below the stop.
* A compromised computer or Telegram account could press Approve. Use a separate wallet holding only
  the budget, a strong panel password, and Telegram two-step verification.
* Keep `live_require_approval` on until the bot has a long, positive paper record.

## First real test (when Jawad decides)

1. Let the paper run reach 100 closed trades and check `net_pnl` and `net_pnl_quoted`.
2. Make a brand-new wallet; put in only a few dollars of SOL.
3. Set `"position_size_usd": 2` (or similar), `"mode": "LIVE"`, the Jupiter key and RPC in `.env`, the
   acknowledgement, and type the wallet key into the terminal (README, "Real money").
4. `/liveon`, approve one buy, watch it on Solscan, let it exit, and compare `data/live/trades.csv` with
   the wallet history.
