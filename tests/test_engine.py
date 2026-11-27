"""Engine behaviour: Jawad's rules and P/L math, plus each bot.py bug fix."""
import csv
from datetime import timedelta

import pytest

from memebot import timeutil
from memebot.engine import Engine
from memebot.executors import SimExecutor

from .conftest import row, write_watch


def make(cfg, data, market, notifier):
    return Engine(cfg, data, market, SimExecutor(cfg), notifier)


def trades(data):
    return data.read_csv(data.trades_path)


async def test_open_and_close_tp_math_matches_bot_py(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A", price="0.01")], {"A": "ENTRY OK"})
    market.set("A", 0.01)
    await e.tick(force_scan=True)
    assert len(e.state["open"]) == 1
    p = e.state["open"][0]
    assert p["tp"] == pytest.approx(0.013) and p["sl"] == pytest.approx(0.0085)
    market.set("A", 0.013000000000000001)  # 0.01 * 1.3 in floating point
    await e.tick()
    t = trades(data)
    assert len(t) == 1 and t[0]["reason"] == "take_profit"
    assert float(t[0]["gross_pnl"]) == 6.0 and float(t[0]["net_pnl"]) == 4.2
    assert list(t[0].keys()) == list(__import__("memebot.storage", fromlist=["x"]).TRADE_FIELDS)  # 23 columns
    assert e.state["day_pnl"] == pytest.approx(4.2)


async def test_stop_loss_and_cooldown(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    market.set("A", 0.85)
    await e.tick()
    assert float(trades(data)[0]["net_pnl"]) == -4.8
    market.set("A", 1.0)
    await e.tick(force_scan=True)  # cooldown blocks re-entry
    assert e.state["open"] == []
    assert any("cooldown" in r["reason"] for r in data.read_csv(data.skips_path))


async def test_fail_closed_without_verdicts_and_entry_fail(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A"), row("B")], None)
    market.set("A", 1.0)
    market.set("B", 1.0)
    await e.tick(force_scan=True)
    assert e.state["open"] == []
    write_watch(data, [row("A"), row("B")], {"A": "ENTRY FAIL", "B": "entry ok"})
    await e.tick(force_scan=True)
    assert [p["address"] for p in e.state["open"]] == ["B"]


async def test_max_open_and_daily_loss_cap(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row(f"M{i}") for i in range(7)], {f"M{i}": "ENTRY OK" for i in range(7)})
    for i in range(7):
        market.set(f"M{i}", 1.0)
    await e.tick(force_scan=True)
    assert len(e.state["open"]) == 5
    e2_data = data.__class__(data.root / "x")
    e2_data.save_state({"open": [], "day": timeutil.today(), "day_pnl": -15.0, "skipped_today": []})
    import shutil
    shutil.copy(data.watchlist_path, e2_data.watchlist_path)
    shutil.copy(data.verdicts_path, e2_data.verdicts_path)
    e2 = make(cfg, e2_data, market, notifier)
    await e2.tick(force_scan=True)
    assert e2.state["open"] == []
    assert "DAILY_LOSS_CAP_HIT" in data.alerts_log.read_text()


async def test_bug2_time_exit_fires_even_without_price(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    market.px.clear()  # DexScreener stops returning the coin
    e.state["open"][0]["deadline"] = timeutil.iso(timeutil.now() - timedelta(minutes=61))
    await e.tick()
    t = trades(data)
    assert len(t) == 1 and t[0]["reason"] == "time_exit"
    assert e.state["open"] == []


async def test_bug2_time_exit_waits_for_grace_when_unpriced(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    market.px.clear()
    e.state["open"][0].pop("last_price")
    e.state["open"][0]["deadline"] = timeutil.iso(timeutil.now() - timedelta(minutes=5))
    await e.tick()
    assert len(e.state["open"]) == 1  # inside the 60-minute grace window: wait for a price


async def test_bug4_sheet_printed_once_per_day(cfg, data, market, notifier, capsys):
    from memebot.executors import BuyResult

    class FakeSheet:
        mode = "SHEET"
        n = 0

        async def buy(self, ctx):
            FakeSheet.n += 1
            return BuyResult(ok=False, sheet={"mint": ctx.mint, "verdict": "PRE-TRADE SAFETY PASS"})

    e = Engine(cfg, data, market, FakeSheet(), notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    for _ in range(4):
        await e.tick(force_scan=True)
    assert FakeSheet.n == 1
    assert len(data.read_csv(data.sheets_path)) == 1
    assert e.state["open"] == []


async def test_bug5_skip_dedupe_ignores_changing_numbers(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    for liq in (19_000, 18_500, 17_250, 19_999):
        market.set("A", 1.0, liq=liq)
        await e.tick(force_scan=True)
    skips = data.read_csv(data.skips_path)
    assert len(skips) == 1 and "liquidity below minimum" in skips[0]["reason"]


async def test_extreme_move_needs_two_reads(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    market.set("A", 0.2)  # -80% in one tick: could be a bad print
    await e.tick()
    assert len(e.state["open"]) == 1
    market.set("A", 1.0)  # it was a glitch
    await e.tick()
    assert len(e.state["open"]) == 1 and e.state["open"][0]["low_since_entry"] == 1.0
    market.set("A", 0.2)
    await e.tick()
    market.set("A", 0.19)  # confirmed on the second read: exit
    await e.tick()
    assert trades(data)[0]["reason"] == "stop_loss"


async def test_stop_keeps_managing_and_pause_freezes(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A"), row("B")], {"A": "ENTRY OK", "B": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    e.set_entries(False, "test")
    market.set("B", 1.0)
    await e.tick(force_scan=True)
    assert [p["address"] for p in e.state["open"]] == ["A"]  # Stop: no new entries
    market.set("A", 1.31)
    e.set_paused(True, "test")
    await e.tick()
    assert len(e.state["open"]) == 1  # Pause everything: no exits either
    e.set_paused(False, "test")
    await e.tick()
    assert e.state["open"] == [] and trades(data)[0]["reason"] == "take_profit"


async def test_force_sell_works_even_when_paused(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A", name="DOGGO")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    e.set_paused(True, "test")
    assert "queued" in e.request_force_sell("doggo", "test")
    await e.tick()
    assert trades(data)[0]["reason"] == "manual"


async def test_controls_persist_across_restart(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    e.set_entries(False, "test")
    e2 = make(cfg, data, market, notifier)
    assert e2.entries_on is False


async def test_loop_survives_exceptions(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)

    async def boom(mints):
        raise ConnectionResetError("peer reset")

    market.prices = boom
    for _ in range(3):
        await e.tick()
    assert e.health.errors_in_row == 3 and "ConnectionResetError" in e.health.last_error
    assert "failed ticks in a row" in data.alerts_log.read_text()


async def test_legacy_state_with_naive_timestamps(cfg, data, market, notifier):
    import json
    pos = {"address": "A", "name": "A", "entry": 1.0, "tp": 1.3, "sl": 0.85, "opened": "2026-11-27T12:57:46",
           "deadline": "2026-11-28T12:57:46", "liq_at_entry": 1.0, "dex_pair": "", "holders": 1.0, "top10_pct": 1.0,
           "scout_price": 1.0, "pf_price": "blind", "pf_mcap": "blind", "pf_vs_dex_pct": "blind",
           "auditor_verdict": "ENTRY OK", "auditor_verdict_time": "", "creator_history": "blind",
           "high_since_entry": 1.0, "low_since_entry": 1.0}
    data.state_path.write_text(json.dumps({"open": [pos], "day": timeutil.today(), "day_pnl": 0.0,
                                            "skipped_today": [], "cooldowns": {"Z": "2020-01-01T00:00:00"}}))
    e = make(cfg, data, market, notifier)
    market.set("A", 1.05)
    await e.tick(force_scan=True)
    assert len(e.state["open"]) == 1 and e.state["cooldowns"] == {}


async def test_status_shape(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    st = e.status()
    for k in ("state", "open", "closed", "target_closed", "today_pnl", "realized_pnl", "label"):
        assert k in st
    assert st["state"] == "ON" and st["label"] == "PAPER"


async def test_sheet_blocked_verdict_gets_no_sign_alert(cfg, data, market, notifier):
    from memebot.executors import BuyResult

    class FakeSheet:
        mode = "SHEET"

        async def buy(self, ctx):
            return BuyResult(ok=False, sheet={"mint": ctx.mint, "verdict": "DO NOT TRADE: price mismatch 4.91% > 3%"})

    e = Engine(cfg, data, market, FakeSheet(), notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(force_scan=True)
    assert not data.sheets_path.exists()
    assert not data.alerts_log.exists() or "TRADE_SHEET" not in data.alerts_log.read_text()
    assert e.state["sheets_printed"] == {}  # may still pass later today
    assert "DO NOT TRADE" in data.read_csv(data.skips_path)[0]["reason"]


async def test_max_open_leaves_a_skip_row(cfg, data, market, notifier):
    e = make(cfg, data, market, notifier)
    write_watch(data, [row(f"M{i}") for i in range(7)], {f"M{i}": "ENTRY OK" for i in range(7)})
    for i in range(7):
        market.set(f"M{i}", 1.0)
    await e.tick(force_scan=True)
    reasons = [r["reason"] for r in data.read_csv(data.skips_path)]
    assert reasons.count("max open positions reached (5)") == 2
    assert e.state["open"][0]["tp"] == 1.3 and e.state["open"][0]["sl"] == 0.85


async def test_bug7_exits_checked_every_exit_poll_not_every_scan(cfg, data, market, notifier):
    """bot.py checked exits only every poll_seconds (300 s). Exits now run every tick
    (exit_poll_seconds, 15 s) while new-entry scans still follow poll_seconds."""
    e = make(cfg, data, market, notifier)
    write_watch(data, [row("A")], {"A": "ENTRY OK"})
    market.set("A", 1.0)
    await e.tick(mono_now=0.0)  # scan at t=0 opens A
    assert len(e.state["open"]) == 1 and e._next_scan == cfg["poll_seconds"]
    scans = []
    orig = e.scan

    async def counting_scan():
        scans.append(1)
        await orig()

    e.scan = counting_scan
    market.set("A", 0.84)
    await e.tick(mono_now=15.0)  # 15 s later: no scan due, but the stop-loss fires
    assert e.state["open"] == [] and scans == []
    await e.tick(mono_now=300.0)
    assert scans == [1]
    assert cfg["exit_poll_seconds"] == 15 and cfg["poll_seconds"] == 300
