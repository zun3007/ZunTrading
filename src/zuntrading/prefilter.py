"""Pre-filter thuần code: tìm setup thô TRƯỚC khi gọi LLM — không setup, không tốn token.

Input quy ước: df đã enrich, CHỈ chứa nến đã đóng (scanner chịu trách nhiệm bỏ nến đang chạy).
3 detector:
  - pullback_trend: trend khung context + giá hồi về vùng ema20–ema50 khung entry
  - range_edge:     thị trường sideways (adx thấp) + giá sát biên swing
  - breakout:       nến đóng vượt swing + adx tăng
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import SymbolConfig

RSI_PULLBACK = (35.0, 65.0)
ADX_RANGE_MAX = 20.0
ADX_BREAKOUT_MIN = 20.0
EDGE_ATR_MULT = 0.5


@dataclass(frozen=True)
class Candidate:
    symbol: str
    market: str
    profile: str
    setup_type: str
    direction: str  # "long" | "short"
    tf_entry: str
    price: float
    atr: float
    context: dict[str, Any] = field(default_factory=dict)


def _last(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-1]


def _context_trend(df_context: pd.DataFrame) -> str:
    """'bullish' | 'bearish' | 'flat' theo thứ tự ema khung context."""
    c = _last(df_context)
    if c["ema20"] > c["ema50"] > c["ema200"]:
        return "bullish"
    if c["ema20"] < c["ema50"] < c["ema200"]:
        return "bearish"
    return "flat"


def _detect_pullback(last: pd.Series, trend: str) -> str | None:
    zone_hi = max(last["ema20"], last["ema50"])
    zone_lo = min(last["ema20"], last["ema50"])
    in_zone = zone_lo <= last["close"] <= zone_hi
    rsi_ok = RSI_PULLBACK[0] <= last["rsi14"] <= RSI_PULLBACK[1]
    if not (in_zone and rsi_ok):
        return None
    if trend == "bullish":
        return "long"
    if trend == "bearish":
        return "short"
    return None


def _detect_range_edge(last: pd.Series) -> str | None:
    if last["adx14"] >= ADX_RANGE_MAX:
        return None
    band = EDGE_ATR_MULT * last["atr14"]
    if pd.notna(last["swing_low"]) and abs(last["close"] - last["swing_low"]) <= band:
        return "long"
    if pd.notna(last["swing_high"]) and abs(last["close"] - last["swing_high"]) <= band:
        return "short"
    return None


BREAKOUT_FRESH_ATR = 0.75  # close quá xa swing = breakout cũ, đuổi theo là chase đỉnh


def _detect_breakout(df_entry: pd.DataFrame) -> str | None:
    if len(df_entry) < 4:
        return None
    last = _last(df_entry)
    adx_rising = last["adx14"] > df_entry["adx14"].iloc[-4] and last["adx14"] > ADX_BREAKOUT_MIN
    if not adx_rising:
        return None
    # so với swing của NẾN TRƯỚC (mức đã tồn tại trước cú phá); chỉ nhận breakout TƯƠI
    fresh = BREAKOUT_FRESH_ATR * last["atr14"]
    prev_sh = df_entry["swing_high"].iloc[-2]
    prev_sl = df_entry["swing_low"].iloc[-2]
    if pd.notna(prev_sh) and prev_sh < last["close"] <= prev_sh + fresh:
        return "long"
    if pd.notna(prev_sl) and prev_sl - fresh <= last["close"] < prev_sl:
        return "short"
    return None


def find_candidates(
    df_context: pd.DataFrame,
    df_entry: pd.DataFrame,
    sym: SymbolConfig,
    profile_name: str,
    tf_entry: str,
) -> list[Candidate]:
    last = _last(df_entry)
    if last[["ema200", "rsi14", "atr14", "adx14"]].isna().any() or last["atr14"] <= 0:
        return []  # chưa đủ nến warm-up → không kết luận gì

    trend = _context_trend(df_context)
    found: list[Candidate] = []

    def add(setup_type: str, direction: str) -> None:
        found.append(
            Candidate(
                symbol=sym.mt5,
                market=sym.market,
                profile=profile_name,
                setup_type=setup_type,
                direction=direction,
                tf_entry=tf_entry,
                price=float(last["close"]),
                atr=float(last["atr14"]),
                context={
                    "context_trend": trend,
                    "rsi": round(float(last["rsi14"]), 1),
                    "adx": round(float(last["adx14"]), 1),
                    "ema20": float(last["ema20"]),
                    "ema50": float(last["ema50"]),
                    "ema200": float(last["ema200"]),
                    "swing_high": None if pd.isna(last["swing_high"]) else float(last["swing_high"]),
                    "swing_low": None if pd.isna(last["swing_low"]) else float(last["swing_low"]),
                },
            )
        )

    if d := _detect_pullback(last, trend):
        add("pullback_trend", d)
    if d := _detect_range_edge(last):
        add("range_edge", d)
    if d := _detect_breakout(df_entry):
        add("breakout", d)
    return found
