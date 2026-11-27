"""Regression tests for the independent code review (one per finding)."""
import json
from datetime import timedelta

import pytest

from memebot import timeutil
from memebot.config import SettingsError, load_settings
from memebot.engine import Engine
from memebot.executors import SimExecutor
from memebot.net import FetchError
from memebot.storage import DataDir

from .conftest import ROOT, row, write_watch
from .test_live import MINT, FakeJup, FakeRpc, kp, live_cfg, make_live  # noqa: F401


def _armed(e):
    e.cfg.values["live_require_approval"] = False
    e.state["control"]["entries_on"] = True


async def test_1_unknown_execute_never_double_buys_and_adopts(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp, exec_raises=True), FakeRpc(tokens=0)
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    _armed(e)
    await e.tick(force_scan=True)
    assert "BUY_UNKNOWN" in d.alerts_log.read_text()
    jup.exec_raises = False
    for _ in range(3):  # the swap hasn't shown up yet: no second buy
        await e.tick(force_scan=True)
    assert jup.executed == [] and e.state["open"] == []
    rpc.tokens = 2_000_000_000  # it landed
    await e.tick(force_scan=True)
    assert len(e.state["open"]) == 1 and e.state["open"][0]["usd_spent"] == 20
    assert d.unresolved_journal() == [] and jup.executed == []


async def test_1b_unknown_expires_after_grace(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp, exec_raises=True)
    e, d = make_live(live_cfg, tmp_path, market, jup, FakeRpc(tokens=0), kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    _armed(e)
    await e.tick(force_scan=True)
    e.UNKNOWN_GRACE_SECONDS = -1
    await e.reconcile_pending()
    assert d.unresolved_journal() == []


async def test_2a_close_survives_locked_trades_csv(cfg, data, market, notifier, monkeypatch):
    e = Engine(cfg, data, market, SimExecutor(cfg), notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    real = data.append_csv

    def locked(path, *a, **k):
        if path == data.trades_path:
            raise PermissionError("file is open in Excel")
        return real(path, *a, **k)

    monkeypatch.setattr(data, "append_csv", locked)
    market.set("A", 1.31)
    await e.tick()
    assert e.state["open"] == [] and len(e.state["pending_trade_rows"]) == 1  # closed, row queued
    assert "open in Excel" in data.alerts_log.read_text()
    monkeypatch.setattr(data, "append_csv", real)
    await e.tick()
    assert e.state["pending_trade_rows"] == [] and data.read_csv(data.trades_path)[0]["reason"] == "take_profit"


async def test_2b_zero_balance_closes_instead_of_looping(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp), FakeRpc(tokens=2_000_000_000)
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    _armed(e)
    await e.tick(force_scan=True)
    rpc.tokens = 0  # sold elsewhere / during an unclear execute
    market.set(MINT, 0.0131)
    await e.tick()
    assert e.state["open"] == [] and "CHECK_TRADE" in d.alerts_log.read_text()
    assert "estimated" in d.read_csv(d.trades_path)[0]["note"]


async def test_3_rug_pull_still_hits_stop_loss(cfg, data, market, notifier):
    e = Engine(cfg, data, market, SimExecutor(cfg), notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    p = 1.0
    for _ in range(3):
        p *= 0.4  # -60% every tick
        market.set("A", p)
        await e.tick()
    assert e.state["open"] == [] and data.read_csv(data.trades_path)[0]["reason"] == "stop_loss"


async def test_4_pause_during_buy_stops_signing(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp), FakeRpc()
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    _armed(e)
    orig = rpc.sol_balance

    async def pause_mid_buy(owner):
        e.set_paused(True, "telegram")  # user hits Pause while the buy is being prepared
        return await orig(owner)

    rpc.sol_balance = pause_mid_buy
    await e.tick(force_scan=True)
    assert jup.executed == [] and e.state["open"] == []


async def test_5_exit_sells_even_when_price_api_is_down(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp), FakeRpc(tokens=2_000_000_000)
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    _armed(e)
    await e.tick(force_scan=True)

    async def down(mint):
        return None

    async def no_balance(owner, mint):
        raise FetchError("rpc down")

    jup.usd_price = down
    rpc.token_balance = no_balance
    market.set(MINT, 0.0084)
    await e.tick()
    t = d.read_csv(d.trades_path)
    assert e.state["open"] == [] and t[0]["reason"] == "stop_loss" and "estimated" in t[0]["note"]


def test_6_non_sol_quote_mint_rejected(tmp_path):
    s = json.loads((ROOT / "config" / "settings.json").read_text())
    s["live_quote_mint"] = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s))
    with pytest.raises(SettingsError, match="must be SOL"):
        load_settings(p)


async def test_7_blocked_buy_alerts_once_and_is_not_reasked(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp, sell_ok=False)
    e, d = make_live(live_cfg, tmp_path, market, jup, FakeRpc(), kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    e.state["control"]["entries_on"] = True  # approval stays ON
    await e.tick(force_scan=True)
    aid = next(iter(e.approvals))
    e.decide_approval(aid, True, "test")
    await e.tick()
    assert "BUY_BLOCKED" in d.alerts_log.read_text()
    await e.tick(force_scan=True)
    assert len([a for a in e.approvals.values() if a.status == "pending"]) == 0  # not asked again today


def test_9_env_example_has_no_inline_comments():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert "#" not in line.split("=", 1)[1], line
