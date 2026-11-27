"""Builds and runs everything in one process: paper engine (always), the real-money engine (only
when armed), the Telegram bot and the web panel, on one asyncio event loop."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import timeutil
from .config import Env, Settings, live_gate_problems, load_env, load_settings
from .engine import Engine
from .executors import LiveExecutor, SheetExecutor, SimExecutor
from .jupiter import Jupiter
from .market import Market
from .net import make_client
from .notify import Notifier, Outbox
from .storage import DataDir

log = logging.getLogger("memebot")


class StartupError(RuntimeError):
    pass


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def record_settings_hash(data: DataDir, cfg: Settings, notify: Notifier) -> None:
    prev = ""
    if data.settings_history.exists():
        lines = [ln for ln in data.settings_history.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            prev = lines[-1].split()[-1]
    with open(data.settings_history, "a", encoding="utf-8") as f:
        f.write(f"{timeutil.iso(timeutil.now())} {cfg.sha256}\n")
    log.info("settings.json sha256 %s", cfg.sha256)
    if prev and prev != cfg.sha256:
        notify.alert("SETTINGS_CHANGED", "settings.json changed since the last start",
                     {"old": prev[:8], "new": cfg.sha256[:8]}, loud=True)
    if cfg.get("_unknown_keys"):
        log.warning("settings.json has keys this version does not use: %s", ", ".join(cfg["_unknown_keys"]))


@dataclass
class Bot:
    cfg: Settings
    env: Env
    data: DataDir
    engines: list[Engine]
    outbox: Outbox
    client: object
    extra_dirs: list[DataDir] = field(default_factory=list)


def build(data_dir: str | Path, env: Env | None = None, client=None) -> Bot:
    env = env or load_env()
    data = DataDir(data_dir)
    settings_path = data.root / "settings.json"
    if not settings_path.exists():
        raise StartupError(f"{settings_path} not found. Run: python -m memebot init --data {data.root}")
    cfg = load_settings(settings_path)
    timeutil.set_tz(cfg["timezone"])
    if cfg.mode == "LIVE":
        root = Path(__file__).resolve().parent.parent
        probs = live_gate_problems(cfg, env, (Path.cwd() / ".env", root / ".env", data.root / ".env"))
        if probs:
            raise StartupError("Real trading is not armed, so the bot will not start in LIVE mode:\n  - "
                               + "\n  - ".join(probs) + "\nSet mode to SIM in settings.json for paper only.")
    data.acquire_lock()
    client = client or make_client()
    market = Market(client)
    outbox = Outbox()
    paper_notify = Notifier(data, cfg["lane"], outbox, tag="PAPER")
    record_settings_hash(data, cfg, paper_notify)
    engines = [Engine(cfg, data, market, SimExecutor(cfg), paper_notify, label="PAPER")]
    extra = []
    jup = Jupiter(client, env.jupiter_api_key, env.jupiter_base)
    if cfg.mode == "SHEET":
        sd = DataDir(data.root / "sheet", inputs_from=data.root)
        extra.append(sd)
        engines.append(Engine(cfg, sd, market, SheetExecutor(cfg, jup), Notifier(sd, "SHEET", outbox, tag="SHEET")))
    elif cfg.mode == "LIVE":
        from .wallet import Rpc, load_keypair

        kp = load_keypair(env.private_key)
        ld = DataDir(data.root / "live", inputs_from=data.root)
        ld.acquire_lock()
        extra.append(ld)
        lex = LiveExecutor(cfg, jup, Rpc(client, env.rpc_url), kp, ld)
        live_engine = Engine(cfg, ld, market, lex, Notifier(ld, "LIVE", outbox, tag="REAL"))
        if cfg["live_requires_paper_target"]:
            target = cfg["target_closed_trades"]

            def paper_target_guard() -> str | None:
                n = data.count_closed()
                if n < target:
                    return (f"Real trading stays OFF until the paper run finishes: {n}/{target} paper trades "
                            "closed (live_requires_paper_target in settings.json).")
                return None

            live_engine.entry_guard = paper_target_guard
            if paper_target_guard():
                live_engine.state["control"]["entries_on"] = False
        engines.append(live_engine)
        log.warning("REAL MONEY ARMED for wallet %s…%s. Real entries start OFF; turn them on with /liveon. "
                    "Every buy needs an Approve tap: %s", str(kp.pubkey())[:4], str(kp.pubkey())[-4:],
                    cfg["live_require_approval"])
    return Bot(cfg, env, data, engines, outbox, client, extra)


async def serve(bot: Bot, *, telegram: bool = True, panel: bool = True) -> None:
    env, stop = bot.env, asyncio.Event()
    if panel and not is_loopback(env.panel_host) and not env.panel_password:
        await bot.client.aclose()
        for d in [bot.data, *bot.extra_dirs]:
            d.release_lock()
        raise StartupError(f"PANEL_HOST={env.panel_host} would expose the panel beyond this computer, "
                           "so PANEL_PASSWORD is required. Set one, or use PANEL_HOST=127.0.0.1.")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(e.run(stop), name=f"engine-{e.label}") for e in bot.engines]
    tasks.append(asyncio.create_task(bot.outbox.run_sender(stop), name="telegram-out"))
    tg = None
    if telegram and env.telegram_token and env.telegram_chat_id:
        from .telegram_bot import TelegramControl

        try:
            tg = TelegramControl(env.telegram_token, env.telegram_chat_id, bot.engines)
            await tg.start()
            bot.outbox.sender = tg.send
            bot.engines[0].notify.alert("STARTED", f"memebot started ({bot.cfg.mode})",
                                        {"settings": bot.cfg.sha256[:8], "engines": ", ".join(e.label for e in bot.engines)})
        except Exception as e:
            log.error("Telegram could not start (%s). The bot keeps running without it.", type(e).__name__)
            tg = None
    elif telegram:
        log.info("Telegram disabled: set ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID to enable it")
    server = None
    if panel:
        import uvicorn

        from .panel import create_app

        app = create_app(bot.engines, password=env.panel_password, bind_host=env.panel_host,
                         port=env.panel_port, extra_hosts=env.panel_allowed_hosts)

        class _Server(uvicorn.Server):
            def install_signal_handlers(self) -> None:  # older uvicorn
                pass

            @contextlib.contextmanager
            def capture_signals(self):  # newer uvicorn: we handle signals ourselves
                yield

        server = _Server(uvicorn.Config(app, host=env.panel_host, port=env.panel_port, log_level="warning",
                                        lifespan="off", access_log=False))
        tasks.append(asyncio.create_task(server.serve(), name="panel"))
        log.info("panel: http://%s:%d", "127.0.0.1" if env.panel_host in ("0.0.0.0", "::") else env.panel_host,
                 env.panel_port)
    try:
        while not stop.is_set():
            done = [t for t in tasks if t.done()]
            for t in done:
                if t.exception() is not None:
                    log.error("task %s crashed: %r; shutting down cleanly", t.get_name(), t.exception())
                    stop.set()
                elif t.get_name() == "panel":
                    stop.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)
    finally:
        stop.set()
        if server:
            server.should_exit = True
        if tg:
            await tg.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        for e in bot.engines:
            e.data.save_state(e.state)
        await bot.client.aclose()
        for d in [bot.data, *bot.extra_dirs]:
            d.release_lock()
        log.info("stopped cleanly")


async def run_once(bot: Bot) -> None:
    try:
        for e in bot.engines:
            if e.mode == "LIVE":
                await e.reconcile()
            await e.tick(force_scan=True)
            st = e.status()
            print(f"[{e.label}] Closed: {st['closed']}/{st['target_closed']} | Win rate: {st['win_rate_pct']:.0f}% | "
                  f"Net P/L: {st['realized_pnl']:.2f} | Without top 3 winners: {st['without_top3']:.2f} | "
                  f"Open: {st['open_count']}")
    finally:
        await bot.client.aclose()
        for d in [bot.data, *bot.extra_dirs]:
            d.release_lock()


def main_run(data_dir: str, *, telegram: bool, panel: bool) -> int:
    try:
        bot = build(data_dir)
    except Exception as e:
        print(f"memebot: {e}", file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(bot, telegram=telegram, panel=panel))
    except StartupError as e:
        print(f"memebot: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0
