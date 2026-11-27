# What actually makes money in Solana memecoins: research summary (Nov 27, 2026)

Four research agents read the published evidence, practitioner sources, data sources, and the
statistics of strategy selection. This is the combined result. Numbers marked (S) came from search
summaries of the source because the full text could not be fetched from the research sandbox; check the
original before quoting them. `reports/4_papers_math_probability.md` has the full statistics report with
formulas; the other files in `reports/` are working notes.

## The short answer

**There is no published evidence of a positive-expectancy *long* strategy on Solana memecoins for a
slow (15-second) retail bot after realistic costs.** The money in this market goes, in measured order,
to: the platform and bot fees; token creators and their own "sniper" wallets; first-block snipers;
sandwich/MEV bots; and arbitrageurs. Every documented edge needs sub-second speed or inside
information. The best-evidenced use of public data is **avoiding** losses (rug, bundle and wash-trading
filters), not creating profit. Treat any backtest that says otherwise as wrong until it survives the
tests in `METHOD.md`.

## Base rates (why "buy coins that look good" loses)

| Fact | Number | Source | Credibility |
|---|---|---|---|
| pump.fun tokens that graduate to a real pool | 0.63% (655,770 tokens, Sep-Oct 2025); typically 1-2.7%, briefly ~5-7% after BOOST (Jul 2026) | Marino, Naviglio, Tarantelli, Lillo, arXiv 2602.14860 (S); The Block; Bitquery | strong / moderate |
| tokens whose last trade is on launch day | 68.67% of 18.67M | CoinGecko (S) | moderate |
| tokens with rug / pump-and-dump traits | 98.6% of pump.fun tokens; 93% of 361k Raydium pools | Solidus Labs (S) | moderate (vendor, broad definition) |
| median graduate 24 h after graduation | about 3.8% of graduation market cap (-96%); 3.6% at or above it after 7 days | MemeFees (S) | weak, but every source agrees on direction |
| median return 15 minutes after a DEX launch | about -50% | SOLMEMES (S) | moderate |
| memecoins that rose >100% showing manipulation | 82.8% of 707 | Mongardini & Mei, USENIX Security 2026 | strong |
| supply held by bundled wallets in graduated tokens | 36.5% on average; 84% labelled high-risk | MemeTrans, arXiv 2602.13480 (S) | moderate-strong |
| wallets with >$10k realised profit | 0.4% of 13.55M | Dune via Decrypt | moderate |
| who has significantly positive returns (post-TRUMP period) | creators 19.2%, snipers 16.0%, ordinary traders 3-4% | SBFC 2025 conference paper (S) | moderate-strong |

Jawad's screen (liquidity ≥ $20k, ≥ 500 holders, top-10 < 30%) selects coins *after* their pump, and a
top-10 filter is defeated by splitting supply across many wallets. His 8% win rate matches these base
rates; it is not a tuning problem.

## Strategy families: evidence vs fluff

| Strategy | What the evidence says | Slow bot? | Verdict |
|---|---|---|---|
| Launch sniping (block 0) | Real profits, mostly to creator-funded snipers (Pine Analytics: 15k SOL in one month, 85% exit within 5 min) | No | real, not for us |
| Migration buy (instant) | Only feature marketing; early pools show buys ≈ sells (holders exiting) | No | fluff for us |
| Bonding-curve stage play | Graduation probability rises with SOL in the curve (Marino et al.) but no P&L study; graduation is usually the top | Partly | untested |
| **BOOST window (niche)** | Since 2026-07-21 the protocol buys a graduated token by TWAP for ~5 min after migration (~17.6 SOL) and burns it. A known, mechanical buy flow; no published returns | Yes, if seen within ~30 s | **untested, worth testing** |
| Momentum / breakout (1-5 min) | One open-source study: a tight stop lost 79.5% while random entries made +19%; a wide stop (-40/+150) beat 45 of 50 random runs but only in 2 of 3 weeks, on survivor-only data | Yes | weak, test against random |
| Dip-buy / mean reversion | On daily data lost least of five strategies (-14% vs -72% buy-and-hold) but still lost | Yes | weak |
| Second wave / consolidation breakout | No study found | Yes | opinion only |
| Copy-trading smart / KOL wallets | Strongly negative: copying "proven" wallets 30 s late lost 10-15%, worse than copying random wallets; 74% of big leaderboard gains were not normal trading (Bitquery) | Technically | fluff |
| KOL calls | +1.8% day 1, then -2.2% at 10 days, -6.5% at 30 days | No (the day-1 move is taken in seconds) | negative |
| Telegram pump groups | Profits go to organisers who buy before the call (Xu & Livshits, USENIX 2019) | No | negative |
| DexScreener boosts / trending | Tokens often fall during or after a boost (Dethective, S) | Yes | possible *avoid* filter |
| Meteora DLMM fee farming | No published live P&L; impermanent loss often exceeds fees on memecoins | Yes | unproven |
| Arbitrage / MEV | ~$143M a year (Helius), but tips take 50-60% and the fastest infrastructure wins | No | real, not for us |
| Rug / bundle / wash filters | Bundle stats predict collapse better than top-10 share; models claim 56% loss reduction (MemeTrans) | Yes | **evidenced, as loss avoidance** |

