"""Reproduce the bot.py bugs reported to Jawad, against his untouched bot.py.
Each test passes when the bug IS present in the original (so the report is verified)."""
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from .conftest import ROOT

SIM = ROOT / "legacy" / "sim-bot"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_bytes((SIM / "settings.json").read_bytes())
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(SIM))
    for k in ("ALERT_TELEGRAM_TOKEN", "ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("orig_bot", SIM / "bot.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_bug1_connection_reset_escapes_fetch_price(bot):
    with patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset by peer")):
        with pytest.raises(ConnectionResetError):
            bot.fetch_price("Mint")


def test_bug2_time_exit_never_fires_without_price(bot):
    s = bot.load_state()
    bot.open_position(s, {"address": "A", "name": "A", "holders": "1000", "top10_pct": "10", "scout_price": "1"},
                      {"price": 1.0, "liq": 5e4})
    s["open"][0]["deadline"] = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    with patch.object(bot, "fetch_price", return_value=None):
        bot.manage(s)
    assert len(s["open"]) == 1  # 3 days past its 24h deadline and still open


def test_bug3_quote_token_pair_price_used(bot):
    payload = {"pairs": [{"chainId": "solana", "priceUsd": "150.0", "liquidity": {"usd": 9e6}, "pairAddress": "P",
                          "dexId": "raydium", "baseToken": {"address": "SOL"}, "quoteToken": {"address": "Mint"}}]}
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with patch("urllib.request.urlopen", return_value=R(json.dumps(payload).encode())):
        assert bot.fetch_price("Mint")["price"] == 150.0  # that's SOL's price, not the coin's


def test_bug5_skip_dedupe_breaks_on_changing_numbers(bot):
    s = bot.load_state()
    for liq in (19000, 18500, 17250):
        bot.skip(s, "A", "A", f"liquidity below minimum (${liq:,.0f} < $20,000)")
    with open(bot.SKIPS) as f:
        assert len(f.readlines()) == 4  # header + 3 rows for the same skip on the same day


def test_bug6_naive_timestamps(bot):
    s = bot.load_state()
    bot.open_position(s, {"address": "A", "name": "A", "holders": "1000", "top10_pct": "10", "scout_price": "1"},
                      {"price": 1.0, "liq": 5e4})
    assert datetime.fromisoformat(s["open"][0]["deadline"]).tzinfo is None
