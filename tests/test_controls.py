"""Web panel security + Telegram formatting/authorization + startup guards."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from memebot.app import StartupError, build, is_loopback, serve
from memebot.config import load_env
from memebot.engine import Engine
from memebot.executors import SimExecutor
from memebot.notify import chunk_text
from memebot.panel import LoginLimiter, check_token, create_app, make_token

from .conftest import ROOT, row, write_watch

H = {"X-Memebot": "1"}


@pytest.fixture
def engine(cfg, data, market, notifier):
    return Engine(cfg, data, market, SimExecutor(cfg), notifier)


def client(engine, password="", host="127.0.0.1", extra=()):
    app = create_app([engine], password=password, bind_host=host, port=8787, extra_hosts=extra)
    return TestClient(app, base_url="http://127.0.0.1:8787")


def test_open_panel_on_localhost(engine):
    c = client(engine)
    r = c.get("/api/status")
    assert r.status_code == 200 and r.json()["engines"][0]["label"] == "PAPER"
    assert c.get("/").status_code == 200 and "memebot" in c.get("/").text
    assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert c.get("/sw.js").status_code == 200 and c.get("/icon-192.png").status_code == 200
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_post_requires_custom_header_and_same_origin(engine):
    c = client(engine)
    assert c.post("/api/control", json={"action": "stop"}).status_code == 403
    r = c.post("/api/control", json={"action": "stop"}, headers={**H, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/control", json={"action": "stop"}, headers={**H, "Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 200 and engine.entries_on is False


def test_dns_rebinding_host_rejected(engine):
    c = client(engine)
    assert c.get("/api/status", headers={"Host": "attacker.example"}).status_code == 421


def test_password_login_flow_and_rate_limit(engine):
    c = client(engine, password="correct horse")
    assert c.get("/api/status").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/manifest.webmanifest").status_code == 200  # PWA install works before login
    for _ in range(5):
        assert c.post("/api/login", json={"password": "nope"}, headers=H).status_code == 401
    assert c.post("/api/login", json={"password": "correct horse"}, headers=H).status_code == 429
    c2 = client(engine, password="correct horse")
    r = c2.post("/api/login", json={"password": "correct horse"}, headers=H)
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert c2.get("/api/status").status_code == 200


def test_no_endpoint_can_change_mode_or_settings(engine):
    c = client(engine)
    for action in ("mode", "set_mode", "live", "settings", "edit"):
        assert c.post("/api/control", json={"action": action, "mode": "LIVE"}, headers=H).status_code == 400
    assert c.post("/api/settings", json={"mode": "LIVE"}, headers=H).status_code == 405
    assert engine.mode == "SIM"
    r = c.post("/api/control", json={"action": "start", "target": "REAL"}, headers=H)
    assert r.status_code == 400  # REAL not armed


def test_tokens_and_limiter():
    s = b"k" * 32
    t = make_token(s, now=1000)
    assert check_token(s, t, now=1001) and not check_token(s, t, now=10**10) and not check_token(b"x" * 32, t, now=1001)
    assert not check_token(s, t[:-1] + ("0" if t[-1] != "0" else "1"), now=1001)
    lim = LoginLimiter(2, 10)
    lim.fail("ip", 0)
    lim.fail("ip", 1)
    assert not lim.allowed("ip", 2) and lim.allowed("ip", 12)


def test_lan_panel_refused_without_password(data, monkeypatch):
    env = load_env({"PANEL_HOST": "0.0.0.0"})
    bot = build(data.root, env=env)
    with pytest.raises(StartupError, match="PANEL_PASSWORD"):
        asyncio.run(serve(bot, telegram=False, panel=True))
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and not is_loopback("0.0.0.0")


def test_live_mode_refuses_to_start_unarmed(data):
    d = json.loads((data.root / "settings.json").read_text())
    d["mode"] = "LIVE"
    (data.root / "settings.json").write_text(json.dumps(d))
    with pytest.raises(StartupError, match="not armed"):
        build(data.root, env=load_env({}))


def test_settings_change_is_alerted(data):
    bot = build(data.root, env=load_env({}))
    asyncio.run(bot.client.aclose())
    bot.data.release_lock()
    d = json.loads((data.root / "settings.json").read_text())
    d["poll_seconds"] = 1800
    (data.root / "settings.json").write_text(json.dumps(d))
    bot = build(data.root, env=load_env({}))
    asyncio.run(bot.client.aclose())
    bot.data.release_lock()
    assert "SETTINGS_CHANGED" in data.alerts_log.read_text()
    assert len(data.settings_history.read_text().splitlines()) == 2


# ------------------------------------------------------------------ telegram
def test_chunking_respects_4096():
    text = "\n".join("x" * 100 for _ in range(200)) + "\n" + "y" * 9000
    parts = chunk_text(text)
    assert all(len(p) <= 4096 for p in parts) and "".join(parts) == text


async def test_telegram_ignores_other_chats_and_cannot_arm_live(engine):
    from memebot.telegram_bot import TelegramControl, fmt_status

    tg = TelegramControl("123456:" + "A" * 35, "42", [engine])
    sent = []

    class Q:
        data = "ctl:off:P"

        async def answer(self, *a, **k):
            sent.append(("answer", a))

    upd = SimpleNamespace(callback_query=Q(), effective_chat=SimpleNamespace(id=999))
    await tg.on_button(upd, None)
    assert sent == [] and engine.entries_on is True  # stranger ignored, nothing changed
    assert "not armed" in tg.act("on", "L")
    assert "<b>PAPER</b>" in fmt_status(engine.status())
    # HTML is escaped in coin names
    engine.state["open"].append({"name": "<script>", "address": "A", "entry": 1.0, "tp": 1.3, "sl": 0.85,
                                 "opened": "x", "deadline": "y"})
    from memebot.telegram_bot import fmt_positions
    assert "<script>" not in fmt_positions(engine.status())


async def test_telegram_owner_chat_controls(engine):
    from memebot.telegram_bot import TelegramControl

    tg = TelegramControl("123456:" + "A" * 35, "42", [engine])
    replies = []

    class Chat:
        id = 42

        async def send_message(self, text, **k):
            replies.append(text)

    class Q:
        data = "ctl:off:P"

        async def answer(self, *a, **k):
            pass

    await tg.on_button(SimpleNamespace(callback_query=Q(), effective_chat=Chat()), None)
    assert engine.entries_on is False and "PAPER" in replies[-1]


# ------------------------------------------------------------------ Jawad's rules from the doc
def _armed_env():
    from solders.keypair import Keypair
    from memebot.config import LIVE_ACK_PHRASE
    return load_env({"MEMEBOT_PRIVATE_KEY": str(Keypair()), "JUPITER_API_KEY": "k" * 12,
                     "SOLANA_RPC_URL": "https://rpc.example", "MEMEBOT_LIVE_ACK": LIVE_ACK_PHRASE})


def _set_live(data):
    d = json.loads((data.root / "settings.json").read_text())
    d["mode"] = "LIVE"
    (data.root / "settings.json").write_text(json.dumps(d))


def test_real_cannot_start_before_paper_target(data):
    _set_live(data)
    bot = build(data.root, env=_armed_env())
    try:
        live = [e for e in bot.engines if e.mode == "LIVE"][0]
        assert [e.label for e in bot.engines] == ["PAPER", "REAL"]
        msg = live.set_entries(True, "telegram")
        assert "0/100 paper trades" in msg and live.entries_on is False
        for i in range(100):  # paper run finished
            data.append_csv(data.trades_path, {"name": f"C{i}", "net_pnl": "-1"})
        assert "Entries ON" in live.set_entries(True, "telegram") and live.entries_on
    finally:
        asyncio.run(bot.client.aclose())
        for d in [bot.data, *bot.extra_dirs]:
            d.release_lock()


def test_private_key_in_a_file_blocks_live(data, tmp_path, monkeypatch):
    from memebot.config import key_in_files, live_gate_problems, load_settings
    _set_live(data)
    envfile = data.root / ".env"
    envfile.write_text("ALERT_TELEGRAM_CHAT_ID=1\nMEMEBOT_PRIVATE_KEY=5abcSECRET\n")
    assert key_in_files(envfile) == [str(envfile)]
    probs = live_gate_problems(load_settings(data.root / "settings.json"), _armed_env(), (envfile,))
    assert any("written in" in p for p in probs) and not any("SECRET" in p for p in probs)
    with pytest.raises(StartupError, match="written in"):
        build(data.root, env=_armed_env())
    envfile.write_text("MEMEBOT_PRIVATE_KEY=\n")
    assert key_in_files(envfile) == []


def test_dotenv_loader_never_reads_the_key(tmp_path, monkeypatch):
    from memebot import __main__ as cli
    (tmp_path / ".env").write_text("MEMEBOT_PRIVATE_KEY=fromfile\nPANEL_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMEBOT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("PANEL_PORT", raising=False)
    cli._load_dotenv()
    import os
    assert os.environ.get("PANEL_PORT") == "9999" and "MEMEBOT_PRIVATE_KEY" not in os.environ
