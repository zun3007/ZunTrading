"""Risk gate: mỗi rule có pass + reject, sizing đối chiếu số học tính tay, combo."""

from pathlib import Path

import pytest

from zuntrading.brain import Signal
from zuntrading.config import SymbolConfig, load_settings
from zuntrading.risk import OpenPosition, TodayStats, Verdict, evaluate, position_size

SETTINGS = load_settings(Path(__file__).resolve().parents[1] / "config.yaml", env_path=None)

XAU = SymbolConfig(
    mt5="XAUUSD", market="gold", session="forex", yfinance="GC=F", binance=None,
    value_per_point=100, lot_step=0.01, min_lot=0.01, max_lot=5.0,
)
EUR = SymbolConfig(
    mt5="EURUSD", market="forex", session="forex", yfinance="EURUSD=X", binance=None,
    value_per_point=100_000, lot_step=0.01, min_lot=0.01, max_lot=5.0,
)
BTC = SymbolConfig(
    mt5="BTCUSD", market="crypto", session="crypto", yfinance=None, binance="BTCUSDT",
    value_per_point=1, lot_step=0.01, min_lot=0.01, max_lot=10.0,
)


def sig(**kw) -> Signal:
    d = dict(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
             confidence=0.75, reason="test")
    d.update(kw)
    return Signal(**d)


def ok(v: Verdict, lots=None):
    assert v.approved, f"bị reject oan: {v.reject_reasons}"
    if lots is not None:
        assert v.lots == pytest.approx(lots)


def rejected(v: Verdict, rule: str):
    assert not v.approved
    assert any(r.startswith(rule) for r in v.reject_reasons), v.reject_reasons
    assert v.lots == 0.0


def run(s=None, sym=XAU, equity=10_000, open_pos=(), today=None, thr=0.65, vpp=None):
    return evaluate(
        s or sig(), sym, equity, list(open_pos), today or TodayStats(), thr, SETTINGS,
        value_per_point=vpp,
    )


# --- sizing số học tính tay ---

def test_sizing_xau_exact():
    # budget 1% của 10k = $100; dist 4; vpp 100 → 100/(4*100) = 0.25 lots; risk đúng $100
    lots, risk = position_size(sig(), XAU, 10_000, SETTINGS)
    assert lots == pytest.approx(0.25)
    assert risk == pytest.approx(100.0)


def test_sizing_btc_exact():
    s = sig(entry=100_000.0, sl=99_000.0, tp=102_000.0)
    lots, risk = position_size(s, BTC, 10_000, SETTINGS)
    assert lots == pytest.approx(0.1)   # 100/(1000*1)
    assert risk == pytest.approx(100.0)


def test_sizing_floors_to_lot_step():
    # dist 3 → raw 100/300 = 0.3333 → floor về 0.33; risk = 3*100*0.33 = 99 ≤ 100
    s = sig(sl=1997.0, tp=2006.0)
    lots, risk = position_size(s, XAU, 10_000, SETTINGS)
    assert lots == pytest.approx(0.33)
    assert risk == pytest.approx(99.0)


def test_sizing_caps_at_max_lot_and_shrinks_risk():
    # SL sát: dist 0.0001, vpp 100k → raw 10 lots → cap 5 → risk 0.0001*100000*5 = $50
    s = sig(entry=1.1000, sl=1.0999, tp=1.1010)
    lots, risk = position_size(s, EUR, 10_000, SETTINGS)
    assert lots == pytest.approx(5.0)
    assert risk == pytest.approx(50.0)


def test_sizing_below_min_lot_returns_zero():
    lots, risk = position_size(sig(), XAU, 100, SETTINGS)  # budget $1 → 0.0025 lot < 0.01
    assert lots == 0.0 and risk == 0.0


def test_sizing_runtime_vpp_overrides_config():
    lots, _ = position_size(sig(), XAU, 10_000, SETTINGS, value_per_point=50.0)
    assert lots == pytest.approx(0.5)  # vpp giảm nửa → lots gấp đôi


