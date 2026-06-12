"""RISK GATE — code thuần, LLM không có đường override.

Defense in depth: gate KHÔNG TIN brain đã validate gì. Mọi rule check lại từ đầu.
Một lệnh chỉ đi tiếp khi Verdict.approved=True; mọi lý do reject đều ghi rõ.

Rules (đánh số khớp spec §3):
  R1  risk/lệnh ≤ max_risk_per_trade_pct% equity (qua position sizing)
  R2  tổng risk vị thế mở + lệnh mới ≤ max_total_open_risk_pct% equity
  R3  RR ≥ min_rr
  R4a ≤ max_trades_per_day_per_market lệnh/ngày/market
  R4b ≤ max_open_positions_per_symbol vị thế mở/symbol
  R5  lỗ realized hôm nay chạm daily_loss_stop_pct% → đóng cửa tới hết ngày
  R6  confidence ≥ threshold (threshold từ calibration, truyền vào)
  R7  SL/TP tồn tại và đúng phía
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .brain import Signal
from .config import Settings, SymbolConfig


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    market: str
    risk_amount: float  # USD mất nếu chạm SL
    direction: str = "long"


def correlation_cluster(market: str) -> str:
    """Cụm tương quan: vàng+bạc chạy cùng nhịp; mỗi market khác tự là một cụm.

    R9 dùng cụm này: 2 lệnh cùng cụm cùng chiều = một ý tưởng ăn double risk."""
    return "metals" if market in ("gold", "silver") else market


@dataclass(frozen=True)
class TodayStats:
    trades_by_market: dict[str, int] = field(default_factory=dict)
    realized_pnl: float = 0.0  # USD, âm = lỗ


@dataclass(frozen=True)
class Verdict:
    approved: bool
    lots: float
    risk_amount: float
    reject_reasons: list[str]


CONF_MULT_MIN = 0.7
CONF_MULT_MAX = 1.5


def confidence_multiplier(confidence: float, threshold: float) -> float:
    """Map tuyến tính conf [threshold → 1.0] thành hệ số size [0.7 → 1.5].

    LLM KHÔNG đặt số risk — nó chỉ kéo hệ số trong dải code cho phép, và confidence
    của nó đã bị calibration giám sát (nói phét là ngưỡng tự siết).
    """
    span = 1.0 - threshold
    if span <= 0:
        return 1.0
    t = (confidence - threshold) / span
    return max(CONF_MULT_MIN, min(CONF_MULT_MAX, CONF_MULT_MIN + (CONF_MULT_MAX - CONF_MULT_MIN) * t))


def position_size(
    sig: Signal, sym: SymbolConfig, equity: float, settings: Settings,
    value_per_point: float | None = None,
    confidence_threshold: float | None = None,
) -> tuple[float, float]:
    """(lots, risk_USD) sao cho risk ≤ R1 (× hệ số confidence nếu profile bật).

    value_per_point: MT5Executor truyền tick value THẬT từ terminal; mặc định dùng config.
    """
    vpp = value_per_point if value_per_point else sym.value_per_point
    dist = abs(sig.entry - sig.sl)
    if dist <= 0 or vpp <= 0 or equity <= 0:
        return 0.0, 0.0
    budget = equity * settings.risk.max_risk_per_trade_pct / 100.0
    if settings.risk.confidence_sizing and confidence_threshold is not None:
        budget *= confidence_multiplier(sig.confidence, confidence_threshold)
    raw = budget / (dist * vpp)
    lots = math.floor(raw / sym.lot_step + 1e-9) * sym.lot_step
    lots = round(min(lots, sym.max_lot), 6)
    if lots < sym.min_lot:
        return 0.0, 0.0
    return lots, round(dist * vpp * lots, 2)


def evaluate(
    sig: Signal,
    sym: SymbolConfig,
    equity: float,
    open_positions: list[OpenPosition],
    today: TodayStats,
    confidence_threshold: float,
    settings: Settings,
    value_per_point: float | None = None,
) -> Verdict:
    reasons: list[str] = []

    # R7 — schema/phía (không tin brain)
    if sig.action != "trade":
        reasons.append("R7: không phải lệnh trade")
        return Verdict(False, 0.0, 0.0, reasons)
    if sig.direction == "long" and not (sig.sl < sig.entry < sig.tp):
        reasons.append("R7: long nhưng SL/TP sai phía")
    if sig.direction == "short" and not (sig.tp < sig.entry < sig.sl):
        reasons.append("R7: short nhưng SL/TP sai phía")
    if sig.direction not in ("long", "short"):
        reasons.append(f"R7: direction lạ '{sig.direction}'")
    if reasons:
        return Verdict(False, 0.0, 0.0, reasons)

    # R3 — reward/risk
    rr = abs(sig.tp - sig.entry) / abs(sig.entry - sig.sl)
    if rr < settings.risk.min_rr:
        reasons.append(f"R3: RR {rr:.2f} < {settings.risk.min_rr}")

    # R6 — confidence vs ngưỡng calibration
    if sig.confidence < confidence_threshold:
        reasons.append(f"R6: confidence {sig.confidence:.2f} < ngưỡng {confidence_threshold:.2f}")

    # R5 — circuit breaker theo ngày
    loss_limit = equity * settings.risk.daily_loss_stop_pct / 100.0
    if today.realized_pnl <= -loss_limit:
        reasons.append(
            f"R5: lỗ hôm nay {today.realized_pnl:.2f} chạm giới hạn -{loss_limit:.2f} — nghỉ tới mai"
        )

    # R4a — số lệnh/ngày/market
    if today.trades_by_market.get(sym.market, 0) >= settings.risk.max_trades_per_day_per_market:
        reasons.append(f"R4a: đã đủ {settings.risk.max_trades_per_day_per_market} lệnh/{sym.market} hôm nay")

    # R4b — vị thế mở cùng symbol
    same_symbol = sum(1 for p in open_positions if p.symbol == sym.mt5)
    if same_symbol >= settings.risk.max_open_positions_per_symbol:
        reasons.append(f"R4b: đã có {same_symbol} vị thế mở trên {sym.mt5}")

    # R9 — correlation guard: cùng cụm tương quan + cùng chiều = double risk một ý tưởng
    cluster = correlation_cluster(sym.market)
    twin = next(
        (p for p in open_positions
         if p.symbol != sym.mt5
         and correlation_cluster(p.market) == cluster
         and p.direction == sig.direction),
        None,
    )
    if twin is not None:
        reasons.append(
            f"R9: đã có {twin.symbol} {twin.direction} cùng cụm '{cluster}' — không chồng ý tưởng"
        )

    # R1 — sizing trong budget (× confidence nếu profile bật)
    lots, risk_amount = position_size(
        sig, sym, equity, settings, value_per_point, confidence_threshold
    )
    if lots <= 0:
        reasons.append("R1: không size được lot trong budget risk (SL quá xa hoặc equity quá nhỏ)")

    # R2 — tổng risk đang mở
    total_open = sum(p.risk_amount for p in open_positions)
    cap = equity * settings.risk.max_total_open_risk_pct / 100.0
    if lots > 0 and total_open + risk_amount > cap + 1e-9:
        reasons.append(
            f"R2: tổng risk {total_open + risk_amount:.2f} vượt trần {cap:.2f}"
        )

    if reasons:
        return Verdict(False, 0.0, 0.0, reasons)
    return Verdict(True, lots, risk_amount, [])
