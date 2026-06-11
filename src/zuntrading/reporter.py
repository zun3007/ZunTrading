"""Reporter — báo cáo ngày qua Telegram + console.

Chạy: python -m zuntrading.reporter [--config config.yaml]
"""

from __future__ import annotations

import argparse

from . import notify
from .config import load_settings
from .journal import Journal
from .scanner import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="ZunTrading daily report")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings(args.config)
    journal = Journal(settings.journal_db)
    summary = journal.daily_summary()
    text = notify.format_report(summary)
    print(text)
    sent = notify.send(text, settings)
    journal.close()
    return 0 if (sent or not settings.telegram.present) else 1


if __name__ == "__main__":
    raise SystemExit(main())
