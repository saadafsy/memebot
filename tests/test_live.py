"""Real-money path with a fake Jupiter and a fake Solana RPC. Nothing here touches the network."""
import base64
import importlib.util
import json

import pytest

from memebot import timeutil
from memebot.config import load_settings
from memebot.engine import Engine
from memebot.executors import BuyContext, LiveExecutor, SheetExecutor
from memebot.jupiter import SOL_MINT, ExecResult, Order, parse_execute, parse_order, price_impact_percent
from memebot.net import FetchError
from memebot.storage import DataDir
from memebot.strategy import pre_trade_gate
from memebot.wallet import WalletError, load_keypair, sign_transaction_b64

from .conftest import ROOT, row, write_watch

MINT = "TokenMint1111111111111111111111111111111111"


@pytest.fixture
def live_cfg(tmp_path):
    d = json.loads((ROOT / "config" / "settings.json").read_text())
    d["mode"] = "LIVE"
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(d))
    return load_settings(p)


def unsigned_tx_b64(kp):
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode(), msg


class FakeJup:
    """Quotes a token at $0.01 with SOL at $200 and 6 decimals."""

    def __init__(self, impact=0.5, sell_ok=True, rt_keep=0.98, exec_ok=True, exec_raises=False, kp=None):
        self.impact, self.sell_ok, self.rt_keep = impact, sell_ok, rt_keep
        self.exec_ok, self.exec_raises, self.kp = exec_ok, exec_raises, kp
        self.executed = []

    async def usd_price(self, mint):
        return 200.0

    async def decimals(self, mint):
        return 9 if mint == SOL_MINT else 6

    async def order(self, inp, out, amount, *, taker=None, slippage_bps=None):
        tx = unsigned_tx_b64(self.kp)[0] if (taker and self.kp) else None
        if inp == SOL_MINT:  # buy: lamports -> tokens at $0.01
            usd = amount / 1e9 * 200
            outa = int(usd / 0.01 * 1e6)
            return Order("req-buy", amount, outa, self.impact, tx, slippage_bps, {"outputMint": out})
        if not self.sell_ok:
            raise FetchError("jupiter order: no route")
        usd = amount / 1e6 * 0.01 * self.rt_keep
        return Order("req-sell", amount, int(usd / 200 * 1e9), self.impact, tx, slippage_bps, {"outputMint": out})

    async def execute(self, signed, request_id, last_valid_block_height=None):
        if self.exec_raises:
            raise FetchError("jupiter execute: network error")
        self.executed.append(request_id)
        return ExecResult(self.exec_ok, "SIG" + request_id, None, 2_000_000_000 if request_id == "req-buy" else 98_000_000,
                          None if self.exec_ok else "slippage", {})


class FakeRpc:
    def __init__(self, sol=1.0, tokens=0):
        self.sol, self.tokens = sol, tokens

    async def sol_balance(self, owner):
        return self.sol

    async def token_balance(self, owner, mint):
        return self.tokens, 6

    async def tx_fee_lamports(self, sig):
        return 5000


def make_live(live_cfg, tmp_path, market, jup, rpc, kp):
    d = DataDir(tmp_path / "live", inputs_from=tmp_path)
    from memebot.notify import Notifier, Outbox
    n = Notifier(d, "LIVE", Outbox(), tag="REAL")
    return Engine(live_cfg, d, market, LiveExecutor(live_cfg, jup, rpc, kp, d), n), d


@pytest.fixture
def kp():
    from solders.keypair import Keypair
    return Keypair()


# ------------------------------------------------------------------ signing / key loading
def test_sign_fills_our_signature(kp):
    from solders.transaction import VersionedTransaction
    b64, msg = unsigned_tx_b64(kp)
    signed = VersionedTransaction.from_bytes(base64.b64decode(sign_transaction_b64(b64, kp)))
    assert signed.signatures[0] == VersionedTransaction(msg, [kp]).signatures[0]


def test_refuses_tx_for_other_wallet(kp):
    from solders.keypair import Keypair
    b64, _ = unsigned_tx_b64(Keypair())
    with pytest.raises(WalletError, match="does not list this wallet"):
        sign_transaction_b64(b64, kp)


def test_keypair_formats_and_errors_hide_key(kp):
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()
    with pytest.raises(WalletError) as ei:
        load_keypair("notakey-SECRET")
    assert "SECRET" not in str(ei.value)


