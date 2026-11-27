"""The strategy matrix: entry rules x filters x exits.

Each entry rule returns the index of the FIRST candle whose close triggers the signal (the trade
fills at the next candle's open), or None. Every rule only reads candles 0..i, never later ones.

Families (from research/notes: what practitioners actually run, and what the evidence says):
  baseline   Jawad's rule shape: enter once the pool is past a minimum age (his screens came from
             scouts; here: first candle past the age with the liquidity filter), TP30/SL15
  early      buy in the first minutes after the pool appears (graduation / launch plays)
  momentum   breakout: k-minute return above x with a volume spike
  dip        buy a deep drawdown from the high after a big run-up, once it stabilises
  second     "second wave": after a quiet consolidation hours in, buy the break of the range
  flow       DexScreener buy/sell pressure: m5 buys clearly above sells, with rising price
  random     control: a random candle in the pool's traded life (same filters/exits), to measure what
             costs and the exit rule alone do. (It knows the pool's length, so it is a control, not a strategy.)
"""
from __future__ import annotations

import itertools
import zlib
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .backtest import Exit, Pool


@dataclass(frozen=True)
class Entry:
    family: str
    params: tuple
    fn: Callable[[Pool], int | None]

    def key(self) -> str:
        return f"{self.family}{self.params}"


def _age_min(p: Pool) -> np.ndarray:
    return (p.t - p.created_ts) / 60.0


def _liq_ok(p: Pool, i: int, min_liq: float) -> bool:
    if min_liq <= 0:
        return True
    L = p.liq[i]
    return L == L and L >= min_liq  # unknown liquidity fails a liquidity filter


def baseline(min_age: float, min_liq: float):
    def fn(p: Pool):
        age = _age_min(p)
        for i in range(p.n - 1):
            if age[i] >= min_age and _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("baseline", (min_age, min_liq), fn)


def early(max_age: float, max_runup: float, min_liq: float):
    """Within the first max_age minutes, as long as price has not already run up > max_runup."""
    def fn(p: Pool):
        age = _age_min(p)
        first = p.o[0]
        for i in range(p.n - 1):
            if age[i] > max_age:
                return None
            if p.c[i] / first - 1 <= max_runup and _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("early", (max_age, max_runup, min_liq), fn)


def momentum(k: int, min_ret: float, vol_mult: float, min_age: float, max_age: float, min_liq: float):
    def fn(p: Pool):
        age = _age_min(p)
        v = p.v
        for i in range(max(k, 15), p.n - 1):
            if age[i] < min_age:
                continue
            if age[i] > max_age:
                return None
            if p.c[i - k] <= 0 or p.c[i] / p.c[i - k] - 1 < min_ret:
                continue
            base = v[max(0, i - 60):i - k + 1].mean() if i - k + 1 > max(0, i - 60) else 0.0
            if base <= 0 or v[i - k + 1:i + 1].mean() < vol_mult * base:
                continue
            if _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("momentum", (k, min_ret, vol_mult, min_age, max_age, min_liq), fn)


def dip(min_runup: float, min_dd: float, bounce: float, min_liq: float):
    """After the price ran up >= min_runup from the first open, wait for a drawdown of >= min_dd
    from the running high, then a bounce of >= bounce off the running low since that high."""
    def fn(p: Pool):
        first = p.o[0]
        hi = p.h[0]
        lo_since_hi = p.l[0]
        armed = False
        for i in range(p.n - 1):
            if p.h[i] > hi:
                hi, lo_since_hi = p.h[i], p.l[i]
            lo_since_hi = min(lo_since_hi, p.l[i])
            if hi / first - 1 >= min_runup and p.c[i] / hi - 1 <= -min_dd:
                armed = True
            if armed and lo_since_hi > 0 and p.c[i] / lo_since_hi - 1 >= bounce and p.c[i] / hi - 1 <= -min_dd * 0.5:
                if _liq_ok(p, i, min_liq):
                    return i
        return None
    return Entry("dip", (min_runup, min_dd, bounce, min_liq), fn)


def second_wave(min_age: float, window: int, max_range: float, min_liq: float):
    """After min_age minutes, if the last `window` candles traded in a tight range (high/low - 1 <=
    max_range), buy the first close above that range's high."""
    def fn(p: Pool):
        age = _age_min(p)
        for i in range(window + 1, p.n - 1):
            if age[i] < min_age:
                continue
            hi = p.h[i - window:i].max()
            lo = p.l[i - window:i].min()
            if lo <= 0 or hi / lo - 1 > max_range:
                continue
            if p.c[i] > hi and _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("second", (min_age, window, max_range, min_liq), fn)


def flow(min_ratio: float, min_buys: float, min_age: float, min_liq: float):
    """DexScreener m5 buys/sells (the latest snapshot at or before the candle) plus a rising close."""
    def fn(p: Pool):
        age = _age_min(p)
        for i in range(5, p.n - 1):
            if age[i] < min_age:
                continue
            b, s = p.buys5[i], p.sells5[i]
            if not (b == b and s == s) or b < min_buys:
                continue
            if b / max(s, 1.0) >= min_ratio and p.c[i] > p.c[i - 5] and _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("flow", (min_ratio, min_buys, min_age, min_liq), fn)


