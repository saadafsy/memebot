"""Forward data collector for unbiased backtests (read-only public APIs, no keys, no wallet).

Survivorship bias is the main trap in memecoin backtests: lists of "pools that exist today" or
"top pools" only contain coins that survived. This collector records EVERY new Solana pool as it
appears (GeckoTerminal new_pools), including the ones that die minutes later, then fetches their
1-minute OHLCV once they are old enough. It also records a periodic DexScreener snapshot of
liquidity and buy/sell counts for each pool, which OHLCV does not contain.

    python research/collect.py --hours 3 --out research/data        # forward collection
    python research/collect.py --ohlcv-only --out research/data     # (re)fetch candles for seen pools

Output (append-only JSONL, safe to stop and restart):
  pools.jsonl      one line per pool first seen: address, base mint, dex, created_at, seen_at, reserve, fdv
  snapshots.jsonl  DexScreener snapshots: liquidity, volume, txns (buys/sells), price, at a timestamp
  ohlcv/<pool>.json  1-minute candles [ts, o, h, l, c, volume_usd], oldest first
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

GT = "https://api.geckoterminal.com/api/v2"
# With a CoinGecko API key (free Demo plan, about 30 calls/min), the same on-chain data comes from
# CoinGecko's API instead of GeckoTerminal's keyless limit of about 5 calls/min. The key is read
# from the environment only (CG_API_KEY) and sent as a header, never written to disk or logs.
CG_KEY = os.environ.get("CG_API_KEY", "").strip()
CG_PRO = os.environ.get("CG_API_PLAN", "demo").strip().lower() == "pro"
if CG_KEY:
    GT = ("https://pro-api.coingecko.com" if CG_PRO else "https://api.coingecko.com") + "/api/v3/onchain"
DS = "https://api.dexscreener.com"
HDR = {"Accept": "application/json;version=20230302", "User-Agent": "memebot-research/1.0"}
if CG_KEY:
    HDR["x-cg-pro-api-key" if CG_PRO else "x-cg-demo-api-key"] = CG_KEY


class Paced:
    """GeckoTerminal's public API allows about 30 calls a minute; stay under it."""

    def __init__(self, interval: float):
        self.interval, self.next_ok = interval, 0.0

    def wait(self) -> None:
        d = self.next_ok - time.monotonic()
        if d > 0:
            time.sleep(d)
        self.next_ok = time.monotonic() + self.interval


