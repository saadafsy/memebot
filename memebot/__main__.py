"""Command line.

  python -m memebot init    [--data DIR]   create the data folder with Jawad's settings.json
  python -m memebot check   [--data DIR]   validate settings + env, test read-only API access
  python -m memebot run     [--data DIR]   run everything (engine + Telegram + panel)
  python -m memebot once    [--data DIR]   one manage + scan cycle, then exit (like bot.py --once)
  python -m memebot report  [--data DIR]   closed-trade summary (like bot.py report)
  python -m memebot fees    [--data DIR]   measure the real round-trip cost of a $size trade via
                                           Jupiter quotes for each watchlist coin (no key, no trade)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from .config import LIVE_ACK_PHRASE, SettingsError, live_gate_problems, load_env, load_settings
from .logsetup import setup_logging

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.json"
ENV_FILES = (Path.cwd() / ".env", ROOT / ".env")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    from dotenv import dotenv_values

    for p in (Path.cwd() / ".env", ROOT / ".env"):
        if p.exists():
            # The wallet key is never taken from a file, even if someone puts it there.
            for k, v in dotenv_values(p).items():
                if k != "MEMEBOT_PRIVATE_KEY" and v is not None and k not in os.environ:
                    os.environ[k] = v
            break


def cmd_init(data: Path) -> int:
    data.mkdir(parents=True, exist_ok=True)
    dst = data / "settings.json"
    if dst.exists():
        print(f"{dst} already exists; left unchanged.")
    else:
        shutil.copyfile(DEFAULT_SETTINGS, dst)
        print(f"Created {dst} (Jawad's settings, mode SIM).")
    for name, header in (("watchlist.csv", "name,address,holders,top10_pct,scout_price\n"),
                         ("auditor_verdicts.csv", "address,verdict,time\n")):
        p = data / name
        if not p.exists():
            p.write_text(header, encoding="utf-8")
            print(f"Created empty {p} (the Scout/Auditor fill this in).")
    return 0


def cmd_report(data: Path) -> int:
    from .storage import DataDir

    d = DataDir(data)
    cfg = load_settings(d.root / "settings.json")
    books = [("PAPER", d)]
    if (d.root / "live").exists():
        books.append(("REAL", DataDir(d.root / "live")))
    for label, dd in books:
        rows = dd.read_csv(dd.trades_path)
        if not rows:
            print(f"[{label}] No closed trades yet.")
            continue
        pnl = [float(r["net_pnl"]) for r in rows if r.get("net_pnl")]
        wins = [x for x in pnl if x > 0]
        top3 = sum(sorted(wins, reverse=True)[:3])
        print(f"[{label}] Closed: {len(rows)}/{cfg['target_closed_trades']} | Win rate: {len(wins)/len(pnl):.0%} | "
              f"Net P/L: {sum(pnl):.2f} | Without top 3 winners: {sum(pnl)-top3:.2f}")
    return 0


async def _check(data: Path) -> int:
    from .jupiter import SOL_MINT, Jupiter
    from .market import Market
    from .net import FetchError, make_client

    ok = True
    env = load_env()
    try:
        cfg = load_settings(data / "settings.json")
        print(f"settings.json OK  mode={cfg.mode}  sha256={cfg.sha256[:8]}")
    except (SettingsError, OSError) as e:
        print(f"settings.json PROBLEM: {e}")
        return 2
    print(f"Telegram: {'configured' if env.telegram_token and env.telegram_chat_id else 'not configured'}")
    print(f"Panel: {env.panel_host}:{env.panel_port}  password {'set' if env.panel_password else 'not set'}")
    probs = live_gate_problems(cfg, env, ENV_FILES)
    print("Real money: ARMED" if not probs else "Real money: not armed (" + "; ".join(probs) + ")")
    async with make_client() as c:
        m, j = Market(c), Jupiter(c, env.jupiter_api_key, env.jupiter_base)
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        try:
            px = await m.prices([SOL_MINT])
            print(f"DexScreener: OK (SOL ${px[SOL_MINT]['price']:.2f})" if SOL_MINT in px else "DexScreener: no SOL price")
        except Exception as e:
            ok = False
            print(f"DexScreener: FAILED {e}")
        sol_usd = await j.usd_price(SOL_MINT)
        print(f"Jupiter price: {'OK $%.2f' % sol_usd if sol_usd else 'FAILED'}")
        try:
            o = await j.order(SOL_MINT, usdc, 10_000_000)
            print(f"Jupiter Swap V2 quote: OK 0.01 SOL -> {o.out_amount/1e6:.4f} USDC, impact {o.price_impact_pct:.4f}%"
                  + ("" if env.jupiter_api_key else " (keyless, rate-limited; set JUPITER_API_KEY for LIVE)"))
        except FetchError as e:
            ok = False
            print(f"Jupiter Swap V2 quote: FAILED {e}")
        rc = await m.creator_history(usdc)
        print(f"RugCheck: {'OK' if rc != 'blind' else 'FAILED/blind'} ({rc[:60]})")
    return 0 if ok else 1


async def _fees(data: Path) -> int:
    from .jupiter import SOL_MINT, Jupiter
    from .net import FetchError, make_client
    from .storage import DataDir

    env = load_env()
    cfg = load_settings(data / "settings.json")
    rows = DataDir(data).read_csv(data / "watchlist.csv")
    async with make_client() as c:
        j = Jupiter(c, env.jupiter_api_key, env.jupiter_base)
        sol = await j.usd_price(SOL_MINT)
        if not sol:
            print("Could not read SOL price.")
            return 1
        lamports = int(cfg["position_size_usd"] / sol * 1e9)
        print(f"Round trip of ${cfg['position_size_usd']} via Jupiter quotes (no trade). settings fee: ${cfg['roundtrip_fee_usd']}")
        for r in rows:
            mint = (r.get("address") or "").strip()
            await asyncio.sleep(2.2)  # keyless Jupiter allows about 0.5 requests per second
            try:
                b = await j.order(SOL_MINT, mint, lamports)
                s = await j.order(mint, SOL_MINT, b.out_amount)
                loss = (1 - s.out_amount / b.in_amount)
                print(f"  {r.get('name','?'):<12} cost ${loss*cfg['position_size_usd']:.3f} ({loss*100:.2f}%) "
                      f"impact in/out {b.price_impact_pct:.2f}%/{s.price_impact_pct:.2f}%")
            except FetchError as e:
                print(f"  {r.get('name','?'):<12} no route: {e}")
    print("Network fee per swap is about 0.000005 SOL plus any priority fee.")
    return 0


def _safe_console() -> None:
    """Coin names often contain emoji; a Windows console or redirected log (cp1252) must never crash on them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    ap = argparse.ArgumentParser(prog="memebot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "check", "run", "once", "report", "fees"])
    ap.add_argument("--data", default=os.environ.get("MEMEBOT_DATA", "data"), help="data folder (default ./data)")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--no-panel", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    _load_dotenv()
    data = Path(a.data).resolve()
    env = load_env()
    import logging

    setup_logging(data if data.exists() else None, env.secrets(), logging.DEBUG if a.verbose else logging.INFO)
    if a.command == "init":
        return cmd_init(data)
    if a.command == "report":
        return cmd_report(data)
    if a.command == "check":
        return asyncio.run(_check(data))
    if a.command == "fees":
        return asyncio.run(_fees(data))
    from .app import build, main_run, run_once

    if a.command == "run":
        return main_run(str(data), telegram=not a.no_telegram, panel=not a.no_panel)
    try:
        bot = build(str(data))
    except Exception as e:
        print(f"memebot: {e}", file=sys.stderr)
        return 2
    asyncio.run(run_once(bot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
