"""Jawad's own test suite must still pass against his untouched files."""
import hashlib
import subprocess
import sys

from .conftest import ROOT

MANIFEST = {
    "legacy/sim-bot/bot.py": "8fe8f98c22086f4ff880ad6a80e8c4390009ebbb3fc223cc0568ca3a9c4ea539",
    "legacy/sim-bot/alerts.py": "bbc84fddb37cbb253213cbecc1052df234f034ee00389051086d207fd39b2075",
    "legacy/handoff/test_bot.py": "f87608d9b449bd431760a38a6972e4fbc26b0680d539dd0cbc3c6eae5ded95ad",
    "legacy/handoff/live_trader_interface.py": "5a18d995a103914b540251577525f51e2768a2d97bec95e86921bc0224edaf49",
    "legacy/handoff/settings.json": "b9f6918624aaf5b2b1a31e3ecca52bf9a1ca1acf76b056f75b3532ce8c9146ed",
    "config/settings.json": "b9f6918624aaf5b2b1a31e3ecca52bf9a1ca1acf76b056f75b3532ce8c9146ed",
}


def test_original_files_untouched():
    for rel, want in MANIFEST.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == want, rel


def test_jawads_test_suite_passes():
    r = subprocess.run([sys.executable, str(ROOT / "legacy" / "run_legacy_tests.py")], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "OK" in r.stderr