def get(client: httpx.Client, url: str, pace: Paced, params=None, tries: int = 5):
    for i in range(tries):
        pace.wait()
        try:
            r = client.get(url, params=params, headers=HDR, timeout=30)
        except httpx.HTTPError as e:
            print(f"  net error {type(e).__name__}, retry", file=sys.stderr)
            time.sleep(2 * (i + 1))
            continue
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After") or 15))
            continue
        if r.status_code >= 500:
            time.sleep(3 * (i + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_seen(out: Path) -> dict[str, dict]:
    seen = {}
    p = out / "pools.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                d = json.loads(line)
                seen[d["pool"]] = d
            except (ValueError, KeyError):
                pass
    return seen


def poll_new_pools(client, gt_pace, out: Path, seen: dict, pages: int) -> int:
    added = 0
    with open(out / "pools.jsonl", "a") as f:
        for page in range(1, pages + 1):
            d = get(client, f"{GT}/networks/solana/new_pools", gt_pace, {"page": page, "include": "base_token,dex"})
            if not d:
                break
            for p in d.get("data", []):
                a = p.get("attributes", {})
                rel = p.get("relationships", {})
                pool = a.get("address")
                if not pool or pool in seen:
                    continue
                base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                rec = {"pool": pool, "base_mint": base_id.split("_", 1)[-1], "name": a.get("name"),
                       "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
                       "created_at": a.get("pool_created_at"), "seen_at": now_iso(),
                       "reserve_usd": a.get("reserve_in_usd"), "fdv_usd": a.get("fdv_usd"),
                       "base_price_usd": a.get("base_token_price_usd")}
                seen[pool] = rec
                f.write(json.dumps(rec) + "\n")
                added += 1
    return added


def snapshot(client, ds_pace, out: Path, pools: list[dict]) -> int:
    """DexScreener pair snapshots (liquidity + buy/sell counts), 30 pairs per call."""
    n = 0
    with open(out / "snapshots.jsonl", "a") as f:
        for i in range(0, len(pools), 30):
            chunk = [p["pool"] for p in pools[i:i + 30]]
            d = get(client, f"{DS}/latest/dex/pairs/solana/{','.join(chunk)}", ds_pace)
            for pr in (d or {}).get("pairs") or []:
                f.write(json.dumps({"pool": pr.get("pairAddress"), "at": now_iso(),
                                    "price_usd": pr.get("priceUsd"), "liq_usd": (pr.get("liquidity") or {}).get("usd"),
                                    "fdv": pr.get("fdv"), "txns": pr.get("txns"), "volume": pr.get("volume"),
                                    "base": (pr.get("baseToken") or {}).get("address")}) + "\n")
                n += 1
    return n


def fetch_ohlcv(client, gt_pace, out: Path, pool: str) -> int:
    """All 1-minute candles since creation (GeckoTerminal returns up to 1000 per call, newest first)."""
    dst = out / "ohlcv" / f"{pool}.json"
    candles: dict[int, list] = {}
    before = None
    for _ in range(10):  # up to ~10,000 minutes (~7 days)
        params = {"aggregate": 1, "limit": 1000, "currency": "usd"}
        if before:
            params["before_timestamp"] = before
        d = get(client, f"{GT}/networks/solana/pools/{pool}/ohlcv/minute", gt_pace, params)
        rows = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            break
        for r in rows:
            candles[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
        oldest = min(int(r[0]) for r in rows)
        if len(rows) < 1000 or (before is not None and oldest >= before):
            break
        before = oldest
    if candles:
        dst.write_text(json.dumps([candles[k] for k in sorted(candles)]))
    return len(candles)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3.0, help="how long to watch for new pools")
    ap.add_argument("--every", type=float, default=240, help="seconds between new-pool polls")
    ap.add_argument("--pages", type=int, default=10, help="new_pools pages per poll (20 pools each)")
    ap.add_argument("--min-age-min", type=float, default=180, help="fetch candles once a pool is this old")
    ap.add_argument("--out", default="research/data")
    ap.add_argument("--ohlcv-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "ohlcv").mkdir(parents=True, exist_ok=True)
    gt_pace, ds_pace = Paced(2.2 if CG_KEY else 12.0), Paced(0.25)
    print(f"on-chain data source: {'CoinGecko API (key from CG_API_KEY)' if CG_KEY else 'GeckoTerminal keyless'}", flush=True)
    seen = load_seen(out)
    with httpx.Client() as client:
        if not a.ohlcv_only:
            t_end = time.time() + a.hours * 3600
            while time.time() < t_end:
                added = poll_new_pools(client, gt_pace, out, seen, a.pages)
                recent = [p for p in seen.values()]
                ns = snapshot(client, ds_pace, out, recent[-600:])
                print(f"{now_iso()} pools={len(seen)} (+{added}) snapshots+={ns}", flush=True)
                time.sleep(max(0, a.every - 30))
            # Let the youngest pools age so every pool has at least --min-age-min of history; keep
            # snapshotting liquidity and buy/sell counts meanwhile.
            settle_until = time.time() + a.min_age_min * 60
            while time.time() < settle_until:
                ns = snapshot(client, ds_pace, out, list(seen.values())[-600:])
                print(f"{now_iso()} settling, snapshots+={ns}", flush=True)
                time.sleep(max(0, min(a.every, settle_until - time.time())))
        done = {p.stem for p in (out / "ohlcv").glob("*.json")}
        todo = [p for p in seen.values() if p["pool"] not in done]
        print(f"fetching candles for {len(todo)} pools", flush=True)
        for i, p in enumerate(todo, 1):
            n = fetch_ohlcv(client, gt_pace, out, p["pool"])
            if i % 25 == 0:
                print(f"  {i}/{len(todo)} last={n} candles", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
