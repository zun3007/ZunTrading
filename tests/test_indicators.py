"""Indicators: đối chiếu tính chất toán học đã biết trên fixture điều khiển được."""

import numpy as np
import pandas as pd

from zuntrading.indicators import adx, atr, enrich, rsi, swings


def make_ohlc(closes, spread=1.0):
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC"),
            "open": closes.shift(1).fillna(closes.iloc[0]),
            "high": closes + spread,
            "low": closes - spread,
            "close": closes,
            "volume": 10.0,
        }
    )


def test_rsi_all_up_is_100():
    df = make_ohlc(np.arange(100, 160, 1.0))
    assert rsi(df["close"]).iloc[-1] == 100.0


def test_rsi_all_down_near_0():
    df = make_ohlc(np.arange(160, 100, -1.0))
    assert rsi(df["close"]).iloc[-1] < 1.0


def test_rsi_flat_oscillation_mid():
    closes = [100 + (1 if i % 2 else -1) for i in range(80)]
    val = rsi(make_ohlc(closes)["close"]).iloc[-1]
    assert 35 < val < 65


def test_atr_constant_range_converges_to_range():
    # mỗi nến: high-low = 2*spread, close không đổi → TR = 2 → ATR → 2
    df = make_ohlc([100.0] * 100, spread=1.0)
    assert abs(atr(df).iloc[-1] - 2.0) < 0.05


def test_adx_trend_vs_chop():
    trend = adx(make_ohlc(np.arange(100, 200, 1.0))).iloc[-1]
    chop = adx(make_ohlc([100 + (1 if i % 2 else -1) for i in range(100)])).iloc[-1]
    assert trend > 25
    assert chop < 25
    assert trend > chop


def test_swings_detects_confirmed_fractal():
    # đỉnh 110 tại i=10 (cao hơn 2 nến mỗi bên), xác nhận từ i=12
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
              110, 108, 106, 104, 102, 100, 99, 98, 97, 96]
    df = make_ohlc(closes, spread=0.5)
    sh, _sl = swings(df)
    assert np.isnan(sh.iloc[11])          # chưa xác nhận tại i=11
    assert sh.iloc[12] == 110.5           # high của đỉnh = close + spread
    assert sh.iloc[-1] == 110.5           # ffill giữ mức gần nhất


def test_enrich_adds_all_columns_no_nan_tail():
    # uptrend CÓ SÓNG — fractal cần đỉnh/đáy cục bộ, chuỗi đơn điệu tuyệt đối không có swing
    i = np.arange(250)
    df = enrich(make_ohlc(100 + 0.15 * i + 3 * np.sin(i / 4)))
    for col in ["ema20", "ema50", "ema200", "rsi14", "atr14", "adx14", "swing_high", "swing_low"]:
        assert col in df.columns
        assert pd.notna(df[col].iloc[-1]), f"{col} NaN ở nến cuối"
    last = df.iloc[-1]
    assert last["ema20"] > last["ema50"] > last["ema200"]  # uptrend mạnh → stack bullish
