"""Candle data layer với fallback chain: MT5 → Binance public → yfinance.

Mọi source trả về DataFrame chuẩn hóa: cột [time, open, high, low, close, volume],
time = datetime64 UTC, sort tăng dần, không trùng. Source fail → thử source kế.
Tất cả fail → raise DataUnavailable (fail-closed: không bao giờ trả data rỗng giả vờ OK).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
import requests

from .config import SymbolConfig

log = logging.getLogger(__name__)

REQUIRED_COLS = ["time", "open", "high", "low", "close", "volume"]
TIMEFRAMES = ("M15", "H1", "H4", "D1")


class DataUnavailable(Exception):
    """Không lấy được data từ bất kỳ source nào cho symbol/timeframe này."""


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Ép DataFrame về schema chuẩn, sort theo time, bỏ trùng, bỏ NaN giá."""
    out = df[REQUIRED_COLS].copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = (
        out.dropna(subset=["open", "high", "low", "close"])
        .drop_duplicates(subset=["time"])
        .sort_values("time")
        .reset_index(drop=True)
    )
    if out.empty:
        raise DataUnavailable("data rỗng sau khi normalize")
    return out


class BinancePublicSource:
    """Binance public REST — không cần API key."""

    BASE = "https://api.binance.com/api/v3/klines"
    INTERVAL = {"M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}

    def fetch(self, sym: SymbolConfig, timeframe: str, n: int) -> pd.DataFrame:
        if not sym.binance:
            raise DataUnavailable(f"{sym.mt5}: không có binance symbol")
        resp = requests.get(
            self.BASE,
            params={"symbol": sym.binance, "interval": self.INTERVAL[timeframe], "limit": n},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        df = pd.DataFrame(
            [
                {
                    "time": pd.to_datetime(int(r[0]), unit="ms", utc=True),
                    "open": r[1],
                    "high": r[2],
                    "low": r[3],
                    "close": r[4],
                    "volume": r[5],
                }
                for r in rows
            ]
        )
        return normalize(df)


class YFinanceSource:
    """yfinance — XAU (GC=F), forex, indices. H4 resample từ H1 (yf không có 4h)."""

    INTERVAL = {"M15": "15m", "H1": "1h", "H4": "1h", "D1": "1d"}
    PERIOD = {"M15": "5d", "H1": "1mo", "H4": "3mo", "D1": "1y"}

    def fetch(self, sym: SymbolConfig, timeframe: str, n: int) -> pd.DataFrame:
        if not sym.yfinance:
            raise DataUnavailable(f"{sym.mt5}: không có yfinance ticker")
        import yfinance as yf  # import muộn: chỉ khi cần

        hist = yf.Ticker(sym.yfinance).history(
            interval=self.INTERVAL[timeframe], period=self.PERIOD[timeframe], auto_adjust=False
        )
        if hist.empty:
            raise DataUnavailable(f"yfinance rỗng cho {sym.yfinance}")
        df = hist.reset_index()
        # yfinance đặt tên cột index khác nhau theo interval
        time_col = "Datetime" if "Datetime" in df.columns else "Date"
        df = df.rename(
            columns={
                time_col: "time",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        df = normalize(df)
        if timeframe == "H4":
            df = resample_h4(df)
        return df.tail(n).reset_index(drop=True)


class MT5Source:
    """Lấy candles thẳng từ terminal MT5 (chính xác nhất — đúng giá Exness)."""

    TF_MAP = {"M15": 15, "H1": 16385, "H4": 16388, "D1": 16408}  # mt5.TIMEFRAME_* values

    def available(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return False
        return mt5.terminal_info() is not None

    def fetch(self, sym: SymbolConfig, timeframe: str, n: int) -> pd.DataFrame:
        import MetaTrader5 as mt5

        if mt5.terminal_info() is None:
            raise DataUnavailable("MT5 terminal chưa kết nối")
        rates = mt5.copy_rates_from_pos(sym.mt5, self.TF_MAP[timeframe], 0, n)
        if rates is None or len(rates) == 0:
            raise DataUnavailable(f"MT5 không trả rates cho {sym.mt5}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        return normalize(df)


def resample_h4(df: pd.DataFrame) -> pd.DataFrame:
    """Gộp H1 → H4 (bin 4 giờ, mốc 00/04/08/12/16/20 UTC)."""
    g = df.set_index("time").resample("4h", label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).dropna(subset=["open", "close"])
    return out.reset_index()


DEFAULT_SOURCES: list = [MT5Source(), BinancePublicSource(), YFinanceSource()]


def get_candles(
    sym: SymbolConfig, timeframe: str, n: int = 200, sources: list | None = None
) -> pd.DataFrame:
    """Thử lần lượt các source, trả về df đầu tiên thành công. Tất cả fail → DataUnavailable."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe '{timeframe}' không hỗ trợ ({TIMEFRAMES})")
    errors: list[str] = []
    for src in sources if sources is not None else DEFAULT_SOURCES:
        if isinstance(src, MT5Source) and not src.available():
            errors.append("MT5Source: terminal không khả dụng")
            continue
        try:
            df = src.fetch(sym, timeframe, n)
            if len(df) < 30:
                raise DataUnavailable(f"chỉ có {len(df)} nến (<30) — không đủ tính indicator")
            return df
        except Exception as e:  # noqa: BLE001 — fallback chain cần bắt mọi lỗi source
            errors.append(f"{type(src).__name__}: {e}")
    raise DataUnavailable(f"{sym.mt5} {timeframe}: mọi source đều fail: {' | '.join(errors)}")


def market_open(session: str, now: datetime | None = None) -> bool:
    """Giờ mở cửa xấp xỉ theo session (UTC). Crypto 24/7; forex/gold/indices 24/5.

    Forex: mở Chủ nhật 22:00 UTC → đóng Thứ sáu 21:00 UTC (xấp xỉ giờ Exness).
    Indices: dùng cùng cửa sổ forex (CFD index Exness chạy gần 24/5) — xấp xỉ có chủ đích.
    """
    now = now or datetime.now(UTC)
    if session == "crypto":
        return True
    wd, hour = now.weekday(), now.hour  # Mon=0 … Sun=6
    if wd in (0, 1, 2, 3):  # T2–T5
        return True
    if wd == 4:  # T6: đóng 21:00 UTC
        return hour < 21
    if wd == 5:  # T7
        return False
    return hour >= 22  # CN: mở lại 22:00 UTC
