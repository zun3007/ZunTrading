"""Prefilter: test logic detector ở mức biên — inject chỉ số có kiểm soát."""

import pandas as pd

from zuntrading.config import SymbolConfig
from zuntrading.prefilter import find_candidates

SYM = SymbolConfig(
    mt5="XAUUSD", market="gold", session="forex", yfinance="GC=F", binance=None,
    value_per_point=100, lot_step=0.01, min_lot=0.01, max_lot=5,
)


def frame(rows: list[dict]) -> pd.DataFrame:
    """df enriched giả — mỗi dict là 1 nến với chỉ số đặt tay."""
    base = {
        "close": 2000.0, "high": 2001.0, "low": 1999.0, "open": 2000.0, "volume": 10.0,
        "ema20": 2000.0, "ema50": 1995.0, "ema200": 1980.0,
        "rsi14": 50.0, "atr14": 8.0, "adx14": 25.0,
        "swing_high": 2010.0, "swing_low": 1985.0,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def ctx(trend: str) -> pd.DataFrame:
    emas = {
        "bullish": {"ema20": 2010.0, "ema50": 2000.0, "ema200": 1980.0},
        "bearish": {"ema20": 1980.0, "ema50": 2000.0, "ema200": 2010.0},
        "flat": {"ema20": 2000.0, "ema50": 2005.0, "ema200": 1990.0},
    }[trend]
    return frame([emas])


def find(df_entry, trend="bullish"):
    return find_candidates(ctx(trend), df_entry, SYM, "day", "M15")


# --- pullback_trend ---

def test_pullback_long_in_bullish_trend():
    # giá trong vùng ema20–ema50 (1995–2000), rsi giữa → long
    df = frame([{"close": 1997.0, "adx14": 30.0}] * 4)
    got = [c for c in find(df) if c.setup_type == "pullback_trend"]
    assert len(got) == 1
    assert got[0].direction == "long"
    assert got[0].context["context_trend"] == "bullish"


def test_pullback_short_in_bearish_trend():
    df = frame([{"close": 1997.0, "ema20": 1995.0, "ema50": 2000.0, "adx14": 30.0}] * 4)
    got = [c for c in find(df, trend="bearish") if c.setup_type == "pullback_trend"]
    assert got and got[0].direction == "short"


def test_pullback_rejected_when_rsi_extreme():
    df = frame([{"close": 1997.0, "rsi14": 75.0, "adx14": 30.0}] * 4)
    assert not [c for c in find(df) if c.setup_type == "pullback_trend"]


def test_pullback_rejected_when_flat_context():
    df = frame([{"close": 1997.0, "adx14": 30.0}] * 4)
    assert not [c for c in find(df, trend="flat") if c.setup_type == "pullback_trend"]


def test_pullback_rejected_when_price_outside_zone():
    df = frame([{"close": 2005.0, "adx14": 30.0}] * 4)
    assert not [c for c in find(df) if c.setup_type == "pullback_trend"]


# --- range_edge ---

def test_range_edge_long_near_swing_low():
    # adx<20, giá cách swing_low 1985 đúng 2 (< 0.5*atr=4) → long
    df = frame([{"close": 1987.0, "adx14": 15.0, "rsi14": 70.0}] * 4)
    got = [c for c in find(df) if c.setup_type == "range_edge"]
    assert got and got[0].direction == "long"


def test_range_edge_short_near_swing_high():
    df = frame([{"close": 2008.5, "adx14": 15.0, "rsi14": 70.0}] * 4)
    got = [c for c in find(df) if c.setup_type == "range_edge"]
    assert got and got[0].direction == "short"


def test_range_edge_rejected_when_trending():
    df = frame([{"close": 1987.0, "adx14": 28.0, "rsi14": 70.0}] * 4)
    assert not [c for c in find(df) if c.setup_type == "range_edge"]


def test_range_edge_rejected_mid_range():
    df = frame([{"close": 1998.0, "adx14": 15.0, "rsi14": 70.0}] * 4)
    assert not [c for c in find(df) if c.setup_type == "range_edge"]


# --- breakout ---

def test_breakout_long_close_above_prior_swing_high():
    rows = [
        {"adx14": 18.0},
        {"adx14": 19.0},
        {"adx14": 20.0},
        # nến cuối: đóng trên swing_high nến trước (2010), adx 26 > adx[-4]=18 và > 20
        {"close": 2012.0, "adx14": 26.0, "swing_high": 2012.5, "rsi14": 70.0},
    ]
    got = [c for c in find(frame(rows)) if c.setup_type == "breakout"]
    assert got and got[0].direction == "long"


def test_breakout_rejected_without_adx_rise():
    rows = [
        {"adx14": 26.0}, {"adx14": 25.0}, {"adx14": 24.0},
        {"close": 2012.0, "adx14": 23.0, "rsi14": 70.0},  # adx giảm → loại
    ]
    assert not [c for c in find(frame(rows)) if c.setup_type == "breakout"]


def test_breakout_short_close_below_prior_swing_low():
    rows = [
        {"adx14": 18.0}, {"adx14": 19.0}, {"adx14": 20.0},
        {"close": 1983.0, "adx14": 26.0, "swing_low": 1982.5, "rsi14": 30.0},
    ]
    got = [c for c in find(frame(rows)) if c.setup_type == "breakout"]
    assert got and got[0].direction == "short"


# --- guard ---

def test_warmup_nan_returns_empty():
    df = frame([{"ema200": float("nan")}] * 4)
    assert find(df) == []


def test_multiple_setups_can_coexist():
    # pullback (giá trong zone, rsi ok) + adx thấp nhưng giá KHÔNG sát biên → chỉ pullback
    df = frame([{"close": 1996.0, "adx14": 15.0}] * 4)
    types = {c.setup_type for c in find(df)}
    assert types == {"pullback_trend"}
