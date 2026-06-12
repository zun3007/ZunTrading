"""Executors: paper fill/sync theo nến; MT5 request dict đúng — mock module MetaTrader5."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from zuntrading.brain import Signal
from zuntrading.config import load_settings
from zuntrading.executor import MAGIC, ExecutorUnavailable, MT5Executor, PaperExecutor
from zuntrading.journal import Journal
from zuntrading.prefilter import Candidate
from zuntrading.risk import Verdict

SETTINGS = load_settings(Path(__file__).resolve().parents[1] / "config.yaml", env_path=None, risk_profile="can_bang")

XAU = next(s for s in SETTINGS.symbols if s.mt5 == "XAUUSD")
SIG = Signal(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
             confidence=0.75, reason="t")
CAND = Candidate(symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
                 direction="long", tf_entry="M15", price=2000.0, atr=8.0, context={})
OK = Verdict(approved=True, lots=0.25, risk_amount=100.0, reject_reasons=[])


@pytest.fixture
def j(tmp_path):
    journal = Journal(tmp_path / "t.db")
    yield journal
    journal.close()


def open_paper(j, paper, sig=SIG):
    sid = j.record_signal(CAND, sig, OK)
    res = paper.place(sig, XAU, 0.25)
    j.record_order(sid, "paper", res.ticket, XAU.mt5, XAU.market, sig, 0.25, 100.0)
    return res


# --- PaperExecutor ---

def test_paper_place_fills_at_entry(j):
    paper = PaperExecutor(SETTINGS, j)
    res = open_paper(j, paper)
    assert res.ok and res.fill_price == 2000.0 and res.ticket == "paper-1"


def test_paper_equity_drifts_with_outcomes(j):
    paper = PaperExecutor(SETTINGS, j)
    assert paper.equity() == SETTINGS.reference_equity
    open_paper(j, paper)
    paper.sync_outcomes(lambda s: (2010.0, 1999.0, 2009.0))  # high chạm TP 2008
    # pnl = (2008-2000)*0.25*100 = +200
    assert paper.equity() == SETTINGS.reference_equity + 200.0


def test_paper_sync_long_hits_tp(j):
    paper = PaperExecutor(SETTINGS, j)
    open_paper(j, paper)
    assert paper.sync_outcomes(lambda s: (2009.0, 1998.0, 2008.5)) == 1
    out = j.conn.execute("SELECT * FROM outcomes").fetchone()
    assert out["exit_price"] == 2008.0 and out["pnl"] == 200.0 and out["result"] == "win"


def test_paper_sync_long_hits_sl(j):
    paper = PaperExecutor(SETTINGS, j)
    open_paper(j, paper)
    assert paper.sync_outcomes(lambda s: (2003.0, 1995.0, 1997.0)) == 1
    out = j.conn.execute("SELECT * FROM outcomes").fetchone()
    assert out["exit_price"] == 1996.0 and out["pnl"] == -100.0 and out["result"] == "loss"


def test_paper_sync_candle_hits_both_takes_sl_pessimistic(j):
    paper = PaperExecutor(SETTINGS, j)
    open_paper(j, paper)
    paper.sync_outcomes(lambda s: (2012.0, 1994.0, 2000.0))  # chạm cả SL lẫn TP
    out = j.conn.execute("SELECT * FROM outcomes").fetchone()
    assert out["exit_price"] == 1996.0  # SL trước — bi quan


def test_paper_sync_no_touch_keeps_open(j):
    paper = PaperExecutor(SETTINGS, j)
    open_paper(j, paper)
    assert paper.sync_outcomes(lambda s: (2004.0, 1998.0, 2001.0)) == 0
    assert len(j.open_positions()) == 1


def test_paper_sync_short_directions(j):
    paper = PaperExecutor(SETTINGS, j)
    sig = Signal(action="trade", direction="short", entry=2000.0, sl=2004.0, tp=1992.0,
                 confidence=0.75, reason="t")
    open_paper(j, paper, sig=sig)
    paper.sync_outcomes(lambda s: (2001.0, 1991.0, 1992.5))  # low chạm TP 1992
    out = j.conn.execute("SELECT * FROM outcomes").fetchone()
    assert out["exit_price"] == 1992.0 and out["pnl"] == 200.0


def test_paper_sync_price_lookup_failure_keeps_open(j):
    paper = PaperExecutor(SETTINGS, j)
    open_paper(j, paper)

    def boom(s):
        raise RuntimeError("data down")

    assert paper.sync_outcomes(boom) == 0
    assert len(j.open_positions()) == 1


# --- MT5Executor với module giả ---

class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_IOC = 2
    ORDER_FILLING_FOK = 1
    ORDER_FILLING_RETURN = 3
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_OUT = 1

    def __init__(self):
        self.sent = []
        self.retcode = self.TRADE_RETCODE_DONE
        self.current_login = 12345  # khớp MT5_LOGIN trong fixture
        self.login_ok = True
        self.margin_free = 10_000.0
        self.margin_needed = 50.0
        self.pending_tickets = False

    def initialize(self, **kw):
        return True

    def login(self, acc, password=None, server=None):
        if self.login_ok:
            self.current_login = acc  # mô phỏng terminal đổi account thành công
            return True
        return False

    def shutdown(self):
        return True

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(equity=10_000.0, login=self.current_login, margin_free=self.margin_free)

    def order_calc_margin(self, order_type, symbol, lots, price):
        return self.margin_needed

    def symbol_info(self, name):
        return SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01, filling_mode=2)

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        return SimpleNamespace(ask=2000.5, bid=2000.3)

    def order_send(self, request):
        self.sent.append(request)
        return SimpleNamespace(retcode=self.retcode, order=777, price=request["price"], comment="ok")

    def positions_get(self, ticket=None):
        return []  # không còn mở

    def orders_get(self, ticket=None):
        return self.pending_tickets and [SimpleNamespace(ticket=ticket)] or []

    def history_deals_get(self, position=None):
        return [
            SimpleNamespace(profit=150.0, swap=-2.0, commission=-3.0, entry=self.DEAL_ENTRY_OUT,
                            price=2008.0)
        ]


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "Exness-MT5Trial")
    return fake


def mt5_settings(tmp_path):
    return load_settings(Path(__file__).resolve().parents[1] / "config.yaml", env_path=None, risk_profile="can_bang")


def test_mt5_place_builds_correct_request(fake_mt5, tmp_path):
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(SIG, XAU, 0.25)
    assert res.ok and res.ticket == "777"
    req = fake_mt5.sent[0]
    assert req["symbol"] == "XAUUSD"
    assert req["volume"] == 0.25
    assert req["type"] == FakeMT5.ORDER_TYPE_BUY
    assert req["sl"] == 1996.0 and req["tp"] == 2008.0  # SL/TP server-side trong order
    assert req["price"] == 2000.5  # long → ask
    assert req["magic"] == MAGIC
    assert req["type_filling"] == FakeMT5.ORDER_FILLING_IOC


def test_mt5_place_short_uses_bid(fake_mt5, tmp_path):
    ex = MT5Executor(mt5_settings(tmp_path))
    sig = Signal(action="trade", direction="short", entry=2000.0, sl=2004.0, tp=1992.0,
                 confidence=0.7, reason="t")
    ex.place(sig, XAU, 0.1)
    req = fake_mt5.sent[0]
    assert req["type"] == FakeMT5.ORDER_TYPE_SELL and req["price"] == 2000.3


def test_mt5_place_bad_retcode_fails(fake_mt5, tmp_path):
    fake_mt5.retcode = 10013
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(SIG, XAU, 0.25)
    assert not res.ok and "10013" in res.message


def test_mt5_value_per_point_from_ticks(fake_mt5, tmp_path):
    ex = MT5Executor(mt5_settings(tmp_path))
    assert ex.value_per_point(XAU) == pytest.approx(100.0)  # 1.0/0.01


def test_mt5_sync_outcomes_records_real_pnl(fake_mt5, tmp_path, j):
    ex = MT5Executor(mt5_settings(tmp_path))
    sid = j.record_signal(CAND, SIG, OK)
    j.record_order(sid, "mt5", "777", XAU.mt5, XAU.market, SIG, 0.25, 100.0)
    assert ex.sync_outcomes(j) == 1
    out = j.conn.execute("SELECT * FROM outcomes").fetchone()
    assert out["pnl"] == 145.0  # 150 - 2 swap - 3 commission
    assert out["exit_price"] == 2008.0


def test_mt5_missing_creds_raises(monkeypatch, tmp_path):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    for var in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        monkeypatch.delenv(var, raising=False)
    ex = MT5Executor(mt5_settings(tmp_path))
    with pytest.raises(ExecutorUnavailable, match="MT5_LOGIN"):
        ex.equity()


def test_mt5_wrong_account_switches_via_login(fake_mt5, tmp_path):
    fake_mt5.current_login = 88888  # terminal đang ở account khác → executor login() để đổi
    ex = MT5Executor(mt5_settings(tmp_path))
    assert ex.equity() == 10_000.0  # đổi account OK, không respawn


def test_mt5_wrong_account_and_login_fails_refused(fake_mt5, tmp_path):
    fake_mt5.current_login = 88888
    fake_mt5.login_ok = False  # đổi account FAIL → từ chối đặt lệnh
    ex = MT5Executor(mt5_settings(tmp_path))
    with pytest.raises(ExecutorUnavailable, match="88888"):
        ex.equity()


def test_mt5_attach_correct_account_no_relogin(fake_mt5, tmp_path):
    # account đã đúng sẵn → không gọi login() (tránh trigger algo-disable)
    ex = MT5Executor(mt5_settings(tmp_path))
    assert ex.equity() == 10_000.0
    assert fake_mt5.current_login == 12345  # không bị đổi — không re-login


def test_mt5_live_mode_requires_live_creds(fake_mt5, tmp_path, monkeypatch):
    for var in ("MT5_LIVE_LOGIN", "MT5_LIVE_PASSWORD", "MT5_LIVE_SERVER"):
        monkeypatch.delenv(var, raising=False)
    ex = MT5Executor(mt5_settings(tmp_path), mode="live")
    with pytest.raises(ExecutorUnavailable, match="MT5_LIVE"):
        ex.equity()


def test_mt5_margin_guard_blocks_oversized_order(fake_mt5, tmp_path):
    fake_mt5.margin_needed = 9_000.0  # > 80% của 10k free margin
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(SIG, XAU, 5.0)
    assert not res.ok and "margin" in res.message
    assert fake_mt5.sent == []  # không gửi order


def test_mt5_margin_guard_passes_normal_order(fake_mt5, tmp_path):
    ex = MT5Executor(mt5_settings(tmp_path))
    assert ex.place(SIG, XAU, 0.25).ok


# --- retest limit orders ---

def test_entry_near_market_uses_market_order(fake_mt5, tmp_path):
    # entry 2000, ask 2000.5 → lệch 0.5 < 0.10*ATR(8)=0.8 → market
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(SIG, XAU, 0.25, atr=8.0, limit_expiry_min=30)
    assert res.ok and fake_mt5.sent[0]["action"] == FakeMT5.TRADE_ACTION_DEAL


def test_entry_below_market_places_buy_limit_with_expiry(fake_mt5, tmp_path):
    # não muốn retest: entry 1995 < ask 2000.5, lệch 5.5 > 0.8 → BUY_LIMIT pending
    sig = Signal(action="trade", direction="long", entry=1995.0, sl=1990.0, tp=2005.0,
                 confidence=0.8, reason="chờ retest ema20")
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(sig, XAU, 0.25, atr=8.0, limit_expiry_min=30)
    assert res.ok and "retest" in res.message
    req = fake_mt5.sent[0]
    assert req["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert req["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT
    assert req["price"] == 1995.0 and req["sl"] == 1990.0 and req["tp"] == 2005.0
    assert req["type_time"] == FakeMT5.ORDER_TIME_SPECIFIED
    assert req["expiration"] > 0


def test_entry_above_market_short_places_sell_limit(fake_mt5, tmp_path):
    sig = Signal(action="trade", direction="short", entry=2006.0, sl=2012.0, tp=1994.0,
                 confidence=0.8, reason="retest kháng cự")
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(sig, XAU, 0.1, atr=8.0)
    assert res.ok
    assert fake_mt5.sent[0]["type"] == FakeMT5.ORDER_TYPE_SELL_LIMIT


def test_entry_wrong_side_falls_back_to_market(fake_mt5, tmp_path):
    # long nhưng entry TRÊN giá (2010 > ask 2000.5) — không phải limit hợp lệ → market
    sig = Signal(action="trade", direction="long", entry=2010.0, sl=2002.0, tp=2025.0,
                 confidence=0.8, reason="t")
    ex = MT5Executor(mt5_settings(tmp_path))
    res = ex.place(sig, XAU, 0.25, atr=8.0)
    assert res.ok and fake_mt5.sent[0]["action"] == FakeMT5.TRADE_ACTION_DEAL


def test_sync_pending_still_waiting_keeps_open(fake_mt5, tmp_path, j):
    fake_mt5.pending_tickets = True  # orders_get trả pending còn treo
    ex = MT5Executor(mt5_settings(tmp_path))
    sid = j.record_signal(CAND, SIG, OK)
    j.record_order(sid, "mt5", "888", XAU.mt5, XAU.market, SIG, 0.25, 100.0)
    assert ex.sync_outcomes(j) == 0
    assert len(j.open_positions(executor="mt5")) == 1  # vẫn chờ


def test_sync_expired_pending_marked_failed(fake_mt5, tmp_path, j, monkeypatch):
    monkeypatch.setattr(fake_mt5, "history_deals_get", lambda position=None: [])
    ex = MT5Executor(mt5_settings(tmp_path))
    sid = j.record_signal(CAND, SIG, OK)
    oid = j.record_order(sid, "mt5", "999", XAU.mt5, XAU.market, SIG, 0.25, 100.0)
    assert ex.sync_outcomes(j) == 0
    row = j.conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "failed"  # đóng sổ, không phải outcome thắng/thua
    assert j.open_positions(executor="mt5") == []  # hết chiếm slot risk
