"""Run the whole strategy matrix with an honest selection procedure.

    python -m research.run_backtest --data research/data --out research/results

1. Pools are ordered by creation time. The first 60% are the TRAIN period, the last 40% the TEST
   period (strictly later in time, never seen during selection).
2. Every strategy (entry x exit) is run on both, at measured costs and at 2x costs (stress).
3. Candidates are chosen on TRAIN only: at least --min-train trades, ranked by t-statistic of P/L.
4. The top --top candidates are judged on TEST with a one-sided bootstrap p-value of mean P/L > 0,
   Holm-corrected for the number of candidates. A strategy "passes" only if: TEST mean > 0, Holm
   p < 0.05, the 2x-cost TEST mean > 0, and at least --min-test TEST trades.
5. The Probability of Backtest Overfitting (CSCV, 8 time blocks) is reported for the whole matrix.
6. A random-entry control shows what costs and exits do with no signal at all.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .backtest import Costs, bootstrap_p_mean_le_0, holm, load_pools, pbo_cscv, simulate_many
from .strategies import entries, exits

N_BLOCKS = 8
_POOLS = None


def _init(data_dir: str, min_candles: int, dex: str = ""):
    global _POOLS
    _POOLS = _filter(load_pools(data_dir, min_candles), dex)


def _filter(pools, dex: str):
    want = {d.strip() for d in dex.split(",") if d.strip()}
    return [p for p in pools if not want or p.dex in want]


def _run_entry(ei: int):
    E = entries()[ei]
    X = exits()
    pools = _POOLS
    n = len(pools)
    split = int(n * 0.6)
    base, stress = Costs(), Costs(mult=2.0)
    # per exit: lists of (net, set, block)
    res = {xi: {"tr": [], "te": [], "te2": [], "blk": [[] for _ in range(N_BLOCKS)], "reasons": {}} for xi in range(len(X))}
    for pi, p in enumerate(pools):
        i = E.fn(p)
        if i is None:
            continue
        a = simulate_many(p, i, X, base)
        if a is None:
            continue
        b = simulate_many(p, i, X, stress) if pi >= split else None
        blk = min(N_BLOCKS - 1, pi * N_BLOCKS // n)
        for xi, (net, reason, _j) in enumerate(a):
            r = res[xi]
            (r["tr"] if pi < split else r["te"]).append(net)
            if b is not None:
                r["te2"].append(b[xi][0])
            r["blk"][blk].append(net)
            r["reasons"][reason] = r["reasons"].get(reason, 0) + 1
    rows = []
    for xi, r in res.items():
        rows.append({"entry_i": ei, "exit_i": xi, "family": E.family, "entry": E.key(), "exit": X[xi].key(),
                     "tr": r["tr"], "te": r["te"], "te2": r["te2"],
                     "blk": [float(np.mean(b)) if b else float("nan") for b in r["blk"]], "reasons": r["reasons"]})
    return rows


def summ(x):
    x = np.asarray(x, float)
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": float("nan"), "total": 0.0, "win": float("nan"), "t": float("nan"), "pf": float("nan")}
    sd = x.std(ddof=1) if n > 1 else 0.0
    loss = -x[x <= 0].sum()
    return {"n": n, "mean": float(x.mean()), "total": float(x.sum()), "win": float((x > 0).mean()),
            "t": float(x.mean() / sd * math.sqrt(n)) if sd > 0 else 0.0,
            "pf": float(x[x > 0].sum() / loss) if loss > 0 else float("inf")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="research/data")
    ap.add_argument("--out", default="research/results")
    ap.add_argument("--min-candles", type=int, default=10)
    ap.add_argument("--min-train", type=int, default=30)
    ap.add_argument("--min-test", type=int, default=30)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--dex", default="", help="only these venues, e.g. pumpswap or pump-fun,meteora-dbc")
    a = ap.parse_args(argv)
    t0 = time.time()
    pools = _filter(load_pools(a.data, a.min_candles), a.dex)
    from collections import Counter
    print("venues:", dict(Counter(p.dex for p in pools)), flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    E, X = entries(), exits()
    print(f"{len(pools)} pools, {len(E)} entries x {len(X)} exits = {len(E) * len(X)} strategies", flush=True)
    rows = []
    with ProcessPoolExecutor(a.workers, initializer=_init, initargs=(a.data, a.min_candles, a.dex)) as ex:
        for r in ex.map(_run_entry, range(len(E))):
            rows.extend(r)
    table = []
    for r in rows:
        tr, te, te2 = summ(r["tr"]), summ(r["te"]), summ(r["te2"])
        table.append({"family": r["family"], "entry": r["entry"], "exit": r["exit"],
                      **{f"train_{k}": v for k, v in tr.items()}, **{f"test_{k}": v for k, v in te.items()},
                      "test2x_mean": te2["mean"], "test2x_total": te2["total"],
                      "reasons": json.dumps(r["reasons"]), "_te": r["te"], "_blk": r["blk"]})
    # ---- selection on TRAIN only
    cands = [t for t in table if t["train_n"] >= a.min_train and t["family"] != "random"]
    cands.sort(key=lambda t: t["train_t"], reverse=True)
    top = cands[:a.top]
    pv = [bootstrap_p_mean_le_0(np.asarray(t["_te"])) if t["test_n"] >= 5 else 1.0 for t in top]
    adj = holm(pv)
    for t, p_raw, p_adj in zip(top, pv, adj):
        t["test_p"], t["test_p_holm"] = p_raw, p_adj
        t["passes"] = bool(t["test_n"] >= a.min_test and t["test_mean"] > 0 and p_adj < 0.05
                           and t["test2x_mean"] == t["test2x_mean"] and t["test2x_mean"] > 0)
    # ---- overfitting across the whole matrix
    perf = np.array([t["_blk"] for t in table if t["train_n"] + t["test_n"] >= a.min_train and t["family"] != "random"])
    pbo = pbo_cscv(perf, N_BLOCKS) if len(perf) else float("nan")
    # ---- controls and families
    rand = [t for t in table if t["family"] == "random"]
    # Per-exit random baseline (20 seeds): a strategy only matters if it beats random entries with the SAME exit.
    rand_by_exit: dict[str, list[float]] = {}
    for t in rand:
        if t["test_n"]:
            rand_by_exit.setdefault(t["exit"], []).append(t["test_mean"])
    for t in top:
        rb = rand_by_exit.get(t["exit"], [])
        t["random_same_exit_test_mean"] = float(np.mean(rb)) if rb else float("nan")
        t["beats_random_seeds"] = int(sum(t["test_mean"] > x for x in rb))
        x = np.sort(np.asarray(t["_te"], float))
        cut = max(1, int(math.ceil(0.01 * len(x)))) if len(x) else 0
        t["test_mean_without_top1pct"] = float(x[:-cut].mean()) if len(x) > cut else float("nan")
        t["passes"] = (t["passes"] and bool(rb) and t["beats_random_seeds"] >= int(0.95 * len(rb))
                       and t["test_mean_without_top1pct"] > 0)
    fam = {}
    for t in table:
        if t["test_n"] >= 10:
            f = fam.setdefault(t["family"], {"strategies": 0, "test_mean_pos": 0, "best_test_mean": -1e9})
            f["strategies"] += 1
            f["test_mean_pos"] += t["test_mean"] > 0
            f["best_test_mean"] = max(f["best_test_mean"], t["test_mean"])
    keys = [k for k in table[0] if not k.startswith("_")]
    with open(out / "all_strategies.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys + ["test_p", "test_p_holm", "passes"], extrasaction="ignore")
        w.writeheader()
        for t in sorted(table, key=lambda t: -(t["train_t"] if t["train_t"] == t["train_t"] else -1e9)):
            w.writerow(t)
    from collections import Counter as _C
    summary = {"venues": dict(_C(p.dex for p in pools)), "dex_filter": a.dex, "pools": len(pools), "train_pools": int(len(pools) * 0.6), "strategies": len(table),
               "candidates_considered": len(cands), "pbo": pbo, "families": fam,
               "random_control_test_mean": float(np.nanmean([t["test_mean"] for t in rand])) if rand else None,
               "top": [{k: v for k, v in t.items() if not k.startswith("_")} for t in top],
               "passed": [t["entry"] + " | " + t["exit"] for t in top if t.get("passes")],
               "seconds": round(time.time() - t0, 1)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: summary[k] for k in ("pools", "strategies", "candidates_considered", "pbo",
                                                "random_control_test_mean", "passed", "seconds")}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
