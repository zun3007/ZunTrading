"""Indicators tự viết trên pandas/numpy thuần — không phụ thuộc pandas-ta.

`enrich(df)` thêm cột: ema20/50/200, rsi14, atr14, adx14, swing_high, swing_low.
Swing dùng fractal 2-2 (đỉnh/đáy cao/thấp hơn 2 nến mỗi bên), xác nhận trễ 2 nến,
forward-fill mức swing gần nhất.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

WILDER_N = 14


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _wilder(s: pd.Series, n: int = WILDER_N) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = WILDER_N) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder((-delta).clip(lower=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0)  # loss=0 (toàn tăng) → RSI 100


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = WILDER_N) -> pd.Series:
    return _wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = WILDER_N) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr_ = _wilder(true_range(df), n).replace(0, np.nan)
    plus_di = 100 * _wilder(plus_dm, n) / atr_
    minus_di = 100 * _wilder(minus_dm, n) / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx.fillna(0), n)


def swings(df: pd.DataFrame, wing: int = 2) -> tuple[pd.Series, pd.Series]:
    """Mức fractal gần nhất đã XÁC NHẬN (trễ `wing` nến), forward-fill."""
    high, low = df["high"], df["low"]
    is_sh = pd.Series(True, index=df.index)
    is_sl = pd.Series(True, index=df.index)
    for k in range(1, wing + 1):
        is_sh &= (high > high.shift(k)) & (high > high.shift(-k))
        is_sl &= (low < low.shift(k)) & (low < low.shift(-k))
    sh = high.where(is_sh).shift(wing).ffill()  # shift(wing): chỉ biết sau khi đóng đủ nến phải
    sl = low.where(is_sl).shift(wing).ffill()
    return sh, sl


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi14"] = rsi(close)
    out["atr14"] = atr(out)
    out["adx14"] = adx(out)
    out["swing_high"], out["swing_low"] = swings(out)
    return out
