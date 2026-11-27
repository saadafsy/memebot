"""
Automated unit test suite for /workspace/sim-bot/bot.py.
Runs in an isolated temp directory — never modifies /workspace/sim-bot/.
"""
import csv
import importlib.util
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


def load_bot_module(tmpdir):
    """Load /workspace/sim-bot/bot.py with cwd pointed at tmpdir."""
    orig_cwd = os.getcwd()
    # Copy settings.json into tmpdir so bot.py loads real settings without touching /workspace/sim-bot
    with open("/workspace/sim-bot/settings.json", "r") as src:
        cfg = json.load(src)
    with open(os.path.join(tmpdir, "settings.json"), "w") as dst:
        json.dump(cfg, dst)
    os.chdir(tmpdir)
    try:
        spec = importlib.util.spec_from_file_location("sim_bot_under_test", "/workspace/sim-bot/bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(orig_cwd)


class TestSimBot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_cwd = os.getcwd()
        self.bot = load_bot_module(self.tmpdir.name)
        os.chdir(self.tmpdir.name)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        self.tmpdir.cleanup()

    def _sample_row(self, addr="Mint111pump", name="TEST", price="0.01", holders="1000", top10="15"):
        return {
            "address": addr,
            "name": name,
            "scout_price": price,
            "holders": holders,
            "top10_pct": top10,
        }

    def _sample_live(self, price=0.01, liq=50000.0):
        return {"price": price, "liq": liq, "pair": "Pair111", "dex": "pumpswap"}

    def test_passes_filters_valid(self):
        self.assertIsNone(self.bot.passes_filters(self._sample_row(), self._sample_live()))

    def test_passes_filters_rejects_low_holders(self):
        why = self.bot.passes_filters(self._sample_row(holders="499"), self._sample_live())
        self.assertIn("holders below minimum", why)

    def test_passes_filters_rejects_high_top10(self):
        why = self.bot.passes_filters(self._sample_row(top10="30"), self._sample_live())
        self.assertIn("top-10 too high", why)

    def test_passes_filters_rejects_low_liquidity(self):
        why = self.bot.passes_filters(self._sample_row(), self._sample_live(liq=19999.99))
        self.assertIn("liquidity below minimum", why)

    def test_passes_filters_rejects_price_mismatch_over_3pct(self):
        why = self.bot.passes_filters(self._sample_row(price="0.01"), self._sample_live(price=0.01035))
        self.assertIn("live price differs", why)

    def test_passes_filters_rejects_nan_and_blank(self):
        why = self.bot.passes_filters(self._sample_row(holders="nan"), self._sample_live())
        self.assertIn("blind", why)
        why2 = self.bot.passes_filters(self._sample_row(price=""), self._sample_live())
        self.assertIn("blind", why2)

    def test_open_and_close_tp_math(self):
        s = self.bot.load_state()
        row = self._sample_row()
        live = self._sample_live(price=0.01)
        pf = {"pf_price": 0.0101, "pf_mcap": 1000000.0}
        v = {"verdict": "ENTRY OK", "time": "2026-11-27T12:00:00"}
        self.bot.open_position(s, row, live, pf=pf, verdict_info=v)
        self.assertEqual(len(s["open"]), 1)
        pos = s["open"][0]
        self.assertAlmostEqual(pos["tp"], 0.013, places=8)
        self.assertAlmostEqual(pos["sl"], 0.0085, places=8)
        self.assertEqual(pos["pf_vs_dex_pct"], 1.0)
        self.assertEqual(pos["auditor_verdict"], "ENTRY OK")

        # Close at exact TP (+30% on $20 = +$6.00 gross, -$1.80 fee = +$4.20 net)
        self.bot.close_position(s, pos, 0.013, "take_profit")
        self.assertEqual(len(s["open"]), 0)
        self.assertAlmostEqual(s["day_pnl"], 4.20, places=4)
        with open(self.bot.TRADES, "r", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["gross_pnl"]), 6.0)
        self.assertEqual(float(rows[0]["net_pnl"]), 4.2)
        self.assertEqual(rows[0]["reason"], "take_profit")

    def test_close_sl_math(self):
        s = self.bot.load_state()
        self.bot.open_position(s, self._sample_row(), self._sample_live(price=0.01))
        pos = s["open"][0]
        # Close at exact SL (-15% on $20 = -$3.00 gross, -$1.80 fee = -$4.80 net)
        self.bot.close_position(s, pos, 0.0085, "stop_loss")
        self.assertAlmostEqual(s["day_pnl"], -4.80, places=4)

    def test_live_mode_blocks_blind_creator_history(self):
        s = self.bot.load_state()
        orig_mode = self.bot.CFG["mode"]
        self.bot.CFG["mode"] = "LIVE"
        try:
            self.bot.open_position(s, self._sample_row(), self._sample_live())
            self.assertEqual(len(s["open"]), 0)
            self.assertTrue(os.path.exists(self.bot.SKIPS))
        finally:
            self.bot.CFG["mode"] = orig_mode

    def test_manage_triggers_tp_sl_and_time_exit(self):
        s = self.bot.load_state()
        self.bot.open_position(s, self._sample_row(addr="A", name="A"), self._sample_live(price=1.0))
        self.bot.open_position(s, self._sample_row(addr="B", name="B"), self._sample_live(price=1.0))
        self.bot.open_position(s, self._sample_row(addr="C", name="C"), self._sample_live(price=1.0))
        # Force C's deadline into the past
        s["open"][2]["deadline"] = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")

        prices = {
            "A": {"price": 1.31, "liq": 50000.0},
            "B": {"price": 0.84, "liq": 50000.0},
            "C": {"price": 1.05, "liq": 50000.0},
        }
        with patch.object(self.bot, "fetch_price", side_effect=lambda addr: prices[addr]):
            self.bot.manage(s)

        self.assertEqual(len(s["open"]), 0)
        with open(self.bot.TRADES, "r", newline="") as f:
            reasons = [r["reason"] for r in csv.DictReader(f)]
        self.assertEqual(reasons, ["take_profit", "stop_loss", "time_exit"])

    def test_scan_fail_closed_without_verdicts(self):
        s = self.bot.load_state()
        with open(self.bot.WATCHLIST, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["address", "name", "scout_price", "holders", "top10_pct"])
            w.writeheader()
            w.writerow(self._sample_row())
        # No auditor_verdicts.csv exists -> must NOT open anything
        with patch.object(self.bot, "fetch_price", return_value=self._sample_live()):
            self.bot.scan(s)
        self.assertEqual(len(s["open"]), 0)

    def test_scan_enforces_entry_ok_max_open_and_daily_loss_cap(self):
        s = self.bot.load_state()
        with open(self.bot.WATCHLIST, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["address", "name", "scout_price", "holders", "top10_pct"])
            w.writeheader()
            for i in range(7):
                w.writerow(self._sample_row(addr=f"Mint{i}", name=f"C{i}"))
        with open(self.bot.VERDICTS, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["address", "verdict", "time"])
            w.writeheader()
            w.writerow({"address": "Mint0", "verdict": "ENTRY FAIL", "time": "2026-11-27T12:00:00"})
            for i in range(1, 7):
                w.writerow({"address": f"Mint{i}", "verdict": "ENTRY OK", "time": "2026-11-27T12:00:00"})

        with patch.object(self.bot, "fetch_price", return_value=self._sample_live()), \
             patch.object(self.bot, "fetch_pumpfun_price", return_value=None):
            self.bot.scan(s)

        # Mint0 skipped (ENTRY FAIL), Mint1..Mint5 opened (max 5), Mint6 not opened
        self.assertEqual([p["address"] for p in s["open"]], ["Mint1", "Mint2", "Mint3", "Mint4", "Mint5"])

        # Daily loss cap test
        s2 = self.bot.load_state()
        s2["day_pnl"] = -15.0
        with patch.object(self.bot, "fetch_price", return_value=self._sample_live()):
            self.bot.scan(s2)
        self.assertEqual(len(s2["open"]), 0)

    def test_rollover_day_and_atomic_state_roundtrip(self):
        s = {"open": [], "day": "2026-11-26", "day_pnl": -12.5, "skipped_today": ["x|y"]}
        self.bot.rollover_day(s)
        self.assertEqual(s["day_pnl"], 0.0)
        self.assertEqual(s["skipped_today"], [])
        self.bot.save_state(s)
        s2 = self.bot.load_state()
        self.assertEqual(s2["day"], s["day"])

    def test_csv_header_migration(self):
        self.bot.append("m.csv", {"a": 1, "b": 2})
        self.bot.append("m.csv", {"a": 3, "b": 4, "c": 5})
        with open("m.csv", "r", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["c"], "")
        self.assertEqual(rows[1]["c"], "5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