def test_sizing_degenerate_inputs_zero():
    assert position_size(sig(sl=2000.0, tp=2008.0), XAU, 10_000, SETTINGS) == (0.0, 0.0)  # dist 0
    assert position_size(sig(), XAU, 0, SETTINGS) == (0.0, 0.0)  # equity 0


# --- R7 ---

def test_r7_skip_action_rejected():
    rejected(run(sig(action="skip")), "R7")


def test_r7_long_bad_sides_rejected():
    rejected(run(sig(sl=2005.0)), "R7")


def test_r7_short_bad_sides_rejected():
    rejected(run(sig(direction="short", entry=2000.0, sl=1990.0, tp=2010.0)), "R7")


def test_r7_short_valid_passes():
    ok(run(sig(direction="short", entry=2000.0, sl=2004.0, tp=1992.0)))


def test_r7_alien_direction_rejected():
    rejected(run(sig(direction="sideways", sl=1996.0)), "R7")


# --- R3 ---

def test_r3_low_rr_rejected():
    rejected(run(sig(tp=2004.0)), "R3")  # RR = 4/4 = 1.0 < 1.5


def test_r3_exact_min_rr_passes():
    ok(run(sig(tp=2006.0)))  # RR = 6/4 = 1.5


# --- R6 ---

def test_r6_low_confidence_rejected():
    rejected(run(sig(confidence=0.5)), "R6")


def test_r6_threshold_is_parameter():
    rejected(run(thr=0.9), "R6")  # 0.75 < 0.9
    ok(run(thr=0.7))


# --- R5 ---

def test_r5_daily_loss_breached_rejects_everything():
    today = TodayStats(realized_pnl=-300.0)  # -3% của 10k
    rejected(run(today=today), "R5")


def test_r5_small_loss_passes():
    ok(run(today=TodayStats(realized_pnl=-299.0)))


# --- R4a ---

def test_r4a_market_quota_reached():
    today = TodayStats(trades_by_market={"gold": 3})
    rejected(run(today=today), "R4a")


def test_r4a_other_market_quota_irrelevant():
    ok(run(today=TodayStats(trades_by_market={"forex": 3})))


# --- R4b ---

def test_r4b_open_position_same_symbol():
    rejected(run(open_pos=[OpenPosition("XAUUSD", "gold", 50.0)]), "R4b")


def test_r4b_other_symbol_ok():
    ok(run(open_pos=[OpenPosition("EURUSD", "forex", 50.0)]))


# --- R1 ---

def test_r1_unsizeable_rejected():
    rejected(run(equity=100), "R1")


# --- R2 ---

def test_r2_total_open_risk_cap():
    # 2 vị thế mở risk 120 + 90 = 210; lệnh mới risk 100 → 310 > 300 (3% của 10k)
    open_pos = [OpenPosition("EURUSD", "forex", 120.0), OpenPosition("BTCUSD", "crypto", 90.0)]
    rejected(run(open_pos=open_pos), "R2")


def test_r2_under_cap_passes():
    ok(run(open_pos=[OpenPosition("EURUSD", "forex", 120.0)]))  # 120+100=220 ≤ 300


# --- combo: R5 thắng dù mọi thứ khác đẹp ---

def test_combo_r5_blocks_perfect_signal():
    v = run(sig(confidence=0.95), today=TodayStats(realized_pnl=-301.0))
    rejected(v, "R5")


def test_combo_multiple_reasons_all_reported():
    v = run(sig(confidence=0.5, tp=2004.0), today=TodayStats(trades_by_market={"gold": 3}))
    assert not v.approved
    rules = {r.split(":")[0] for r in v.reject_reasons}
    assert rules == {"R3", "R6", "R4a"}


def test_clean_approval_has_lots_and_no_reasons():
    v = run()
    ok(v, lots=0.25)
    assert v.risk_amount == pytest.approx(100.0)
    assert v.reject_reasons == []
