"""Config, storage, time, logging redaction, network retries, market parsing."""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from memebot import timeutil
from memebot.config import LIVE_ACK_PHRASE, SettingsError, live_gate_problems, load_env, load_settings
from memebot.logsetup import RedactFilter
from memebot.market import Market, best_pairs, summarize_rugcheck
from memebot.net import FetchError, request_json
from memebot.storage import AlreadyRunning, DataDir, StateCorrupt

from .conftest import ROOT


# ------------------------------------------------------------------ settings
def test_jawads_settings_load_unchanged(cfg):
    raw = (ROOT / "config" / "settings.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest().startswith("b9f69186")  # his file, byte-identical
    assert cfg.mode == "SIM" and cfg["position_size_usd"] == 20 and cfg["roundtrip_fee_usd"] == 1.8
    assert cfg["exit_poll_seconds"] == 15 and cfg["live_require_approval"] is True  # new keys default


@pytest.mark.parametrize("patch,msg", [
    ({"mode": "YOLO"}, "mode must be"),
    ({"stop_loss_pct": 150}, "outside the allowed range"),
    ({"max_open_positions": 2.5}, "whole number"),
    ({"take_profit_pct": "30"}, "must be a number"),
    ({"live_require_approval": "yes"}, "true or false"),
    ({"timezone": "Mars/Base"}, "timezone"),
])
def test_bad_settings_rejected(tmp_path, patch, msg):
    d = json.loads((ROOT / "config" / "settings.json").read_text())
    d.update(patch)
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(d))
    with pytest.raises(SettingsError, match=msg):
        load_settings(p)


def test_missing_key_and_bom(tmp_path):
    d = json.loads((ROOT / "config" / "settings.json").read_text())
    p = tmp_path / "s.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(d).encode())  # Windows Notepad BOM
    assert load_settings(p).mode == "SIM"
    del d["min_holders"]
    p.write_text(json.dumps(d))
    with pytest.raises(SettingsError, match="min_holders"):
        load_settings(p)


def test_live_gate_needs_everything(tmp_path):
    d = json.loads((ROOT / "config" / "settings.json").read_text())
    d["mode"] = "LIVE"
    p = tmp_path / "s.json"
    p.write_text(json.dumps(d))
    cfg = load_settings(p)
    assert len(live_gate_problems(cfg, load_env({}))) == 4
    env = load_env({"MEMEBOT_PRIVATE_KEY": "x" * 40, "JUPITER_API_KEY": "k" * 10, "SOLANA_RPC_URL": "https://rpc",
                    "MEMEBOT_LIVE_ACK": "yes"})
    assert any("MEMEBOT_LIVE_ACK" in p for p in live_gate_problems(cfg, env))
    env = load_env({"MEMEBOT_PRIVATE_KEY": "x" * 40, "JUPITER_API_KEY": "k" * 10, "SOLANA_RPC_URL": "https://rpc",
                    "MEMEBOT_LIVE_ACK": LIVE_ACK_PHRASE})
    assert live_gate_problems(cfg, env) == []


# ------------------------------------------------------------------ storage
def test_state_roundtrip_backup_and_corrupt_refusal(data):
    s = data.load_state()
    s["open"].append({"address": "A", "entry": 1.0})
    data.save_state(s)
    data.save_state(s)  # second save creates .bak of the first
    assert (data.root / "state.json.bak").exists()
    (data.root / "state.json").write_text("{ truncated")
    with pytest.raises(StateCorrupt, match="Refusing to start"):
        data.load_state()


def test_legacy_state_without_new_keys(data):
    (data.root / "state.json").write_text(json.dumps({"open": [], "day": "2026-11-27", "day_pnl": 0.0,
                                                       "skipped_today": []}))
    s = data.load_state()
    assert s["cooldowns"] == {} and s["loss_cap_alerted_day"] == ""


def test_single_instance_lock(tmp_path):
    a, b = DataDir(tmp_path), DataDir(tmp_path)
    a.acquire_lock()
    try:
        with pytest.raises(AlreadyRunning):
            b.acquire_lock()
    finally:
        a.release_lock()
    b.acquire_lock()
    b.release_lock()


def test_csv_migration_and_utf8_sig(data):
    p = data.root / "m.csv"
    data.append_csv(p, {"a": 1, "b": 2})
    data.append_csv(p, {"a": 3, "b": 4, "c": 5})
    rows = data.read_csv(p)
    assert rows == [{"a": "1", "b": "2", "c": ""}, {"a": "3", "b": "4", "c": "5"}]
    p2 = data.root / "w.csv"
    p2.write_bytes("﻿name,address\nX,Y\n".encode("utf-8"))  # Excel-saved CSV
    assert data.read_csv(p2) == [{"name": "X", "address": "Y"}]


