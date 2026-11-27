"""The backtest engine must not flatter results: no look-ahead, stop beats target in the same candle,
gaps fill at the open, costs always subtract, and the fast simulator equals the simple one."""
import sys

import numpy as np
import pytest

from .conftest import ROOT

sys.path.insert(0, str(ROOT))
from research.backtest import Costs, Exit, Pool, holm, pbo_cscv, simulate, simulate_many, stats  # noqa: E402
from research.strategies import entries, exits  # noqa: E402

ZERO = Costs(base_side=0.0, impact_k=0.0, stop_extra=0.0, network_usd=0.0)


def pool(rows, liq=50_000.0):
    a = np.asarray(rows, float)
    n = len(a)
    t = np.arange(n) * 60 + 1_000_000
    return Pool("P", "pumpswap", int(t[0]), t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, 100.0),
                np.full(n, liq), np.full(n, np.nan), np.full(n, np.nan))


def test_entry_is_next_open_not_signal_close():
    p = pool([[1, 1, 1, 1], [2, 2, 2, 2], [2, 2.1, 2, 2]])
    tr = simulate(p, 0, Exit(tp=None, sl=None, trail=None, max_min=1), ZERO)
    assert tr.entry_px == 2.0  # filled at candle 1's open, not candle 0's close of 1.0


def test_stop_wins_when_candle_hits_both():
    p = pool([[1, 1, 1, 1], [1, 2.0, 0.5, 1]])
    tr = simulate(p, 0, Exit(tp=0.3, sl=0.15), ZERO)
    assert tr.reason == "stop" and tr.exit_px == pytest.approx(0.85)


def test_gap_through_stop_fills_at_open():
    p = pool([[1, 1, 1, 1], [1, 1, 1, 1], [0.4, 0.45, 0.3, 0.35]])
    tr = simulate(p, 0, Exit(tp=0.3, sl=0.15), ZERO)
    assert tr.reason == "stop" and tr.exit_px == 0.4  # not 0.85


def test_trailing_uses_prior_highs_only():
    p = pool([[1, 1, 1, 1], [1, 1, 1, 1], [1, 2.0, 1.55, 1.9], [1.9, 1.9, 1.5, 1.5]])
    tr = simulate(p, 0, Exit(tp=None, sl=0.5, trail=0.2), ZERO)
    # candle 2's own high (2.0) must not stop it out at 1.6 in the same candle
    assert tr.exit_i == 3 and tr.exit_px == pytest.approx(1.6)


def test_costs_always_subtract():
    p = pool([[1, 1, 1, 1], [1, 1, 1, 1], [1, 1, 1, 1]])
    tr = simulate(p, 0, Exit(tp=None, sl=None, max_min=1), Costs())
    assert tr.gross == 0 and tr.net_usd < -0.3  # about 1.7% of $20 plus network


def test_dead_pool_exits_at_last_price():
    p = pool([[1, 1, 1, 1], [1, 1, 1, 1], [0.02, 0.02, 0.01, 0.01]])
    tr = simulate(p, 0, Exit(tp=None, sl=None, max_min=10_000), ZERO)
    assert tr.reason == "data_end" and tr.exit_px == 0.01


def test_vectorized_equals_scalar_on_random_paths():
    rng = np.random.default_rng(3)
    X = exits()
    for _ in range(25):
        n = 200
        c = np.exp(np.cumsum(rng.normal(0, 0.06, n)))
        o = np.concatenate(([c[0]], c[:-1]))
        h = np.maximum(o, c) * (1 + rng.uniform(0, 0.05, n))
        l = np.minimum(o, c) * (1 - rng.uniform(0, 0.05, n))
        p = pool(np.column_stack([o, h, l, c]))
        i = int(rng.integers(0, 50))
        many = simulate_many(p, i, X, Costs())
        for ex, (net, reason, j) in zip(X, many):
            s = simulate(p, i, ex, Costs())
            assert (round(s.net_usd, 9), s.reason, s.exit_i) == (round(net, 9), reason, j), ex


def test_entry_rules_never_look_ahead():
    """Changing candles after the signal must not change the signal."""
    rng = np.random.default_rng(5)
    for _ in range(10):
        n = 300
        c = np.exp(np.cumsum(rng.normal(0, 0.05, n)))
        rows = np.column_stack([c, c * 1.02, c * 0.98, c])
        p = pool(rows)
        p.buys5[:] = rng.integers(0, 60, n)
        p.sells5[:] = rng.integers(0, 60, n)
        for e in entries():
            i = e.fn(p)
            if i is None or i >= n - 2:
                continue
            q = pool(np.vstack([rows[:i + 1], rows[i + 1:] * 50]))
            q.buys5[:], q.sells5[:] = p.buys5, p.sells5
            assert e.fn(q) == i, e.key()


def test_matrix_size_and_stats_helpers():
    assert len(entries()) * len(exits()) > 10_000
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    perf = np.vstack([np.random.default_rng(i).normal(0, 1, 8) for i in range(30)])
    assert 0.0 <= pbo_cscv(perf) <= 1.0
    assert stats([])["n"] == 0
