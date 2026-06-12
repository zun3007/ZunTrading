"""Data layer: normalize, fallback chain, market hours — không network trong unit test."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from zuntrading.config import SymbolConfig
from zuntrading.data import (
    DataUnavailable,
    get_candles,
    market_open,
    normalize,
    resample_h4,
)

SYM = SymbolConfig(
    mt5="BTCUSD", market="crypto", session="crypto", yfinance=None, binance="BTCUSDT",
    value_per_point=1, lot_step=0.01, min_lot=0.01, max_lot=10,
)


def make_df(n=60, start="2026-06-01", freq="15min", base=100.0):
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [base + i for i in range(n)],
            "high": [base + i + 1 for i in range(n)],
            "low": [base + i - 1 for i in range(n)],
            "close": [base + i + 0.5 for i in range(n)],
            "volume": [10.0] * n,
        }
    )


class FakeSource:
    def __init__(self, df=None, exc=None):
        self.df, self.exc, self.calls = df, exc, 0

    def fetch(self, sym, timeframe, n):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.df


# --- normalize ---

def test_normalize_sorts_dedups_and_coerces():
    df = make_df(40)
    shuffled = pd.concat([df.iloc[20:], df.iloc[:20], df.iloc[5:6]])  # đảo + 1 dòng trùng
    shuffled["close"] = shuffled["close"].astype(str)  # string số → coerce
    out = normalize(shuffled)
    assert len(out) == 40
    assert out["time"].is_monotonic_increasing
    assert out["close"].dtype == float


def test_normalize_empty_raises():
    df = make_df(3)
    df["close"] = None
    with pytest.raises(DataUnavailable):
        normalize(df)


# --- resample H4 ---

def test_resample_h4_bins_4_hours():
    df = make_df(24, freq="1h", base=50)  # 24 nến H1 từ 00:00
    out = resample_h4(df)
    assert len(out) == 6
    first = out.iloc[0]
    assert first["open"] == 50.0          # open của nến 00h
    assert first["close"] == 53.5         # close của nến 03h
    assert first["high"] == 54.0          # max high 4 nến đầu
    assert first["volume"] == 40.0        # tổng volume 4 nến


# --- fallback chain ---

def test_chain_first_success_wins():
    good = FakeSource(df=make_df(60))
    never = FakeSource(df=make_df(60))
    out = get_candles(SYM, "M15", 60, sources=[good, never])
    assert len(out) == 60
    assert never.calls == 0


def test_chain_falls_through_on_error():
    bad = FakeSource(exc=RuntimeError("network down"))
    good = FakeSource(df=make_df(60))
    out = get_candles(SYM, "M15", 60, sources=[bad, good])
    assert len(out) == 60
    assert bad.calls == 1 and good.calls == 1


def test_chain_rejects_too_few_candles_then_fallback():
    thin = FakeSource(df=make_df(10))   # <30 nến → coi như fail
    good = FakeSource(df=make_df(60))
    out = get_candles(SYM, "M15", 60, sources=[thin, good])
    assert len(out) == 60


def test_chain_all_fail_raises_with_reasons():
    a = FakeSource(exc=RuntimeError("timeout"))
    b = FakeSource(exc=RuntimeError("http 500"))
    with pytest.raises(DataUnavailable, match="timeout.*http 500"):
        get_candles(SYM, "M15", 60, sources=[a, b])


def test_bad_timeframe_raises():
    with pytest.raises(ValueError, match="M5"):
        get_candles(SYM, "M5", 60, sources=[])


# --- market hours (UTC) ---

@pytest.mark.parametrize(
    ("session", "dt", "expected"),
    [
        ("crypto", datetime(2026, 6, 13, 3, 0, tzinfo=UTC), True),    # T7 — crypto vẫn mở
        ("forex", datetime(2026, 6, 10, 12, 0, tzinfo=UTC), True),    # T4 trưa
        ("forex", datetime(2026, 6, 12, 20, 59, tzinfo=UTC), True),   # T6 trước 21h
        ("forex", datetime(2026, 6, 12, 21, 0, tzinfo=UTC), False),   # T6 đúng 21h — đóng
        ("forex", datetime(2026, 6, 13, 12, 0, tzinfo=UTC), False),   # T7 — đóng
        ("forex", datetime(2026, 6, 14, 21, 0, tzinfo=UTC), False),   # CN trước 22h
        ("forex", datetime(2026, 6, 14, 22, 0, tzinfo=UTC), True),    # CN 22h — mở lại
        ("indices", datetime(2026, 6, 13, 12, 0, tzinfo=UTC), False), # T7 — đóng
    ],
)
def test_market_open(session, dt, expected):
    assert market_open(session, dt) is expected


# --- scan windows (giờ vàng per symbol) ---

def make_sym(windows):
    return SymbolConfig(
        mt5="XAUUSD", market="gold", session="forex", yfinance="GC=F", binance=None,
        value_per_point=100, lot_step=0.01, min_lot=0.01, max_lot=5, scan_windows=windows,
    )


@pytest.mark.parametrize(("windows", "hhmm", "expected"), [
    ((), (3, 0), True),                            # không khai = 24/7
    (((390, 990),), (7, 0), True),                 # 07:00 UTC trong London+NY
    (((390, 990),), (3, 0), False),                # 03:00 UTC = phiên Á chết → skip
    (((390, 990),), (16, 30), False),              # đúng giờ đóng cửa sổ — ngoài
    (((0, 240), (390, 990)), (2, 0), True),        # USDJPY: phiên Á sáng vẫn scan
    (((1320, 120),), (23, 30), True),              # window qua nửa đêm 22:00-02:00
    (((1320, 120),), (1, 0), True),
    (((1320, 120),), (3, 0), False),
])
def test_scan_window_open(windows, hhmm, expected):
    from zuntrading.data import scan_window_open

    now = datetime(2026, 6, 10, *hhmm, tzinfo=UTC)
    assert scan_window_open(make_sym(windows), now) is expected


# --- live smoke (chạy riêng: pytest -m live) ---

@pytest.mark.live
def test_binance_live_fetch():
    from zuntrading.data import BinancePublicSource

    df = BinancePublicSource().fetch(SYM, "M15", 50)
    assert len(df) == 50
    assert df["time"].is_monotonic_increasing
