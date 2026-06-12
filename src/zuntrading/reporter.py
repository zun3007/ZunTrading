"""Reporter — báo cáo ngày qua Telegram + console.

Chạy: python -m zuntrading.reporter [--config config.yaml]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from . import notify
from .config import load_settings
from .journal import Journal
from .scanner import setup_logging

WEEKLY_REVIEW_PROMPT = """Bạn là trader trưởng đang review hệ thống giao dịch tự động của CHÍNH MÌNH.
Dữ liệu {days} ngày qua (THẬT, từ journal):

Hiệu suất theo (symbol, setup): {performance}
Lý do bị risk gate chặn nhiều nhất: {rejects}
Tổng signals: {total} (được duyệt: {approved})

Viết bản tổng kết NGẮN bằng tiếng Việt, markdown, đúng 3 mục:
## Đang ăn — setup/symbol nào hoạt động, vì sao (dựa số liệu, không đoán)
## Đang hỏng — setup/symbol nào thua/bị chặn nhiều, pattern gì
## 3 đề xuất — cụ thể, đo được (ví dụ: "tắt setup X trên symbol Y", "nâng ngưỡng Z").
Lưu ý: đây là ĐỀ XUẤT cho người vận hành duyệt, không tự áp dụng. Dữ liệu ít thì nói thẳng
"chưa đủ mẫu để kết luận" thay vì bịa pattern."""


def run_weekly_review(settings, journal: Journal) -> int:
    """Não họp tổng kết tuần — 1 call Opus, lưu data/weekly_review.md, KHÔNG tự đổi config."""
    from . import brain

    d = journal.weekly_digest()
    prompt = WEEKLY_REVIEW_PROMPT.format(
        days=d["days"],
        performance=json.dumps(d["performance"], ensure_ascii=False) or "[]",
        rejects=json.dumps(d["top_rejects"], ensure_ascii=False) or "[]",
        total=d["signals_total"], approved=d["signals_approved"],
    )
    text = brain._ask(prompt, settings.models.decision, settings)  # noqa: SLF001
    if not text:
        print("weekly review: não không trả lời (xem log)")
        return 1
    out = Path("data/weekly_review.md")
    out.parent.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out.write_text(f"<!-- generated {stamp} -->\n\n{text}\n", encoding="utf-8")
    print(f"weekly review -> {out}")
    notify.send(f"🧠 <b>Tổng kết tuần</b>\n{text[:3500]}", settings)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ZunTrading reporter")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--weekly", action="store_true", help="não tổng kết tuần (1 call Opus)")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings(args.config)
    journal = Journal(settings.journal_db)
    if args.weekly:
        rc = run_weekly_review(settings, journal)
        journal.close()
        return rc
    summary = journal.daily_summary()
    text = notify.format_report(summary)
    print(text)
    sent = notify.send(text, settings)
    journal.close()
    return 0 if (sent or not settings.telegram.present) else 1


if __name__ == "__main__":
    raise SystemExit(main())
