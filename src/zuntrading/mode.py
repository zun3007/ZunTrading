"""Run state — mode demo/live + pause. File-based để scanner (Task Scheduler) và UI đồng bộ.

Nguyên tắc LIVE:
  - Mặc định vĩnh viễn là DEMO; file hỏng/thiếu → DEMO (fail-safe).
  - Chuyển LIVE phải qua confirm phrase + có creds live riêng (MT5_LIVE_*).
  - UI hiển thị readiness (số liệu demo) ngay lúc chuyển — quyết định là của user,
    nhưng user phải NHÌN THẤY dữ liệu trước khi gõ xác nhận.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .journal import Journal

DATA_DIR = Path("data")
MODE_FILE = DATA_DIR / "mode.json"
PAUSE_FILE = DATA_DIR / "paused.flag"

CONFIRM_PHRASE = "TRADE LIVE"
MIN_DEMO_TRADES = 20
MIN_DEMO_DAYS = 14


@dataclass(frozen=True)
class RunState:
    mode: str  # "demo" | "live"
    paused: bool


def get_mode() -> str:
    try:
        obj = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        return "live" if obj.get("mode") == "live" else "demo"
    except (OSError, json.JSONDecodeError, AttributeError):
        return "demo"  # fail-safe


def set_mode(mode: str, settings: Settings, confirm: str | None = None) -> None:
    if mode not in ("demo", "live"):
        raise ValueError(f"mode '{mode}' không hợp lệ")
    if mode == "live":
        if confirm != CONFIRM_PHRASE:
            raise ValueError(f'chuyển LIVE cần gõ đúng "{CONFIRM_PHRASE}"')
        if not settings.mt5_live.present:
            raise ValueError("thiếu MT5_LIVE_LOGIN/MT5_LIVE_PASSWORD/MT5_LIVE_SERVER trong .env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(
        json.dumps({"mode": mode, "changed_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def set_paused(paused: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSE_FILE.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    elif PAUSE_FILE.exists():
        PAUSE_FILE.unlink()


def get_state() -> RunState:
    return RunState(mode=get_mode(), paused=is_paused())


def set_risk_profile(name: str, settings: Settings) -> None:
    if name not in settings.risk_profile_names:
        raise ValueError(
            f"profile '{name}' không tồn tại (có: {settings.risk_profile_names})"
        )
    from .config import RISK_PROFILE_STATE

    RISK_PROFILE_STATE.parent.mkdir(parents=True, exist_ok=True)
    RISK_PROFILE_STATE.write_text(
        json.dumps({"profile": name, "changed_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )


def live_readiness(journal: Journal, settings: Settings) -> dict:
    """Số liệu demo + cảnh báo — hiển thị trong modal chuyển LIVE. Không chặn, chỉ soi gương."""
    rows = journal.conn.execute(
        "SELECT oc.pnl, oc.result, oc.ts_closed_utc FROM outcomes oc"
        " JOIN orders o ON o.id = oc.order_id"
    ).fetchall()
    closed = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    win_rate = round(wins / closed, 3) if closed else None
    total_pnl = round(sum(r["pnl"] for r in rows), 2)
    days = 0
    if rows:
        first = min(datetime.fromisoformat(r["ts_closed_utc"]) for r in rows)
        days = (datetime.now(UTC) - first).days

    warnings: list[str] = []
    if not settings.mt5_live.present:
        warnings.append("Chưa có tài khoản LIVE trong .env (MT5_LIVE_LOGIN/PASSWORD/SERVER)")
    if closed < MIN_DEMO_TRADES:
        warnings.append(f"Mới có {closed}/{MIN_DEMO_TRADES} lệnh demo đã đóng — mẫu quá nhỏ để kết luận")
    if days < MIN_DEMO_DAYS:
        warnings.append(f"Mới có {days}/{MIN_DEMO_DAYS} ngày dữ liệu demo")
    if win_rate is not None and win_rate < settings.risk.target_winrate:
        warnings.append(f"Win rate demo {win_rate:.0%} dưới mục tiêu {settings.risk.target_winrate:.0%}")
    if total_pnl < 0:
        warnings.append(f"Demo đang LỖ {total_pnl:+.2f} USD — live sẽ lỗ thật")

    return {
        "closed_trades": closed,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "days_of_data": days,
        "live_creds_present": settings.mt5_live.present,
        "confirm_phrase": CONFIRM_PHRASE,
        "warnings": warnings,
    }
