"""Journal + calibration: CRUD, biên ngày giờ VN, ngưỡng theo evidence."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from zuntrading.brain import Signal
from zuntrading.calibration import threshold_for
from zuntrading.config import load_settings
from zuntrading.journal import Journal, _vn_day, today_vn
from zuntrading.prefilter import Candidate
from zuntrading.risk import Verdict

SETTINGS = load_settings(Path(__file__).resolve().parents[1] / "config.yaml", env_path=None, risk_profile="can_bang")

CAND = Candidate(
    symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
    direction="long", tf_entry="M15", price=2000.0, atr=8.0, context={},
)
SIG = Signal(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
             confidence=0.75, reason="t")
OK = Verdict(approved=True, lots=0.25, risk_amount=100.0, reject_reasons=[])


@pytest.fixture
def j(tmp_path):
    journal = Journal(tmp_path / "t.db")
    yield journal
    journal.close()


def place(j, conf=0.75, market="gold", symbol="XAUUSD", pnl=None):
    sig = Signal(**{**SIG.__dict__, "confidence": conf})
    cand = Candidate(**{**CAND.__dict__, "market": market, "symbol": symbol})
    sid = j.record_signal(cand, sig, OK)
    oid = j.record_order(sid, "paper", None, symbol, market, sig, 0.25, 100.0)
    if pnl is not None:
        j.record_outcome(oid, 2008.0 if pnl > 0 else 1996.0, pnl)
    return oid


# --- ngày VN ---

def test_vn_day_boundary():
    # 16:55 UTC = 23:55 VN cùng ngày; 17:05 UTC = 00:05 VN NGÀY SAU
    assert _vn_day("2026-06-11T16:55:00+00:00") == "2026-06-11"
    assert _vn_day("2026-06-11T17:05:00+00:00") == "2026-06-12"
    assert today_vn(datetime(2026, 6, 11, 17, 5, tzinfo=UTC)) == "2026-06-12"


# --- CRUD + stats ---

def test_signal_order_outcome_roundtrip(j):
    oid = place(j, pnl=100.0)
    stats = j.today_stats()
    assert stats.trades_by_market == {"gold": 1}
    assert stats.realized_pnl == 100.0
    assert j.open_positions() == []  # đã đóng
    row = j.conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["symbol"] == "XAUUSD" and row["status"] == "closed"


def test_open_positions_until_outcome(j):
    place(j)
    pos = j.open_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "XAUUSD" and pos[0].risk_amount == 100.0


def test_rejected_signal_recorded_without_order(j):
    bad = Verdict(approved=False, lots=0.0, risk_amount=0.0, reject_reasons=["R3: RR thấp"])
    j.record_signal(CAND, SIG, bad)
    assert j.today_stats().trades_by_market == {}
    row = j.conn.execute("SELECT * FROM signals").fetchone()
    assert row["approved"] == 0 and "R3" in row["reject_reasons"]


def test_daily_summary_counts(j):
    place(j, pnl=150.0)
    place(j, pnl=-80.0, market="forex", symbol="EURUSD")
    place(j)  # còn mở
    j.heartbeat("day", scanned=8, candidates=3, signals=3, errors=1)
    s = j.daily_summary()
    assert s["trades_closed"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 0.5
    assert s["realized_pnl"] == 70.0
    assert s["open_positions"] == 1
    assert s["heartbeats"] == 1 and s["errors"] == 1


# --- calibration ---

def fill_outcomes(j, spec):
    """spec: list[(conf, win)] — bơm outcomes đã đóng vào journal."""
    for conf, win in spec:
        place(j, conf=conf, pnl=100.0 if win else -100.0)


def test_calibration_too_few_samples_returns_default(j):
    fill_outcomes(j, [(0.8, True)] * 10)
    assert threshold_for(j, "gold", SETTINGS) == SETTINGS.risk.default_confidence


def test_calibration_tightens_when_losing(j):
    fill_outcomes(j, [(0.9, False)] * 25)  # đủ mẫu trên ngưỡng, thua sạch
    assert threshold_for(j, "gold", SETTINGS) == pytest.approx(
        SETTINGS.risk.default_confidence + 0.10
    )


def test_calibration_loosens_one_step_when_edge_band_strong(j):
    # 25 mẫu ở biên [0.65, 0.75) thắng 68% ≥ target 50% + margin 10% → nới 1 nấc 0.60
    fill_outcomes(j, [(0.68, True)] * 17 + [(0.70, False)] * 8)
    assert threshold_for(j, "gold", SETTINGS) == pytest.approx(
        SETTINGS.risk.default_confidence - 0.05
    )


def test_calibration_no_blind_loosening_without_edge_data(j):
    # thắng đậm nhưng toàn conf 0.85+ — biên thấp KHÔNG có dữ liệu → giữ default
    fill_outcomes(j, [(0.85, True)] * 30)
    assert threshold_for(j, "gold", SETTINGS) == SETTINGS.risk.default_confidence


def test_calibration_winning_but_below_margin_keeps_default(j):
    # biên đủ mẫu, win-rate 56% ≥ target nhưng < target+margin → chưa đủ bằng chứng để nới
    fill_outcomes(j, [(0.68, True)] * 14 + [(0.70, False)] * 11)
    assert threshold_for(j, "gold", SETTINGS) == SETTINGS.risk.default_confidence


def test_calibration_per_market_isolation(j):
    fill_outcomes(j, [(0.68, True)] * 25)  # toàn gold
    assert threshold_for(j, "forex", SETTINGS) == SETTINGS.risk.default_confidence


# --- trading memory ---

def test_setup_stats_empty_then_tracks(j):
    assert j.setup_stats("XAUUSD", "pullback_trend") == {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    place(j, pnl=150.0)
    place(j, pnl=-100.0)
    place(j, pnl=80.0)
    s = j.setup_stats("XAUUSD", "pullback_trend")
    assert s == {"n": 3, "wins": 2, "losses": 1, "pnl": 130.0}


def test_setup_stats_isolated_by_symbol_and_setup(j):
    place(j, pnl=100.0)  # XAUUSD pullback_trend
    assert j.setup_stats("EURUSD", "pullback_trend")["n"] == 0
    assert j.setup_stats("XAUUSD", "breakout")["n"] == 0
