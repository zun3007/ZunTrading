"""Benchmark não: haiku vs sonnet vs opus trên golden set 6 case.

Đo 4 thứ THẬT: latency, tỷ lệ JSON hợp lệ, verdict đúng trên case có đáp án rõ,
token usage. KHÔNG đo khả năng kiếm tiền — cái đó chỉ demo 2-4 tuần trả lời được.

Chạy: python benchmark/bench_models.py   (cần .env có CLAUDE_CODE_OAUTH_TOKEN)
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
load_dotenv(REPO / ".env")

from zuntrading import brain  # noqa: E402
from zuntrading.config import load_settings  # noqa: E402
from zuntrading.prefilter import Candidate  # noqa: E402

MODELS = ["haiku", "sonnet", "opus"]

def cand(**kw) -> Candidate:
    base = dict(
        symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
        direction="long", tf_entry="M15", price=2650.0, atr=8.0,
        context={"context_trend": "bullish", "rsi": 48.0, "adx": 27.0,
                 "ema20": 2652.0, "ema50": 2648.0, "ema200": 2610.0,
                 "swing_high": 2668.0, "swing_low": 2631.0},
    )
    base.update(kw)
    return Candidate(**base)

# (tên, candidate, đáp_án) — đáp_án: "trade" | "skip" | None (mơ hồ, không chấm)
CASES = [
    ("BAD-counter-trend", cand(
        context={"context_trend": "bearish", "rsi": 76.0, "adx": 12.0,
                 "ema20": 2640.0, "ema50": 2655.0, "ema200": 2670.0,
                 "swing_high": 2668.0, "swing_low": 2631.0}), "skip"),
    ("BAD-mid-range-chop", cand(
        setup_type="range_edge",
        context={"context_trend": "flat", "rsi": 51.0, "adx": 34.0,
                 "ema20": 2649.0, "ema50": 2650.0, "ema200": 2651.0,
                 "swing_high": 2690.0, "swing_low": 2610.0}), "skip"),
    ("GOOD-clean-pullback", cand(), "trade"),
    ("GOOD-breakout-momentum", cand(
        setup_type="breakout", price=2670.0,
        context={"context_trend": "bullish", "rsi": 62.0, "adx": 29.0,
                 "ema20": 2661.0, "ema50": 2652.0, "ema200": 2615.0,
                 "swing_high": 2668.0, "swing_low": 2640.0}), "trade"),
    ("AMBIG-weak-trend", cand(
        context={"context_trend": "bullish", "rsi": 47.0, "adx": 14.0,
                 "ema20": 2652.0, "ema50": 2648.0, "ema200": 2610.0,
                 "swing_high": 2668.0, "swing_low": 2631.0}), None),
    ("AMBIG-oversold-edge", cand(
        setup_type="range_edge", price=2633.0,
        context={"context_trend": "flat", "rsi": 25.0, "adx": 16.0,
                 "ema20": 2645.0, "ema50": 2648.0, "ema200": 2650.0,
                 "swing_high": 2668.0, "swing_low": 2631.0}), None),
]


def main() -> int:
    base = load_settings(REPO / "config.yaml", env_path=None, risk_profile="can_bang")
    rows = []
    for model in MODELS:
        s = dataclasses.replace(
            base, models=dataclasses.replace(base.models, decision=model, timeout_seconds=150)
        )
        for name, c, expect in CASES:
            t0 = time.perf_counter()
            sig = brain.decide(c, s)
            dt = time.perf_counter() - t0
            verdict = sig.action if sig else "INVALID"
            match = None if expect is None else (verdict == expect)
            rows.append({"model": model, "case": name, "verdict": verdict,
                         "expect": expect, "match": match, "latency_s": round(dt, 1),
                         "confidence": sig.confidence if sig else None})
            print(f"{model:7s} {name:24s} {verdict:8s} expect={expect or '—':6s} {dt:5.1f}s", flush=True)

    print("\n=== TỔNG KẾT ===")
    summary = {}
    for model in MODELS:
        mine = [r for r in rows if r["model"] == model]
        graded = [r for r in mine if r["match"] is not None]
        valid = sum(1 for r in mine if r["verdict"] != "INVALID")
        correct = sum(1 for r in graded if r["match"])
        avg_lat = sum(r["latency_s"] for r in mine) / len(mine)
        summary[model] = {
            "json_valid": f"{valid}/{len(mine)}",
            "correct_on_graded": f"{correct}/{len(graded)}",
            "avg_latency_s": round(avg_lat, 1),
        }
        print(f"{model:7s} valid={valid}/{len(mine)} đúng={correct}/{len(graded)} latency_tb={avg_lat:.1f}s")

    out = REPO / "benchmark" / "results.json"
    out.write_text(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nghi {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