# ------------------------------------------------------------------ jupiter parsing
def test_parse_order_and_impact_spellings():
    o = parse_order({"requestId": "r", "inAmount": "100", "outAmount": "200", "priceImpactPct": "0.0123",
                     "transaction": None})
    assert o.price_impact_pct == pytest.approx(1.23)
    assert price_impact_percent({"priceImpact": -0.5}) == 0.5
    for bad in ({"inAmount": "1", "outAmount": "0", "priceImpact": 0}, {"errorMessage": "no route"},
                {"inAmount": "1", "outAmount": "2"}):
        with pytest.raises(FetchError):
            parse_order(bad)


def test_parse_execute():
    r = parse_execute({"status": "Success", "signature": "abc", "inputAmountResult": "10", "outputAmountResult": "20"})
    assert r.ok and r.out_amount == 20
    assert not parse_execute({"status": "Failed", "signature": "abc", "error": "slippage"}).ok
    assert not parse_execute({"status": "Success", "signature": ""}).ok


# ------------------------------------------------------------------ gate parity with Jawad's validate_pre_trade_safety
def _his_gate():
    spec = importlib.util.spec_from_file_location("lti", ROOT / "legacy" / "handoff" / "live_trader_interface.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("case", [
    dict(), dict(quote_mint="OTHER"), dict(creator="blind"), dict(sell=False), dict(open_n=3), dict(day=-15.0),
    dict(jp=1.04), dict(impact=3.5),
])
def test_gate_matches_his_function(live_cfg, case):
    m = _his_gate()
    live_cfg.values["live_max_price_impact_pct"] = 3.0  # his LiveSafetyConfig value, for a like-for-like check
    args = dict(expected_mint="M", quote_mint=case.get("quote_mint", "M"), ref=1.0, jp=case.get("jp", 1.01),
                impact=case.get("impact", 1.0), open_n=case.get("open_n", 0), day=case.get("day", 0.0),
                creator=case.get("creator", "rugcheck_ok(score=1)"), sell=case.get("sell", True))
    his = m.validate_pre_trade_safety(expected_mint=args["expected_mint"], quote_mint=args["quote_mint"],
                                      pumpfun_price=args["ref"], jupiter_price=args["jp"],
                                      price_impact_pct=args["impact"], open_live_count=args["open_n"],
                                      day_pnl_usd=args["day"], creator_history=args["creator"],
                                      sell_route_verified=args["sell"])
    ours = pre_trade_gate(expected_mint=args["expected_mint"], quote_mint=args["quote_mint"], ref_price=args["ref"],
                          jupiter_price=args["jp"], price_impact_pct=args["impact"], open_live_count=args["open_n"],
                          day_pnl_usd=args["day"], creator_history=args["creator"], sell_route_verified=args["sell"],
                          roundtrip_loss_pct=1.0, deployed_usd=0, size_usd=20, sol_after_buy=1.0, cfg=live_cfg)
    assert his.ok == ours.ok


def test_gate_extra_caps(live_cfg):
    base = dict(expected_mint="M", quote_mint="M", ref_price=1.0, jupiter_price=1.0, price_impact_pct=0.1,
                open_live_count=0, day_pnl_usd=0, creator_history="rugcheck_ok", sell_route_verified=True,
                roundtrip_loss_pct=1.0, deployed_usd=0, size_usd=20, sol_after_buy=1.0, cfg=live_cfg)
    assert pre_trade_gate(**base).ok
    assert "budget" in pre_trade_gate(**{**base, "deployed_usd": 30}).reason
    assert "reserve" in pre_trade_gate(**{**base, "sol_after_buy": 0.01}).reason
    assert "round-trip" in pre_trade_gate(**{**base, "roundtrip_loss_pct": 25}).reason
    assert not pre_trade_gate(**{**base, "creator_history": "rugcheck_danger:x"}).ok


# ------------------------------------------------------------------ engine in LIVE
async def test_live_starts_off_and_needs_approval(live_cfg, tmp_path, market, kp):
    jup, rpc = FakeJup(kp=kp), FakeRpc()
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    await e.tick(force_scan=True)
    assert e.entries_on is False and jup.executed == []  # REAL starts OFF
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    assert jup.executed == [] and len(e.approvals) == 1  # waits for Approve
    aid = next(iter(e.approvals))
    assert "Approved" in e.decide_approval(aid, True, "test")
    await e.tick()
    assert jup.executed == ["req-buy"]
    p = e.state["open"][0]
    assert p["buy_tx_sig"] == "SIGreq-buy" and p["entry"] == pytest.approx(0.01, rel=1e-3)
    jl = [x["status"] for x in d.read_journal()]
    assert jl == ["sent", "done"]  # written BEFORE and after execute


