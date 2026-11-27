"""Importing Jawad's existing sim-bot / sim-fast folders keeps the closed count and open positions."""
import csv
import json
import shutil

from memebot.__main__ import main
from memebot.config import load_settings
from memebot.engine import Engine
from memebot.executors import SimExecutor
from memebot.notify import Notifier, Outbox
from memebot.storage import TRADE_FIELDS, DataDir

from .conftest import ROOT, FakeMarket


def legacy_folder(path, lane=None, closed=37, open_n=5):
    path.mkdir()
    s = json.loads((ROOT / "config" / "settings.json").read_text())
    if lane:
        s.update(lane=lane, time_exit_hours=4)
    (path / "settings.json").write_text(json.dumps(s, indent=2))
    with open(path / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for i in range(closed):
            w.writerow({k: "" for k in TRADE_FIELDS} | {"name": f"C{i}", "address": f"A{i}", "net_pnl": "-1.2",
                                                         "gross_pnl": "0.6", "reason": "time_exit"})
    pos = [{"address": f"M{i}", "name": f"M{i}", "entry": 1.0, "tp": 1.3, "sl": 0.85,
            "opened": "2026-11-27T12:57:46", "deadline": "2026-11-28T12:57:46", "liq_at_entry": 1.0,
            "dex_pair": "", "holders": 1.0, "top10_pct": 1.0, "scout_price": 1.0, "pf_price": "blind",
            "pf_mcap": "blind", "pf_vs_dex_pct": "blind", "auditor_verdict": "ENTRY OK",
            "auditor_verdict_time": "", "creator_history": "blind", "high_since_entry": 1.0,
            "low_since_entry": 1.0} for i in range(open_n)]
    (path / "state.json").write_text(json.dumps({"open": pos, "day": "2026-11-27", "day_pnl": -3.5,
                                                 "skipped_today": [], "cooldowns": {"X": "2026-11-28T09:00:00"},
                                                 "loss_cap_alerted_day": ""}))
    (path / "watchlist.csv").write_text("name,address,holders,top10_pct,scout_price\n")
    (path / "auditor_verdicts.csv").write_text("address,verdict,time\n")
    return path


async def test_import_main_and_fast_lane_carry_on(tmp_path, capsys):
    main_src = legacy_folder(tmp_path / "sim-bot")
    fast_src = legacy_folder(tmp_path / "sim-fast", lane="FAST LANE", closed=12, open_n=2)
    before = {p: p.read_bytes() for p in main_src.iterdir()}
    assert main(["import", "--from", str(main_src), "--data", str(tmp_path / "data")]) == 0
    assert "closed trades: 37/100   open positions: 5" in capsys.readouterr().out
    assert main(["import", "--from", str(fast_src), "--data", str(tmp_path / "data-fast")]) == 0
    out = capsys.readouterr().out
    assert "lane FAST LANE, time exit 4" in out and "closed trades: 12/100   open positions: 2" in out
    assert {p: p.read_bytes() for p in main_src.iterdir()} == before  # source untouched
    d = DataDir(tmp_path / "data")
    cfg = load_settings(d.root / "settings.json")
    e = Engine(cfg, d, FakeMarket(), SimExecutor(cfg), Notifier(d, outbox=Outbox()))
    st = e.status()
    assert st["closed"] == 37 and st["open_count"] == 5 and e.state["cooldowns"]


def test_import_refuses_to_overwrite(tmp_path, capsys):
    src = legacy_folder(tmp_path / "sim-bot")
    dst = tmp_path / "data"
    assert main(["import", "--from", str(src), "--data", str(dst)]) == 0
    assert main(["import", "--from", str(src), "--data", str(dst)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_import_rejects_corrupt_state(tmp_path, capsys):
    src = legacy_folder(tmp_path / "sim-bot")
    (src / "state.json").write_text("{broken")
    assert main(["import", "--from", str(src), "--data", str(tmp_path / "data")]) == 2
    assert "did not validate" in capsys.readouterr().err
