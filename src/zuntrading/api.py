"""Local dashboard API — FastAPI, chạy trên máy user (mặc định 127.0.0.1:8420).

Chạy: python -m zuntrading.api  (hoặc scripts/run_ui.ps1)

Thiết kế:
  - Journal SQLite là nguồn sự thật; API chỉ đọc + điều khiển qua file state (mode/pause).
  - Scan chạy nền 1-instance-một-lúc (lock), không block HTTP.
  - Chuyển LIVE: bắt buộc confirm phrase, trả readiness để UI bắt user nhìn số liệu.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import mode as runmode
from .calibration import threshold_for
from .config import Settings, load_settings
from .executor import PaperExecutor
from .journal import Journal
from .scanner import pick_executor, run_cycle, setup_logging

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="ZunTrading", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_scan_lock = threading.Lock()
_scan_state = {"running": False, "last_result": None}
_mt5_cache: dict = {"executor": None, "mode": None}


def _mt5_executor(settings: Settings):
    from .executor import MT5Executor

    current_mode = runmode.get_mode()
    if _mt5_cache["executor"] is None or _mt5_cache["mode"] != current_mode:
        _mt5_cache["executor"] = MT5Executor(settings, current_mode)
        _mt5_cache["mode"] = current_mode
    return _mt5_cache["executor"]


def _mt5_equity(settings: Settings) -> float | None:
    """Equity THẬT từ MT5 terminal (cached connection). None nếu MT5 chưa sẵn sàng."""
    try:
        return round(_mt5_executor(settings).equity(), 2)
    except Exception:  # noqa: BLE001 — terminal tắt/chưa login
        _mt5_cache["executor"] = None  # reset để lần sau thử connect lại
        return None


def _mt5_positions(settings: Settings) -> list[dict] | None:
    """Vị thế THẬT trên sàn + floating P&L từ terminal. None nếu MT5 không sẵn sàng.

    Đây là NGUỒN SỰ THẬT khi MT5 connected — journal chỉ là sổ ghi của bot."""
    try:
        ex = _mt5_executor(settings)
        mt5 = ex._mt5()  # noqa: SLF001 — API nội bộ cùng package
        out = []
        for p in mt5.positions_get() or ():
            out.append({
                "ticket": str(p.ticket),
                "symbol": p.symbol,
                "direction": "long" if p.type == 0 else "short",
                "lots": p.volume,
                "entry": p.price_open,
                "current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit + getattr(p, "swap", 0.0), 2),  # floating P&L
                "ts": p.time,
                "is_bot": getattr(p, "magic", 0) == 20260611,
            })
        return out
    except Exception:  # noqa: BLE001
        _mt5_cache["executor"] = None
        return None


def get_settings() -> Settings:
    return load_settings()


def get_journal() -> Journal:
    return Journal(get_settings().journal_db)


class PauseBody(BaseModel):
    paused: bool


class ModeBody(BaseModel):
    mode: str
    confirm: str | None = None


class RiskProfileBody(BaseModel):
    profile: str


class MT5ConfigBody(BaseModel):
    target: str = "demo"  # demo | live
    login: str
    password: str
    server: str


ENV_PATH = Path(".env")


def _update_env_file(updates: dict[str, str]) -> None:
    """Update/append KEY=VALUE trong .env, giữ nguyên các dòng khác. Value quote JSON-style."""
    import json as _json

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        key = None
        if "=" in ln and not ln.lstrip().startswith("#"):
            key = ln.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={_json.dumps(updates[key])}")
            seen.add(key)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={_json.dumps(v)}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    load_dotenv(ENV_PATH, override=True)  # refresh env của process UI ngay lập tức


class ScanBody(BaseModel):
    profile: str = "day"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status():
    settings = get_settings()
    journal = get_journal()
    try:
        state = runmode.get_state()
        paper = PaperExecutor(settings, journal)
        today = journal.today_stats()
        hb = journal.conn.execute(
            "SELECT ts_utc, profile, errors FROM heartbeats ORDER BY id DESC LIMIT 1"
        ).fetchone()
        open_rows = [
            {
                "id": r["id"], "symbol": r["symbol"], "market": r["market"],
                "direction": r["direction"], "lots": r["lots"], "entry": r["entry"],
                "sl": r["sl"], "tp": r["tp"], "risk_amount": r["risk_amount"],
                "executor": r["executor"], "ts": r["ts_utc"],
            }
            for r in journal.open_orders_rows()
        ]
        mt5_connected = settings.mt5.present or settings.mt5_live.present
        exchange_positions = _mt5_positions(settings) if mt5_connected else None
        floating_pnl = (
            round(sum(p["profit"] for p in exchange_positions), 2)
            if exchange_positions else 0.0
        )
        # Nguồn P&L hiển thị = sổ của executor đang ACTIVE; paper là sổ phụ tách riêng
        pnl_source = "mt5" if exchange_positions is not None else "paper"
        return {
            "mode": state.mode,
            "paused": state.paused,
            "scan_running": _scan_state["running"],
            "paper_equity": paper.equity(),
            "mt5_equity": _mt5_equity(settings) if settings.mt5.present or settings.mt5_live.present else None,
            "reference_equity": settings.reference_equity,
            "pnl_source": pnl_source,
            "today": {
                "trades_by_market": today.trades_by_market,
                "realized_pnl": journal.today_stats(executor=pnl_source).realized_pnl,
                "realized_pnl_paper": journal.today_stats(executor="paper").realized_pnl,
            },
            "summary": journal.daily_summary(executor=pnl_source),
            "open_positions": open_rows,
            "exchange_positions": exchange_positions,  # None = MT5 chưa kết nối
            "floating_pnl": floating_pnl,
            "last_heartbeat": dict(hb) if hb else None,
            "risk": {
                "max_risk_per_trade_pct": settings.risk.max_risk_per_trade_pct,
                "max_total_open_risk_pct": settings.risk.max_total_open_risk_pct,
                "min_rr": settings.risk.min_rr,
                "max_trades_per_day_per_market": settings.risk.max_trades_per_day_per_market,
                "daily_loss_stop_pct": settings.risk.daily_loss_stop_pct,
            },
            "mt5_demo_configured": settings.mt5.present,
            "mt5_live_configured": settings.mt5_live.present,
            "risk_profile": settings.risk_profile_name,
            "risk_profiles": settings.risk_profile_names,
        }
    finally:
        journal.close()


@app.get("/api/equity-curve")
def equity_curve(executor: str = "paper"):
    settings = get_settings()
    journal = get_journal()
    try:
        rows = journal.conn.execute(
            "SELECT oc.ts_closed_utc AS t, oc.pnl FROM outcomes oc"
            " JOIN orders o ON o.id = oc.order_id WHERE o.executor = ?"
            " ORDER BY oc.ts_closed_utc",
            (executor,),
        ).fetchall()
        eq = settings.reference_equity
        out = []
        for r in rows:
            eq = round(eq + r["pnl"], 2)
            out.append({"time": r["t"], "value": eq})
        return {"start": settings.reference_equity, "points": out}
    finally:
        journal.close()


@app.get("/api/signals")
def signals(limit: int = 100):
    journal = get_journal()
    try:
        rows = journal.conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (min(limit, 500),)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        journal.close()


@app.get("/api/orders")
def orders(limit: int = 100):
    journal = get_journal()
    try:
        rows = journal.conn.execute(
            "SELECT o.*, oc.exit_price, oc.pnl, oc.result, oc.ts_closed_utc"
            " FROM orders o LEFT JOIN outcomes oc ON oc.order_id = o.id"
            " ORDER BY o.id DESC LIMIT ?",
            (min(limit, 500),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        journal.close()


@app.get("/api/calibration")
def calibration():
    settings = get_settings()
    journal = get_journal()
    try:
        markets = sorted({s.market for s in settings.symbols})
        return {
            m: {
                "threshold": threshold_for(journal, m, settings),
                "samples": len(journal.confidence_outcomes(m)),
            }
            for m in markets
        }
    finally:
        journal.close()


@app.get("/api/live-readiness")
def live_readiness():
    settings = get_settings()
    journal = get_journal()
    try:
        return runmode.live_readiness(journal, settings)
    finally:
        journal.close()


@app.get("/api/logs")
def logs(lines: int = 120):
    path = Path("logs/zuntrading.log")
    if not path.exists():
        return {"lines": []}
    with path.open(encoding="utf-8", errors="replace") as f:
        tail = deque(f, maxlen=min(lines, 1000))
    return {"lines": [ln.rstrip("\n") for ln in tail]}


@app.post("/api/pause")
def set_pause(body: PauseBody):
    runmode.set_paused(body.paused)
    return {"paused": runmode.is_paused()}


@app.get("/api/mt5-config")
def get_mt5_config():
    """Trả login/server hiện tại — KHÔNG BAO GIỜ trả password, chỉ trạng thái đã đặt."""
    s = get_settings()
    return {
        "demo": {"login": s.mt5.login, "server": s.mt5.server, "password_set": bool(s.mt5.password)},
        "live": {"login": s.mt5_live.login, "server": s.mt5_live.server,
                 "password_set": bool(s.mt5_live.password)},
    }


@app.post("/api/mt5-config")
def set_mt5_config(body: MT5ConfigBody):
    if body.target not in ("demo", "live"):
        raise HTTPException(status_code=400, detail="target phải là demo hoặc live")
    login = body.login.strip()
    server = body.server.strip()
    if not login.isdigit():
        raise HTTPException(status_code=400, detail="MT5 login là dãy số (xem email Exness)")
    if not server or not body.password:
        raise HTTPException(status_code=400, detail="thiếu server hoặc password")
    prefix = "MT5_" if body.target == "demo" else "MT5_LIVE_"
    _update_env_file({
        f"{prefix}LOGIN": login,
        f"{prefix}PASSWORD": body.password,
        f"{prefix}SERVER": server,
    })
    log.warning("MT5 %s config updated (login %s, server %s)", body.target, login, server)
    s = get_settings()
    return {"ok": True, "demo_configured": s.mt5.present, "live_configured": s.mt5_live.present}


@app.post("/api/risk-profile")
def set_risk_profile(body: RiskProfileBody):
    settings = get_settings()
    try:
        runmode.set_risk_profile(body.profile, settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.warning("RISK PROFILE → %s", body.profile)
    return {"risk_profile": load_settings().risk_profile_name}


@app.post("/api/mode")
def set_mode(body: ModeBody):
    settings = get_settings()
    try:
        runmode.set_mode(body.mode, settings, confirm=body.confirm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.warning("MODE CHANGED → %s", body.mode.upper())
    return {"mode": runmode.get_mode()}


def _scan_worker(profile: str) -> None:
    try:
        settings = get_settings()
        journal = Journal(settings.journal_db)
        executor = pick_executor(settings, journal, "auto")
        stats = run_cycle(profile, settings, journal, executor)
        _scan_state["last_result"] = {
            "profile": profile, "scanned": stats.scanned, "candidates": stats.candidates,
            "approved": stats.signals_approved, "orders": stats.orders_placed,
            "errors": stats.errors,
        }
        journal.close()
    except Exception as e:  # noqa: BLE001 — worker nền không được nổ process UI
        log.error("scan worker lỗi: %s", e)
        _scan_state["last_result"] = {"profile": profile, "error": str(e)}
    finally:
        _scan_state["running"] = False
        _scan_lock.release()


@app.post("/api/scan")
def scan_now(body: ScanBody):
    settings = get_settings()
    if body.profile not in settings.profiles:
        raise HTTPException(status_code=400, detail=f"profile '{body.profile}' không tồn tại")
    if not _scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="đang có cycle chạy")
    _scan_state["running"] = True
    threading.Thread(target=_scan_worker, args=(body.profile,), daemon=True).start()
    return {"started": True, "profile": body.profile}


@app.get("/api/scan/last")
def scan_last():
    return {"running": _scan_state["running"], "last_result": _scan_state["last_result"]}


def main() -> int:
    import uvicorn

    setup_logging()
    log.info("ZunTrading dashboard: http://127.0.0.1:8420")
    uvicorn.run(app, host="127.0.0.1", port=8420, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
