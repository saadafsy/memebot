# memebot

A Solana memecoin bot you control from Telegram and from a phone web app. It runs Jawad's
strategy rules (from his `sim-bot`) with three modes behind one switch in `settings.json`:

| Mode | What it does | Needs |
|---|---|---|
| **SIM** (default) | Paper trading only. Same rules, same files and the same P/L math as `bot.py`. | nothing |
| **SHEET** | Paper trading, plus a print-only **trade sheet** for each signal: a Jupiter quote both ways, all safety checks, and a link. You sign it yourself in Jupiter. The bot never holds a key. | nothing (a free `JUPITER_API_KEY` raises Jupiter's rate limit) |
| **LIVE** | Paper trading keeps running as a shadow, **plus** a separate real-money book that signs Jupiter Swap V2 trades with your wallet key. Real entries start **OFF**, and each real buy waits for your **Approve** tap in Telegram or the panel. | key, Jupiter key, RPC, acknowledgement (see below) |

> **Read this first.** Jawad's own backtest of these rules lost money: 100 trades, 8 wins,
> **-$17.71 before fees and -$197.71 after** the $1.80/trade fee. The strategy has not shown an edge.
> Memecoins can go to zero in minutes, and some cannot be sold at all. Run paper mode until it proves
> itself, and only ever put real money in a separate wallet holding an amount you can lose completely.

---

## What's in the box

```
memebot/            the bot (engine, executors, Telegram bot, web panel + PWA)
live_trader.py      Jawad's live-executor interface: buy()/sell() returning the dicts in his INTERFACE_CONTRACT.md
app.py, commands.py the entry points named in Prompts B and C (same program as `python -m memebot run`)
config/settings.json  Jawad's settings, byte-identical (sha256 b9f69186...)
legacy/             Jawad's original files, untouched, plus a runner for his test_bot.py
tests/              103 automated tests (pytest)
deploy/             systemd unit for an always-on Linux box
Dockerfile, docker-compose.yml
start-windows.bat, start-mac-linux.sh
```

One program runs the paper engine, the optional real-money engine, the Telegram bot and the
panel on a single event loop, so nothing fights over files. All state lives in a data folder
(default `./data`) with Jawad's formats: `settings.json`, `watchlist.csv`, `auditor_verdicts.csv`,
`state.json`, `trades.csv` (his 23 columns), `skipped.csv`, `alerts.log`. You can point it at his
existing `sim-bot` folder and it carries on from there. The real-money book keeps its own files in
`data/live/` and reads the same watchlist and verdicts.

## Quick start (paper mode)

**Windows:** install Python 3.11+ from python.org (tick "Add to PATH"), unzip this folder,
double-click `start-windows.bat`. Open http://127.0.0.1:8787.

**Mac / Linux:**
```bash
./start-mac-linux.sh            # creates .venv, installs, creates ./data, starts
```

Manual steps (any OS):
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip ...
.venv/bin/python -m memebot init  --data data      # copies settings.json, creates empty CSVs
.venv/bin/python -m memebot check --data data      # validates settings, tests API access
.venv/bin/python -m memebot run   --data data      # engine + Telegram + panel
```

Other commands: `once` (one cycle then exit, like `bot.py --once`), `report` (like `bot.py report`),
`fees` (measures the real cost of a round trip for each watchlist coin with Jupiter quotes; no trade).

Put the Scout's `watchlist.csv` and the Auditor's `auditor_verdicts.csv` in the data folder.
Without a verdicts file the bot opens nothing (fail-closed), exactly like `bot.py`.

## Telegram setup (5 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the token it gives you.
2. Open your new bot and send it any message (e.g. "hi").
3. Get your chat ID: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `"chat":{"id": ...}`. (Or message **@userinfobot**.)
4. Put both in `.env`:
   ```
   ALERT_TELEGRAM_TOKEN=123456:ABC...
   ALERT_TELEGRAM_CHAT_ID=987654321
   ```
5. Restart the bot. You get a "started" message. Send `/help`.

The bot answers **only** that chat ID; anyone else is ignored silently. If a token is ever pasted
somewhere public, revoke it in BotFather (`/revoke`) and put the new one in `.env`.

| Command | Does |
|---|---|
| `/status` | ON/OFF, open count, closed x/100, today and realized P/L, for paper and real |
| `/positions`, `/pnl`, `/closed` | details |
| `/on`, `/off` | paper entries on/off (off = no new entries; open positions still exit on TP/SL/time) |
| `/liveon`, `/liveoff` | real-money entries on/off. Only exists when LIVE is armed on the computer. |
| `/pause`, `/resume` | pause everything (no entries, no automatic exits) / resume exits |
| `/sell NAME`, `/sellall` | force-sell, with a confirm button |
| `/settings` | read-only view |

Telegram and the panel **cannot** change the mode, edit settings or move money anywhere.
Inline buttons: Paper on/off, REAL on/off, Pause everything, Resume, Status, and Approve/Reject on
each real buy.

## Phone app (PWA) and the password

The panel is a Progressive Web App: open it on your phone and choose **Add to Home Screen**.

* By default it listens on `127.0.0.1` only (this computer).
* To use it from your phone on the same Wi-Fi, set `PANEL_HOST=0.0.0.0` **and** `PANEL_PASSWORD`
  in `.env`. The bot refuses to start on a non-local address without a password.
* Phones only treat a site as a full installable app over HTTPS. The simplest safe way:
  install **Tailscale** on the computer and the phone, run `tailscale serve 8787` on the computer,
  and open the `https://<computer>.<tailnet>.ts.net` address on the phone. Add that hostname to
  `PANEL_ALLOWED_HOSTS`. Don't port-forward the panel to the open internet.

Panel security: password login with a signed HttpOnly cookie, 5 tries per 5 minutes, Host-header
check (blocks DNS-rebinding), every button needs a custom header and a same-origin request
(blocks cross-site attacks), strict Content-Security-Policy, and live data is never cached.

## Real money (LIVE): how to arm it

Nothing real can happen unless **all** of these are true at startup:

1. `"mode": "LIVE"` in `settings.json` (edited by you on the computer).
2. `MEMEBOT_PRIVATE_KEY` = the secret key of a **new, separate** wallet (base58 from Phantom/Solflare,
   or a solana-keygen JSON array). Keep only your budget in it. **Set it only in the terminal that
   starts the bot, never in a file** (not in `.env`, not in a script). The bot refuses to start in
   LIVE if it finds the key written in a `.env` file, and its `.env` loader never reads that variable.
   ```powershell
   # Windows PowerShell (lasts until you close this window)
   $env:MEMEBOT_PRIVATE_KEY = Read-Host -MaskInput "wallet key"
   .venv\Scripts\python -m memebot run --data data
   ```
   ```bash
   # Mac / Linux (read -s keeps it out of the screen and shell history)
   read -rs MEMEBOT_PRIVATE_KEY && export MEMEBOT_PRIVATE_KEY
   .venv/bin/python -m memebot run --data data
   ```
3. `JUPITER_API_KEY` (free at portal.jup.ag; keyless access is limited to about 0.5 requests a second,
   too slow to rely on for exits) and `SOLANA_RPC_URL` (these two can go in `.env`).
4. `MEMEBOT_LIVE_ACK=I UNDERSTAND THIS TRADES REAL MONEY`.

If any is missing, the bot **refuses to start** in LIVE and says what is missing. Once armed:
real entries are still OFF until you send `/liveon` (or press Start REAL), and that is refused until
the paper book has finished its run (`target_closed_trades`, 100; setting `live_requires_paper_target`,
default true), as Jawad asked. Every buy needs your Approve tap (`live_require_approval`, default
true). Exits (TP/SL/time) are automatic.

Before every real buy it re-quotes and checks, failing closed:
creator history from RugCheck must be clean (a "blind" or "danger" result blocks the buy, which is
Jawad's rule); a **sell route must exist**; the round-trip quote loss must be under
`live_max_roundtrip_loss_pct`; price impact under `live_max_price_impact_pct`; Jupiter's price within
`max_price_mismatch_pct` of DexScreener's; at most `live_max_open` real positions; the daily loss cap;
the wallet budget `live_wallet_budget_usd`; and `live_min_sol_reserve` SOL left for fees.

Every send is written to `data/live/live_journal.jsonl` **before** it goes out. If the computer
crashes mid-trade, the next start checks the wallet and either adopts the tokens it finds as a
position or raises an alert. Sells are retried with backoff; after `live_sell_max_failures` you get a
loud **MANUAL ACTION NEEDED** alert. Buy/sell signatures, actual fills and fees go into
`data/live/trades.csv` (his 23 columns plus `mode, buy_tx_sig, sell_tx_sig, actual_entry_price, ...`).

Start with the smallest possible `position_size_usd` and watch the first trades on Solscan.

## Is the $1.80 fee real? (measured Nov 27, 2026)

No, it is about 2 to 4 times too high for coins that pass these rules. A helper session measured a
$20 round trip (buy, then sell the exact amount received) with live Jupiter Swap V2 quotes on 10 real
Solana memecoins with $21k to $300k liquidity:

* quoted round-trip cost **$0.15 to $0.61 (0.7% to 3.0%)**, average $0.40 (2.0%). This already includes
  the pool fees, Jupiter's 0.1% fee each way and price impact;
* plus network fees of about $0.0004 per signature and up to about $0.07 priority fee per swap, and about
  $0.15 one-time rent for the token account on the first buy of a coin;
* realistic all-in: **about $0.45 to $0.90 per $20 round trip (2% to 4.5%)**, before slippage and price
  movement during a real trade, which can be large in fast pumps and dumps.

So the flat $1.80 (9%) makes paper results look worse than reality would, but it is a defensible safety
buffer. Jawad chose to keep $1.80 for the current paper run; with `log_quoted_fee` (on by default) every
paper trade also records the real quoted cost and the P/L it would give, so both can be compared at the
end. `"paper_fee_mode": "quoted"` would book the quoted cost instead (change it only between runs). `python -m memebot fees --data data` measures it for the coins in your watchlist. Even with
realistic fees, Jawad's backtest is still -$17.71 before any fee, so the fee is not what makes it lose.

## Settings

Jawad's 15 keys are required and validated at startup (wrong types, out-of-range values and
unknown modes stop the bot with a clear message). The file is never written by the bot. Its SHA-256
is logged at every start in `settings_hash_history.log`, and a change since the last start raises a
SETTINGS_CHANGED alert. (His settings hash changed from f521d388 to b9f69186 with no log. Jawad has
since confirmed he approved both changes: polling 1800 s to 300 s, and the 24-hour re-entry cooldown.)

Optional keys this version adds (all have defaults; add them only to change them):

| Key | Default | Meaning |
|---|---|---|
| `timezone` | `America/Toronto` | for day rollover and reading old timestamps |
| `exit_poll_seconds` | 15 | how often open positions are checked (bot.py: 300) |
| `extreme_move_pct` | 50 | a one-tick move bigger than this needs a second read before acting |
| `time_exit_grace_minutes` | 60 | paper only: how long to wait for a price after the deadline |
| `slippage_bps` | 300 | Jupiter slippage for SHEET/LIVE |
| `creator_check` | `rugcheck` | `off` disables RugCheck (then LIVE refuses to start) |
| `live_max_open` | 3 | max real positions |
| `live_max_roundtrip_loss_pct` | 5 | the main cost gate: a buy-then-sell quote must lose less than this |
| `live_max_price_impact_pct` | 10 | loose sanity cap only: Jupiter's `priceImpact` was measured to be noisy (4.2% reported on a coin whose real round trip cost 1.4%) |
| `live_wallet_budget_usd` | 40 | max $ in real positions at once (≈ his $60 CAD) |
| `live_min_sol_reserve` | 0.02 | SOL always kept for fees |
| `live_require_approval` | true | each real buy needs an Approve tap |
| `live_approval_timeout_seconds` | 120 | after this, the request expires |
| `live_sell_max_failures` | 5 | then MANUAL ACTION NEEDED |
| `pumpfun_audit` | true | record the pump.fun price as an audit field |
| `live_requires_paper_target` | true | real entries can't be turned on until paper has `target_closed_trades` closed |
| `paper_fee_mode` | `flat` | `flat` = `roundtrip_fee_usd` per trade (Jawad's rule, kept for the current run); `quoted` = book the real Jupiter round-trip quote instead |
| `log_quoted_fee` | true | also log the real quoted cost next to the flat fee: `quoted_fee_usd` and `net_pnl_quoted` columns at the end of trades.csv, so both can be compared |
| `network_fee_sol_per_swap` | 0.0002 | used by `quoted` |
| `token_account_rent_sol` | 0.00204 | used by `quoted` |

## Fixes compared with bot.py

| bot.py problem | Now |
|---|---|
| Connection reset / remote disconnect crashed the bot; the main loop had no error handling | all network calls retry with backoff and Retry-After; each loop tick is isolated; repeated failures alert you |
| No price → the 24h time exit never fired, so a position could stay open forever | time exit fires regardless; paper waits a grace period, then exits at the last known price |
| Pairs where the coin is the *quote* token were used, giving another coin's price | only pairs where `baseToken.address == mint` count |
| LIVE reprinted the same trade sheet every scan | one sheet per coin per day, logged to `trade_sheets.csv` |
| Skip reasons with changing numbers defeated the once-a-day dedupe; skipped.csv grew every scan | numbers are ignored in the dedupe key |
| Naive local timestamps, so deadlines moved across daylight-saving changes | timezone-aware ISO timestamps; old ones are read in your timezone |
| 300 s exit checks made paper stop-loss fills optimistic | 15 s exit checks, batched (30 coins per DexScreener call) |
| `state.json` written without fsync; a corrupt file would be silently replaced | fsync + `.bak` copy; a corrupt state stops the bot instead of forgetting open positions |
| Two copies could run on the same folder | single-instance lock |
| Telegram library logs the bot token inside request URLs | httpx logs silenced and every log line passes a redaction filter |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q                  # 103 tests
python legacy/run_legacy_tests.py    # Jawad's original 14 tests against his original bot.py
```

The tests use fake market data, a fake Jupiter and a fake wallet; they never touch the network or
real money. `tests/test_legacy_bugs.py` reproduces each bot.py bug against his untouched file.

## A second "fast lane" bot

Jawad's notes mention a second paper bot (`sim-fast`, 4-hour time exit). Run it as a second copy with
its own data folder and settings: copy `data/settings.json` to `data-fast/settings.json`, set
`"time_exit_hours": 4` and `"lane": "FAST LANE"`, put the same watchlist and verdicts there, and start
`PANEL_PORT=8788 python -m memebot run --data data-fast --no-telegram` (one Telegram bot token can
only be polled by one program; use a second BotFather bot if you want fast-lane alerts too). The lock stops two copies from ever sharing one folder.

## Always-on

The bot only trades while it runs. For 24/7 use either:

* **Docker**: set `PANEL_PASSWORD` in `.env` (inside the container the panel listens on all interfaces, so the
  bot requires it), then `docker compose up -d --build` (data in `./data`, panel on `127.0.0.1:8787`). If the
  container can't write `./data`, run `sudo chown -R 10001 data`.
* **Linux server**: copy to `/opt/memebot`, create `.venv`, then use `deploy/memebot.service`.
* **Windows PC**: Task Scheduler → "At log on" → run `start-windows.bat`, and set the PC never to sleep.

## Verified against live data

A separate read-only session (no wallet, no keys, quotes only) checked this code against the live APIs
and their docs on Nov 27, 2026: Jupiter Swap V2 `/order` and `/execute` field names (price impact is
read correctly in both its percent and fraction forms), Jupiter price and token APIs, DexScreener batch
prices (30 per call, 53 of 53 real coins priced, base-token filter correct) and RugCheck reports (it
flagged a real coin for "Creator history of rugged tokens", so that coin would be blocked in LIVE).
Not tested for real: signing and sending a transaction (no money was spent, by Jawad's choice) and
Telegram message delivery (the build machine can't reach Telegram). Do those yourself on tiny amounts.

## Research this is based on

* **freqtrade**: Telegram control with chat-ID auth, `/stopentry`-style stop that keeps managing open
  trades, `/forceexit`, dry-run by default.
* **Jupiter Swap V2** (order → sign → execute, with `requestId`), checking a sell route before buying,
  and recording the executed in/out amounts.
* **RugCheck** token reports for creator/authority risks.
* The usual bot failure modes: stuck positions, duplicate instances, crash during a trade, rate
  limits (429), token leaks in logs, and quote-token price confusion on DEX aggregators.
