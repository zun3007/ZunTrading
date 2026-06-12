"""Economic calendar guard (R8) — NÉ tin lớn, không trade tin.

Spread giãn + slippage + quét stop quanh NFP/FOMC/CPI giết retail nhanh hơn
mọi setup xấu. Chuyên nghiệp = đứng ngoài cửa sổ ±N phút quanh HIGH-impact event.

Nguồn: ForexFactory weekly JSON (free, không key), cache 4h tại data/.
FAIL-OPEN: calendar chết → không chặn ai, chỉ log — guard phụ không được tê liệt bot.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .config import SymbolConfig

log = logging.getLogger(__name__)

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_FILE = Path("data/calendar_cache.json")
CACHE_TTL = timedelta(hours=4)
KNOWN_CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"}


def currencies_for(sym: SymbolConfig) -> set[str]:
    """Currency nào ảnh hưởng symbol này. Crypto miễn nhiễm calendar vĩ mô (theo thiết kế)."""
    if sym.session == "crypto":
        return set()
    name = sym.mt5.upper()
    out = {p for p in (name[:3], name[3:6]) if p in KNOWN_CCY} if len(name) >= 6 else set()
    return out or {"USD"}  # vàng/bạc/dầu/indices đều neo USD


def _fetch_events() -> list[dict]:
    resp = requests.get(FF_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_high_impact_events(now: datetime | None = None, fetch=None) -> list[dict]:
    """[{title, country, ts(datetime UTC)}] HIGH-impact tuần này. Cache 4h. Lỗi → []."""
    now = now or datetime.now(UTC)
    try:
        if CACHE_FILE.exists():
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if datetime.fromisoformat(cached["fetched_at"]) > now - CACHE_TTL:
                return [
                    {**e, "ts": datetime.fromisoformat(e["ts"])} for e in cached["events"]
                ]
    except (OSError, ValueError, KeyError) as e:
        log.warning("calendar cache hỏng: %s", e)

    try:
        raw = (fetch or _fetch_events)()
        events = []
        for e in raw:
            if e.get("impact") != "High":
                continue
            try:
                ts = datetime.fromisoformat(e["date"]).astimezone(UTC)
            except (ValueError, KeyError):
                continue
            events.append({"title": e.get("title", "?"), "country": e.get("country", "?"), "ts": ts})
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(
                {"fetched_at": now.isoformat(),
                 "events": [{**e, "ts": e["ts"].isoformat()} for e in events]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info("calendar: %d HIGH-impact events tuần này", len(events))
        return events
    except Exception as e:  # noqa: BLE001 — fail-open
        log.warning("calendar fetch lỗi (fail-open, không chặn): %s", e)
        return []


def news_blackout(
    sym: SymbolConfig, window_minutes: int, now: datetime | None = None, events: list | None = None
) -> str | None:
    """Tên event nếu đang trong cửa sổ cấm của symbol này, else None."""
    now = now or datetime.now(UTC)
    ccys = currencies_for(sym)
    if not ccys:
        return None
    window = timedelta(minutes=window_minutes)
    for e in events if events is not None else get_high_impact_events(now):
        if e["country"] in ccys and abs(e["ts"] - now) <= window:
            return f"{e['country']} {e['title']} @ {e['ts']:%H:%M UTC}"
    return None
