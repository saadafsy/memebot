"""live_trader.py follows Jawad's INTERFACE_CONTRACT.md return shapes and fails closed."""
import json
import sys

import pytest

from memebot.config import load_env
from memebot.executors import LiveExecutor
from memebot.storage import DataDir

from .conftest import ROOT
from .test_live import MINT, FakeJup, FakeRpc, kp, live_cfg  # noqa: F401

sys.path.insert(0, str(ROOT))
import live_trader  # noqa: E402

BUY_KEYS = {"ok", "fill_price", "tokens_received", "fee_usd", "tx_sig", "error"}
SELL_KEYS = {"ok", "fill_price", "usd_received", "fee_usd", "tx_sig", "error"}


def trader(live_cfg, tmp_path, market, jup, rpc, kp):
    d = DataDir(tmp_path / "live")
    return live_trader.LiveTrader(live_cfg, LiveExecutor(live_cfg, jup, rpc, kp, d), market)


async def test_buy_and_sell_return_contract_dicts(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp), FakeRpc(tokens=2_000_000_000)
    market.set(MINT, 0.01)
    t = trader(live_cfg, tmp_path, market, jup, rpc, kp)
    b = await t.abuy(MINT, position_size_usd=20, ref_price=0.01, max_price_mismatch_pct=3)
    assert set(b) == BUY_KEYS and b["ok"] and b["tx_sig"] == "SIGreq-buy" and b["tokens_received"] > 0
    s = await t.asell(MINT, tokens_received=b["tokens_received"], ref_price=0.013, reason="take_profit")
    assert set(s) == SELL_KEYS and s["ok"] and s["tx_sig"] == "SIGreq-sell" and s["usd_received"] > 0


async def test_buy_fails_closed(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp, sell_ok=False)
    market.set(MINT, 0.01)
    t = trader(live_cfg, tmp_path, market, jup, FakeRpc(), kp)
    b = await t.abuy(MINT)
    assert b["ok"] is False and "sell route" in b["error"] and jup.executed == []
    market.creator = "blind"
    assert "blind" in (await t.abuy(MINT))["error"]
    assert "must match" in (await t.abuy(MINT, position_size_usd=500))["error"]
    assert "looser" in (await t.abuy(MINT, max_price_mismatch_pct=50))["error"]


def test_from_env_refuses_when_not_armed(tmp_path):
    (tmp_path / "settings.json").write_bytes((ROOT / "config" / "settings.json").read_bytes())
    with pytest.raises(live_trader.NotArmed, match="mode is not LIVE"):
        live_trader.LiveTrader.from_env(tmp_path, env=load_env({}))


def test_entry_points_exist():
    import importlib.util
    for f in ("app.py", "commands.py", "live_trader.py"):
        assert importlib.util.spec_from_file_location(f[:-3], ROOT / f) is not None