async def test_live_reject_and_expire(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp)
    e, _ = make_live(live_cfg, tmp_path, market, jup, FakeRpc(), kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    aid = next(iter(e.approvals))
    e.decide_approval(aid, False, "test")
    await e.tick()
    assert jup.executed == []
    e.approvals[aid].status = "pending"
    e.approvals[aid].created = timeutil.now().replace(year=2020)
    assert "expired" in e.decide_approval(aid, True, "test")


@pytest.mark.parametrize("jup_kw,reason", [
    (dict(sell_ok=False), "sell route"), (dict(impact=12.0), "price impact"), (dict(rt_keep=0.94), "round-trip"),
])
async def test_live_gate_blocks_buy(live_cfg, tmp_path, market, kp, jup_kw, reason):
    jup = FakeJup(kp=kp, **jup_kw)
    e, d = make_live(live_cfg, tmp_path, market, jup, FakeRpc(), kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    e.cfg.values["live_require_approval"] = False
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    assert jup.executed == [] and e.state["open"] == []
    assert reason in d.read_csv(d.skips_path)[0]["reason"]


async def test_live_blind_creator_never_buys(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp)
    e, d = make_live(live_cfg, tmp_path, market, jup, FakeRpc(), kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    market.creator = "blind"
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    assert e.approvals == {} and jup.executed == []
    assert "creator_history" in d.read_csv(d.skips_path)[0]["reason"]


async def test_live_sell_retries_then_manual_alert(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp)
    rpc = FakeRpc(tokens=2_000_000_000)
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    e.cfg.values["live_require_approval"] = False
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    assert len(e.state["open"]) == 1
    jup.exec_ok = False
    market.set(MINT, 0.0131)
    for _ in range(5):
        e.state["open"][0].pop("next_sell_try_at", None)
        await e.tick()
    assert e.state["open"][0]["sell_failures"] == 5
    assert "MANUAL_ACTION_NEEDED" in d.alerts_log.read_text()
    jup.exec_ok = True
    e.state["open"][0].pop("next_sell_try_at", None)
    await e.tick()
    t = d.read_csv(d.trades_path)
    assert e.state["open"] == [] and t[0]["mode"] == "LIVE" and t[0]["sell_tx_sig"] == "SIGreq-sell"
    assert t[0]["buy_tx_sig"] == "SIGreq-buy"


async def test_live_crash_mid_buy_is_reconciled(live_cfg, tmp_path, market, kp):
    jup = FakeJup(kp=kp, exec_raises=True)
    rpc = FakeRpc(tokens=0)
    e, d = make_live(live_cfg, tmp_path, market, jup, rpc, kp)
    write_watch(DataDir(tmp_path), [row(MINT, price="0.01")], {MINT: "ENTRY OK"})
    market.set(MINT, 0.01)
    e.cfg.values["live_require_approval"] = False
    e.set_entries(True, "test")
    await e.tick(force_scan=True)
    assert e.state["open"] == [] and len(d.unresolved_journal()) == 1
    rpc.tokens = 1_500_000_000  # the swap actually landed
    e2, _ = make_live(live_cfg, tmp_path, market, FakeJup(kp=kp), rpc, kp)
    await e2.reconcile()
    assert len(e2.state["open"]) == 1 and e2.state["open"][0]["creator_history"] == "recovered"
    assert d.unresolved_journal() == []


async def test_sheet_executor_never_signs(live_cfg, market):
    jup = FakeJup()
    ex = SheetExecutor(live_cfg, jup)
    ctx = BuyContext(mint=MINT, name="T", live={"price": 0.01, "liq": 5e4}, creator_history="rugcheck_ok",
                     open_live_count=0, day_pnl=0, deployed_usd=0)
    res = await ex.buy(ctx)
    assert not res.ok and res.sheet["verdict"] == "PRE-TRADE SAFETY PASS" and jup.executed == []
    assert res.sheet["expected_tokens"] == pytest.approx(2000, rel=1e-3)
