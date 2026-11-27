import asyncio
import shutil
from pathlib import Path

import pytest

from memebot import timeutil
from memebot.config import load_settings
from memebot.notify import Notifier, Outbox
from memebot.storage import DataDir

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def data(tmp_path):
    shutil.copyfile(ROOT / "config" / "settings.json", tmp_path / "settings.json")
    timeutil.set_tz("America/Toronto")
    return DataDir(tmp_path)


@pytest.fixture
def cfg(data):
    return load_settings(data.root / "settings.json")


class FakeMarket:
    def __init__(self):
        self.px = {}
        self.creator = "rugcheck_ok(score=1)"
        self.calls = 0

    def set(self, mint, price, liq=50_000.0):
        self.px[mint] = {"price": price, "liq": liq, "pair": "Pair" + mint, "dex": "pumpswap", "symbol": mint}

    async def prices(self, mints):
        self.calls += 1
        return {m: dict(self.px[m]) for m in mints if m in self.px}

    async def price(self, mint):
        return (await self.prices([mint])).get(mint)

    async def pumpfun(self, mint):
        return None

    async def creator_history(self, mint):
        return self.creator


@pytest.fixture
def market():
    return FakeMarket()


@pytest.fixture
def notifier(data):
    return Notifier(data, "MAIN SIM", Outbox(), tag="PAPER")


def write_watch(data, rows, verdicts):
    import csv

    with open(data.watchlist_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "address", "holders", "top10_pct", "scout_price"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    if verdicts is not None:
        with open(data.verdicts_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["address", "verdict", "time"])
            w.writeheader()
            for a, v in verdicts.items():
                w.writerow({"address": a, "verdict": v, "time": "2026-11-27T12:00:00"})


def row(addr, name=None, price="1.0", holders="1000", top10="15"):
    return {"name": name or addr, "address": addr, "holders": holders, "top10_pct": top10, "scout_price": price}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