## Costs for a small bot

Measured with live Jupiter quotes (`fee_check_2026-11-27.md`): a $20 round trip costs 0.7-3.0% in
quotes, about $0.45-0.90 all-in. Using a UI bot (Photon, BullX) adds 1% per side. Priority fees and
stop-losses that gap through in a dump add more. At $20 positions the fixed fees are a large share; at
$200 they would be under 1%.

## The math that decides it

* **Exits cannot create an edge.** If price has no drift, P(hit +a before -b) = b/(a+b) and any mix of
  take-profit, stop-loss, trailing or time exits has expected return zero before costs, negative after.
  Edge can only come from the *entry* signal. So every strategy must beat **random entries with the same
  exits** on the same coins.
* **Luck looks like skill when you test many strategies.** The best of N zero-edge strategies has an
  expected t-statistic of about 2.5 (N=100), 3.05 (N=500), 3.26 (N=1,000). This matrix has ~27,000
  combinations, so in-sample "winners" with t ≈ 3-4 are expected from noise alone.
* **Lottery strategies need huge samples.** With a 20% win rate, +98% wins and -22% losses, a real +2%
  edge per trade needs about 2,300 trades for t = 2 and about 7,900 for the t ≈ 3.7 needed after testing
  500 strategies. A few weeks of data cannot *prove* an edge of that size; it can only rule things out.
* **Position size vs budget.** Kelly for that example is about 8.7% of bankroll. $20 trades on a $40-60
  budget are 33-50% per trade, 4-6 times Kelly, where the bankroll is expected to shrink even with a real
  edge. Risk of ruin with $30 of room and a true +2% edge is about 77%. Keep positions at most 1/10 of
  the budget.

## What we test (see `METHOD.md` and `strategies.py`)

All the families above that a slow bot can run, including the niche BOOST-window trade and the
"graduates that hold" pattern, under strict rules: forward-collected data that includes dead coins,
entry at the next candle's open, stop-before-target inside a candle, measured costs plus a 2x-cost
stress test, selection on the earlier period only, judgment on the later period with Holm correction,
a 20-seed random-entry control, the Probability of Backtest Overfitting, and a robustness check that
drops the best 1% of trades. "Nothing passes" is an acceptable and honest result.

## Sources (main)

Marino et al. arXiv 2602.14860 · MemeTrans arXiv 2602.13480 · Mongardini & Mei, USENIX Security 2026 ·
Xu & Livshits arXiv 1811.10109 · Hamrick et al. (UCL) · Li, Shin & Wang (JFQA) · La Morgia et al. ·
Cernera et al. USENIX Security 2023 · Xia et al. (Uniswap scams) · Mazorra et al. (rug pulls) · Bailey,
Borwein, López de Prado & Zhu (PBO/CSCV) · Bailey & López de Prado (Deflated Sharpe) · Harvey & Liu
(multiple testing) · White (Reality Check) · Hansen (SPA) · Romano & Wolf (StepM) · Solidus Labs ·
Pine Analytics · Bitquery investigations · CoinGecko research · Dune dashboards · Helius MEV report ·
open-source repos kendallcoding202/memecoin-bots, leex0x0/memecoin-paper-trader,
zostaff/quant-memcoins-researcher, chainstacklabs/pumpfun-bonkfun-bot, aldiopramudya/LP-METEORA-BOT.