def test_journal_unresolved(data):
    data.journal({"id": "1", "status": "sent", "side": "buy", "mint": "M"})
    data.journal({"id": "2", "status": "sent", "side": "buy", "mint": "N"})
    data.journal({"id": "2", "status": "done"})
    with open(data.journal_path, "a") as f:
        f.write('{"id": "3", "sta')  # torn write from a crash
    assert [e["id"] for e in data.unresolved_journal()] == ["1"]


# ------------------------------------------------------------------ time
def test_naive_legacy_timestamps_and_dst():
    timeutil.set_tz("America/Toronto")
    naive = timeutil.parse("2026-11-27T12:57:46")  # what bot.py wrote
    assert naive.utcoffset() == timedelta(hours=-5)
    # A 24h deadline across the Nov 1 2026 DST change stays 24 real hours
    opened = timeutil.parse("2026-10-31T12:00:00")
    deadline = opened + timedelta(hours=24)
    assert (deadline - opened).total_seconds() == 86400
    assert timeutil.parse(timeutil.iso(deadline)) == deadline


# ------------------------------------------------------------------ redaction
def test_redact_filter_masks_tokens_and_secrets():
    f = RedactFilter(["supersecretkey123"])
    rec = logging.LogRecord("httpx", logging.INFO, "", 0,
                            "POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage "
                            "key=supersecretkey123 x-api-key: abcdef123456", None, None)
    f.filter(rec)
    out = rec.getMessage()
    assert "AAHdq" not in out and "supersecretkey123" not in out and "abcdef123456" not in out


# ------------------------------------------------------------------ network
async def test_retry_on_429_with_retry_after_and_reset():
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    with respx.mock() as m:
        route = m.get("https://x.test/a").mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.ConnectError("reset"),
            httpx.Response(200, json={"ok": 1}),
        ])
        async with httpx.AsyncClient() as c:
            assert await request_json(c, "GET", "https://x.test/a", label="t", sleep=fake_sleep) == {"ok": 1}
        assert route.call_count == 3 and sleeps[0] == 7


async def test_4xx_not_retried_and_gives_fetcherror():
    with respx.mock() as m:
        route = m.get("https://x.test/b").mock(return_value=httpx.Response(404, json={"e": 1}))
        async with httpx.AsyncClient() as c:
            with pytest.raises(FetchError):
                await request_json(c, "GET", "https://x.test/b", label="t")
        assert route.call_count == 1


async def test_network_down_never_raises_raw_exception():
    async def no_sleep(s):
        pass

    with respx.mock() as m:
        m.get("https://x.test/c").mock(side_effect=httpx.RemoteProtocolError("Server disconnected"))
        async with httpx.AsyncClient() as c:
            with pytest.raises(FetchError):
                await request_json(c, "GET", "https://x.test/c", label="t", sleep=no_sleep)


# ------------------------------------------------------------------ market
def _pair(base, quote, price, liq, chain="solana"):
    return {"chainId": chain, "baseToken": {"address": base, "symbol": "B"}, "quoteToken": {"address": quote},
            "priceUsd": str(price), "liquidity": {"usd": liq}, "pairAddress": f"{base}-{quote}", "dexId": "raydium"}


def test_best_pairs_requires_base_token():
    pairs = [_pair("OTHER", "MINT", 150.0, 9e6),   # MINT is the quote token here: wrong price
             _pair("MINT", "SOL", 0.004, 30_000), _pair("MINT", "USDC", 0.0041, 80_000),
             _pair("MINT", "SOL", 9.0, 1e9, chain="ethereum"), {"chainId": "solana", "priceUsd": "nan"}]
    out = best_pairs(pairs, {"MINT"})
    assert out["MINT"]["price"] == 0.0041 and out["MINT"]["liq"] == 80_000


async def test_market_batches_30_per_call():
    mints = [f"M{i}" for i in range(65)]
    with respx.mock() as m:
        route = m.get(url__regex=r"https://api\.dexscreener\.com/tokens/v1/solana/.*").mock(
            side_effect=lambda req: httpx.Response(200, json=[_pair(x, "SOL", 1.0, 1e5) for x in
                                                               req.url.path.rsplit("/", 1)[1].split(",")]))
        async with httpx.AsyncClient() as c:
            out = await Market(c).prices(mints)
    assert route.call_count == 3 and len(out) == 65


def test_rugcheck_summary():
    assert summarize_rugcheck({"risks": [], "score_normalised": 3}) == "rugcheck_ok(score=3)"
    assert summarize_rugcheck({"risks": [{"name": "Mutable metadata", "level": "warn"},
                                         {"name": "Freeze Authority still enabled", "level": "danger"}]}
                              ).startswith("rugcheck_danger:Freeze")
    assert summarize_rugcheck(None) == "blind"
