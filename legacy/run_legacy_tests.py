"""Run Jawad's original test_bot.py, unchanged, against his original bot.py.

His tests hard-code /workspace/sim-bot/ paths. This runner reads test_bot.py, points those paths
at legacy/sim-bot/ in memory, and runs it. Neither file is modified on disk.
Usage:  python legacy/run_legacy_tests.py
"""
import os
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM = HERE / "sim-bot"


def main() -> int:
    for k in ("ALERT_TELEGRAM_TOKEN", "ALERT_TELEGRAM_CHAT_ID"):
        os.environ.pop(k, None)  # never send real Telegram messages from tests
    sys.path.insert(0, str(SIM))  # so bot.py finds his alerts.py
    src = (HERE / "handoff" / "test_bot.py").read_text(encoding="utf-8")
    src = src.replace("/workspace/sim-bot/", SIM.as_posix() + "/")
    mod = types.ModuleType("legacy_test_bot")
    mod.__file__ = str(HERE / "handoff" / "test_bot.py")
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
