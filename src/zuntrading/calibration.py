"""Confidence calibration — không tin lời LLM suông, đối chiếu với kết quả thật.

Nguyên tắc: CHỈ điều chỉnh ngưỡng ở vùng có bằng chứng trực tiếp.
(Vùng dưới ngưỡng hiện hành không bao giờ có lệnh thật → không bao giờ nới mù vào đó.)

  - Chưa đủ mẫu               → giữ default.
  - Win-rate (conf ≥ default) < target  → SIẾT +0.10 (bot đang ảo tưởng sức mạnh).
  - Biên thấp nhất đang trade [default, default+0.1) thắng vượt target + margin
    với đủ mẫu               → NỚI đúng 1 nấc -0.05 (thận trọng, có floor).
  - Còn lại                  → giữ default.
"""

from __future__ import annotations

import logging

from .config import Settings
from .journal import Journal

log = logging.getLogger(__name__)

MIN_SAMPLES = 20
TIGHTEN_STEP = 0.10
THRESHOLD_CAP = 0.90
LOOSEN_STEP = 0.05
LOOSEN_MARGIN = 0.10
THRESHOLD_FLOOR = 0.55
EDGE_WIDTH = 0.10


def _winrate(outcomes: list[bool]) -> float:
    return sum(outcomes) / len(outcomes)


def threshold_for(journal: Journal, market: str, settings: Settings) -> float:
    """Ngưỡng confidence cho market, dựa trên outcomes đã đóng trong journal."""
    rows = journal.confidence_outcomes(market)
    default = settings.risk.default_confidence
    target = settings.risk.target_winrate

    if len(rows) < MIN_SAMPLES:
        return default

    above = [win for conf, win in rows if conf >= default]
    if len(above) >= MIN_SAMPLES and _winrate(above) < target:
        tightened = min(default + TIGHTEN_STEP, THRESHOLD_CAP)
        log.info(
            "calibration %s: winrate %.2f < target %.2f (n=%d) → siết %.2f",
            market, _winrate(above), target, len(above), tightened,
        )
        return tightened

    edge = [win for conf, win in rows if default <= conf < default + EDGE_WIDTH]
    if len(edge) >= MIN_SAMPLES and _winrate(edge) >= target + LOOSEN_MARGIN:
        loosened = max(default - LOOSEN_STEP, THRESHOLD_FLOOR)
        log.info(
            "calibration %s: biên [%.2f, %.2f) winrate %.2f (n=%d) → nới %.2f",
            market, default, default + EDGE_WIDTH, _winrate(edge), len(edge), loosened,
        )
        return loosened

    return default
