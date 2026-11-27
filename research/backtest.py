"""Backtest engine for 1-minute pool candles. Built to avoid the usual ways backtests lie.

Rules (see research/METHOD.md for the reasoning):
  * No look-ahead: a signal uses candles up to and including minute t; the entry fills at the OPEN
    of minute t+1, plus slippage.
  * Conservative intrabar: if one candle touches both the stop and the target, the stop wins. A
    candle that opens through the stop fills at the open (gap), not at the stop price.
  * Trailing stops only use highs from candles BEFORE the current one.
  * Costs from measurement (research/fee_check_2026-11-27.md): fee + price impact by liquidity, extra
    slippage on stop-outs, network fees. A 2x-cost stress run is reported too.
  * One entry per pool per strategy, so trades are close to independent.
  * A trade still open when the data ends exits at the last traded price (for a dead pool that is
    the post-rug price), flagged "data_end". Nothing is dropped.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

SIZE_USD = 20.0
DEAD_HAIRCUT = 0.2          # a dead pool's last print is worth at most 20% when you try to sell
DEAD_GAP_S = 30 * 60        # no candle in the last 30 minutes of the dataset = dead


# --------------------------------------------------------------------------- data
@dataclass
class Pool:
    pool: str
    dex: str
    created_ts: int           # unix seconds
    t: np.ndarray             # candle start times (s), ascending
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray             # volume USD
    liq: np.ndarray           # liquidity USD known at each candle (nan if unknown), from snapshots
    buys5: np.ndarray         # m5 buys from the latest snapshot at or before the candle (nan if unknown)
    sells5: np.ndarray
    meta: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.t)

    @property
    def fdv(self) -> float:
        return _f(self.meta.get("fdv_usd"))

    def end_price(self) -> tuple[float, str]:
        """Price at which a still-open trade is closed when the data ends. If the pool stopped trading
        well before the dataset ended (a rug or dead pool), you could not really sell at the last
        print: book DEAD_HAIRCUT of it, reason "dead"."""
        if self.meta.get("dead_end"):
            return self.c[-1] * DEAD_HAIRCUT, "dead"
        return self.c[-1], "data_end"


def _iso_to_ts(s: str | None) -> int | None:
    if not s:
        return None
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def load_pumpportal(pp_dir: str | Path | None) -> dict[str, dict]:
    """mint -> {"creator_sol", "create_ts", "migrate_ts"} from the PumpPortal recorder
    (events-*.jsonl: create and migrate events). Missing directory -> {}."""
    out: dict[str, dict] = {}
    if not pp_dir or not Path(pp_dir).exists():
        return out
    for f in sorted(Path(pp_dir).glob("events-*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            mint, kind = e.get("mint"), e.get("txType")
            if not mint or kind not in ("create", "migrate"):
                continue
            r = out.setdefault(mint, {})
            if kind == "create":
                r["creator_sol"] = _f(e.get("solAmount"))
                r["create_ts"] = _f(e.get("recv_ts"))
            else:
                r["migrate_ts"] = _f(e.get("recv_ts"))
    return out


def load_pools(data_dir: str | Path, min_candles: int = 10, pp_dir: str | Path | None = None) -> list[Pool]:
    d = Path(data_dir)
    pp = load_pumpportal(pp_dir)
    meta = {}
    for line in (d / "pools.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
            meta[r["pool"]] = r
        except (ValueError, KeyError):
            pass
    snaps: dict[str, list] = {}
    sp = d / "snapshots.jsonl"
    if sp.exists():
        for line in sp.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = _iso_to_ts(r.get("at"))
            if ts is None or not r.get("pool"):
                continue
            tx = ((r.get("txns") or {}).get("m5") or {})
            snaps.setdefault(r["pool"], []).append((ts, _f(r.get("liq_usd")), _f(tx.get("buys")), _f(tx.get("sells"))))
    pools = []
    for f in sorted((d / "ohlcv").glob("*.json")):
        m = meta.get(f.stem)
        if not m:
            continue
        rows = json.loads(f.read_text())
        if len(rows) < min_candles:
            continue
        a = np.asarray(rows, dtype=float)
        created = _iso_to_ts(m.get("created_at")) or int(a[0, 0])
        t = a[:, 0].astype(np.int64)
        liq = np.full(len(t), np.nan)
        b5 = np.full(len(t), np.nan)
        s5 = np.full(len(t), np.nan)
        sn = sorted(snaps.get(f.stem, []))
        if sn:
            st = np.array([x[0] for x in sn])
            idx = np.searchsorted(st, t + 59, side="right") - 1  # snapshot taken by the END of the candle
            ok = idx >= 0
            liq[ok] = [sn[i][1] for i in idx[ok]]
            b5[ok] = [sn[i][2] for i in idx[ok]]
            s5[ok] = [sn[i][3] for i in idx[ok]]
        info = pp.get(m.get("base_mint") or "")
        if info:
            m = dict(m, pp_creator_sol=info.get("creator_sol"),
                     pp_secs_to_migrate=(info["migrate_ts"] - info["create_ts"])
                     if info.get("migrate_ts") and info.get("create_ts") else None)
        reserve = _f(m.get("reserve_usd"))
        if reserve == reserve and reserve > 0:
            liq = np.where(np.isnan(liq), reserve, liq)  # GeckoTerminal reserve when DexScreener has none
        pools.append(Pool(f.stem, m.get("dex") or "", created, t, a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5],
                          liq, b5, s5, m))
    if pools:
        end = max(int(p.t[-1]) for p in pools)
        for p in pools:
            last_liq = p.liq[-1]
            p.meta["dead_end"] = bool(end - int(p.t[-1]) > DEAD_GAP_S and not (last_liq == last_liq and last_liq >= 5_000))
    pools.sort(key=lambda p: p.created_ts)
    return pools


def _f(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------- costs
# Pool fee per side by venue (+ Jupiter's 0.1%). pump.fun's bonding curve charges 1.25% per side;
# PumpSwap small caps up to 1.25% (measured round trips on PumpSwap coins: 1.5-3%); Raydium/Meteora lower.
DEX_FEE_SIDE = {"pump-fun": 0.0135, "pumpswap": 0.0095, "raydium": 0.0035, "raydium-clmm": 0.0035,
                "raydium-launchlab": 0.0110, "meteora": 0.0060, "meteora-damm-v2": 0.0060, "meteora-dbc": 0.0110}


@dataclass(frozen=True)
class Costs:
    """Per-side cost as a fraction of notional, calibrated on the measured Jupiter quotes (a $20 round
    trip cost 0.7%-3.0%, mean 2.0%, at $21k-$300k liquidity, before network fees)."""
    default_side: float = 0.0110    # venue fee when the dex is unknown
    impact_k: float = 2.0           # constant-product impact ~ 2 * size / liquidity per side
    unknown_liq: float = 3_000.0    # pessimistic liquidity when neither a snapshot nor a reserve is known
    stop_extra: float = 0.01        # extra slippage on stop-loss / data_end exits (fast markets)
    network_usd: float = 0.06       # 2 swaps incl. priority fee, plus a share of token-account rent
    mult: float = 1.0               # stress multiplier

    def side(self, liq: float, dex: str = "", fdv: float = float("nan")) -> float:
        L = liq if (liq and liq == liq and liq > 0) else self.unknown_liq
        fee = DEX_FEE_SIDE.get(dex, self.default_side)
        if dex == "pumpswap":
            # PumpSwap fees are tiered by market cap (about 1.25% right after graduation, falling to a
            # ~0.30% floor); unknown market cap is treated as a fresh graduate.
            fee = 0.0135 if not (fdv == fdv) or fdv < 100_000 else (0.0095 if fdv < 1_000_000 else 0.0040)
        return self.mult * (fee + self.impact_k * SIZE_USD / L)


# --------------------------------------------------------------------------- exits
@dataclass(frozen=True)
class Exit:
    tp: float | None = 0.3        # +30% take profit (fraction), None = off
    sl: float | None = 0.15       # -15% stop
    trail: float | None = None    # trailing stop distance from the highest prior high
    max_min: int = 240            # time exit in minutes

    def key(self) -> str:
        return f"tp{self.tp}_sl{self.sl}_tr{self.trail}_t{self.max_min}"


@dataclass
class Trade:
    pool: str
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    reason: str
    gross: float     # fraction
    net_usd: float


def simulate(p: Pool, i_sig: int, ex: Exit, costs: Costs) -> Trade | None:
    """Signal at the close of candle i_sig; enter at the open of i_sig+1."""
    i0 = i_sig + 1
    if i0 >= p.n:
        return None
    # Only enter if the next candle is really the next minute (a gap means no trades: no fill possible).
    if p.t[i0] - p.t[i_sig] > 120:
        return None
    liq0 = p.liq[i_sig]
    if liq0 == liq0 and liq0 < 100 * SIZE_USD:
        return None  # position would be more than 1% of the pool
    side_in = costs.side(liq0, p.dex, p.fdv)
    px_in = p.o[i0]
    if not (px_in > 0):
        return None
    tp = px_in * (1 + ex.tp) if ex.tp else math.inf
    sl = px_in * (1 - ex.sl) if ex.sl else -math.inf
    t_end = p.t[i0] + ex.max_min * 60
    hi_prior = px_in
    px_out, reason = p.end_price()
    j = p.n - 1
    for j in range(i0, p.n):
        if p.t[j] >= t_end:
            reason, px_out = "time", p.o[j]  # exit at the first candle after the deadline
            break
        stop = sl
        if ex.trail:
            stop = max(stop, hi_prior * (1 - ex.trail))
        o, h, l, c = p.o[j], p.h[j], p.l[j], p.c[j]
        if o <= stop:
            reason, px_out = ("trail" if ex.trail and stop > sl else "stop"), o
            break
        if l <= stop:
            reason, px_out = ("trail" if ex.trail and stop > sl else "stop"), stop
            break
        if h >= tp:
            reason, px_out = "tp", max(tp, o)
            break
        hi_prior = max(hi_prior, h)
    else:
        j = p.n - 1
        px_out, reason = p.end_price()
    liq_out = p.liq[min(j, p.n - 1)]
    side_out = costs.side(liq_out, p.dex, p.fdv) + (costs.stop_extra * costs.mult if reason in ("stop", "trail", "data_end", "dead") else 0.0)
    gross = px_out / px_in - 1.0
    net_frac = (1 - side_in) * (px_out / px_in) * (1 - side_out) - 1.0
    net_usd = SIZE_USD * net_frac - costs.network_usd * costs.mult
    return Trade(p.pool, i0, j, px_in, px_out, reason, gross, net_usd)


# --------------------------------------------------------------------------- stats
def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    x = np.array([t.net_usd for t in trades])
    n = len(x)
    mean = float(x.mean())
    sd = float(x.std(ddof=1)) if n > 1 else 0.0
    wins = x[x > 0]
    losses = -x[x <= 0]
    pf = float(wins.sum() / losses.sum()) if losses.sum() > 0 else math.inf
    top3 = np.sort(x)[-3:].sum() if n >= 3 else x.sum()
    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {"n": n, "total_usd": float(x.sum()), "mean_usd": mean, "median_usd": float(np.median(x)),
            "win_rate": float((x > 0).mean()), "pf": pf, "t": (mean / sd * math.sqrt(n)) if sd > 0 else 0.0,
            "worst_usd": float(x.min()), "best_usd": float(x.max()), "without_top3_usd": float(x.sum() - top3),
            "reasons": reasons}


def bootstrap_p_mean_le_0(x: np.ndarray, n_boot: int = 4000, seed: int = 7) -> float:
    """One-sided p-value for mean > 0 (centered bootstrap, robust to fat tails better than a t-test)."""
    if len(x) < 5:
        return 1.0
    rng = np.random.default_rng(seed)
    xc = x - x.mean()
    bm = rng.choice(xc, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float((bm >= x.mean()).mean())


def holm(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def pbo_cscv(perf: np.ndarray, n_blocks: int = 8) -> float:
    """Probability of Backtest Overfitting (Bailey, Borwein, Lopez de Prado, Zhu 2017), CSCV method.
    perf: [n_strategies, n_blocks] mean P/L per time block. Returns the fraction of in-sample/out-of-sample
    splits where the in-sample best strategy ranks below the out-of-sample median."""
    from itertools import combinations

    S = perf.shape[1]
    if S < 4 or perf.shape[0] < 2:
        return float("nan")
    half = S // 2
    below = total = 0
    for is_blocks in combinations(range(S), half):
        oos = [b for b in range(S) if b not in is_blocks]
        is_perf = np.nanmean(perf[:, list(is_blocks)], axis=1)
        oos_perf = np.nanmean(perf[:, oos], axis=1)
        if np.all(np.isnan(is_perf)):
            continue
        best = int(np.nanargmax(is_perf))
        rank = (np.sum(oos_perf < oos_perf[best]) + 0.5 * np.sum(oos_perf == oos_perf[best])) / len(oos_perf)
        below += rank < 0.5
        total += 1
    return below / total if total else float("nan")


def simulate_many(p: Pool, i_sig: int, exits: list[Exit], costs: Costs) -> list[tuple[float, str, int]] | None:
    """Same rules as simulate() for many exits at once (vectorized over the path). Returns
    [(net_usd, reason, exit_index)] in the order of `exits`, or None if no fill is possible."""
    i0 = i_sig + 1
    if i0 >= p.n or p.t[i0] - p.t[i_sig] > 120 or not (p.o[i0] > 0):
        return None
    if p.liq[i_sig] == p.liq[i_sig] and p.liq[i_sig] < 100 * SIZE_USD:
        return None  # position would be more than 1% of the pool
    px_in = p.o[i0]
    side_in = costs.side(p.liq[i_sig], p.dex, p.fdv)
    t, o, h, l = p.t[i0:], p.o[i0:], p.h[i0:], p.l[i0:]
    # highest high of candles strictly before each candle (trailing reference), starting at entry price
    prior_hi = np.maximum.accumulate(np.concatenate(([px_in], h[:-1])))
    prior_hi = np.maximum(prior_hi, px_in)
    out = []
    last_c = p.c[-1]
    for ex in exits:
        tp = px_in * (1 + ex.tp) if ex.tp else np.inf
        sl = px_in * (1 - ex.sl) if ex.sl else -np.inf
        stop = np.full(len(t), sl) if not ex.trail else np.maximum(sl, prior_hi * (1 - ex.trail))
        timed = t >= t[0] + ex.max_min * 60
        stop_hit = (o <= stop) | (l <= stop)
        tp_hit = h >= tp
        ev = timed | stop_hit | tp_hit
        if not ev.any():
            j = p.n - 1
            px_out, reason = p.end_price()
        else:
            k = int(np.argmax(ev))
            j = i0 + k
            if timed[k]:
                reason, px_out = "time", o[k]
            elif stop_hit[k]:
                reason = "trail" if (ex.trail and stop[k] > sl) else "stop"
                px_out = o[k] if o[k] <= stop[k] else stop[k]
            else:
                reason, px_out = "tp", max(tp, o[k])
        side_out = costs.side(p.liq[j], p.dex, p.fdv) + (costs.stop_extra * costs.mult if reason in ("stop", "trail", "data_end", "dead") else 0.0)
        net_frac = (1 - side_in) * (px_out / px_in) * (1 - side_out) - 1.0
        out.append((SIZE_USD * net_frac - costs.network_usd * costs.mult, reason, j))
    return out


def deflated_sharpe(x: np.ndarray, n_trials: int, sr_var_trials: float) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014): probability that the true per-trade Sharpe
    is above the best you would expect from n_trials zero-edge strategies. x = per-trade P/L."""
    from statistics import NormalDist

    x = np.asarray(x, float)
    T = len(x)
    if T < 10 or n_trials < 2 or not (sr_var_trials > 0):
        return float("nan")
    sd = x.std(ddof=1)
    if sd == 0:
        return float("nan")
    sr = x.mean() / sd
    z = (x - x.mean()) / sd
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    nd, gamma = NormalDist(), 0.5772156649
    sr0 = math.sqrt(sr_var_trials) * ((1 - gamma) * nd.inv_cdf(1 - 1 / n_trials)
                                      + gamma * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return float("nan")
    return float(nd.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom)))


def bootstrap_ci(x: np.ndarray, n_boot: int = 4000, seed: int = 11, alpha: float = 0.05) -> tuple[float, float]:
    x = np.asarray(x, float)
    if len(x) < 5:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float(np.quantile(m, alpha / 2)), float(np.quantile(m, 1 - alpha / 2))