def boost_window(max_age_min: float, min_liq: float):
    """Niche, from the practitioner research: pump.fun BOOST mode (migrations after 2026-07-21) has
    the protocol buy the token by TWAP for about 5 minutes after migration to PumpSwap. Buy a new
    PumpSwap pool in its first minutes; exits with a ~5-minute time stop ride that known buy flow."""
    def fn(p: Pool):
        if "pumpswap" not in (p.dex or ""):
            return None
        age = _age_min(p)
        for i in range(p.n - 1):
            if age[i] > max_age_min:
                return None
            if _liq_ok(p, i, min_liq):
                return i
        return None
    return Entry("boost", (max_age_min, min_liq), fn)


def boost_filtered(min_secs_to_migrate: float, max_creator_sol: float):
    """BOOST window, but only for graduates that look organic per the research: the curve took at least
    min_secs_to_migrate to fill (fast fills are usually the creator or a bundle buying it out) and the
    creator's own initial buy was at most max_creator_sol. Needs PumpPortal data (create + migrate)."""
    def fn(p: Pool):
        if "pumpswap" not in (p.dex or ""):
            return None
        secs, csol = p.meta.get("pp_secs_to_migrate"), p.meta.get("pp_creator_sol")
        if secs is None or csol is None or not (secs == secs and csol == csol):
            return None
        if secs < min_secs_to_migrate or csol > max_creator_sol:
            return None
        age = _age_min(p)
        for i in range(p.n - 1):
            if age[i] > 2:
                return None
            return i
        return None
    return Entry("boost_f", (min_secs_to_migrate, max_creator_sol), fn)


def graduate_hold(min_age: float, max_age: float, min_ratio: float):
    """'Graduates that hold': a PumpSwap pool hours after migration still at or above its first
    price, with more buys than sells in the latest snapshot."""
    def fn(p: Pool):
        if "pumpswap" not in (p.dex or ""):
            return None
        age = _age_min(p)
        first = p.o[0]
        for i in range(1, p.n - 1):
            if age[i] < min_age:
                continue
            if age[i] > max_age:
                return None
            b, s = p.buys5[i], p.sells5[i]
            if p.c[i] >= first and b == b and s == s and b / max(s, 1.0) >= min_ratio:
                return i
        return None
    return Entry("grad_hold", (min_age, max_age, min_ratio), fn)


def random_entry(seed: int, min_liq: float):
    def fn(p: Pool):
        rng = np.random.default_rng(zlib.crc32(f"{p.pool}:{seed}".encode()))  # stable across runs
        ok = [i for i in range(p.n - 1) if _liq_ok(p, i, min_liq)]
        return int(rng.choice(ok)) if ok else None
    return Entry("random", (seed, min_liq), fn)


def entries() -> list[Entry]:
    E = []
    for a, L in itertools.product((0, 30, 120), (0, 20_000)):
        E.append(baseline(a, L))
    for a, r, L in itertools.product((3, 10, 30), (0.5, 2.0), (0, 20_000)):
        E.append(early(a, r, L))
    for k, x, vm, (a0, a1), L in itertools.product((3, 10), (0.15, 0.4), (2.0, 4.0),
                                                   ((5, 120), (60, 1440)), (0, 20_000)):
        E.append(momentum(k, x, vm, a0, a1, L))
    for ru, dd, b, L in itertools.product((1.0, 3.0), (0.5, 0.7), (0.15, 0.3), (0, 20_000)):
        E.append(dip(ru, dd, b, L))
    for a, w, mr, L in itertools.product((60, 180), (20, 45), (0.15, 0.3), (0, 20_000)):
        E.append(second_wave(a, w, mr, L))
    for ratio, mb, a, L in itertools.product((1.5, 2.5), (10, 40), (5, 30), (0, 20_000)):
        E.append(flow(ratio, mb, a, L))
    for a, L in itertools.product((1, 2), (0, 20_000)):
        E.append(boost_window(a, L))
    for secs, csol in itertools.product((120, 600), (0.5, 2.0)):
        E.append(boost_filtered(secs, csol))
    for a0, a1, r in itertools.product((60, 120), (360, 1440), (1.2, 2.0)):
        E.append(graduate_hold(a0, a1, r))
    for seed, L in itertools.product(range(1, 21), (0, 20_000)):
        E.append(random_entry(seed, L))
    return E


def exits() -> list[Exit]:
    X = []
    for tp, sl, tr, t in itertools.product((0.15, 0.3, 0.6, 1.5, None), (0.1, 0.15, 0.3, 0.5),
                                           (None, 0.2, 0.35), (30, 120, 360)):
        if tp is None and tr is None and t == 360:
            continue
        X.append(Exit(tp, sl, tr, t))
    X.append(Exit(0.3, 0.15, None, 1440))  # Jawad's exits exactly
    X.append(Exit(1.5, 0.4, None, 4320))   # practitioners' "wide stop" momentum exits
    for tp, sl in ((None, 0.2), (0.15, 0.2), (0.3, 0.2)):
        X.append(Exit(tp, sl, None, 5))    # BOOST window: out by ~5 minutes after migration
    return X
