"""Scanner — một cycle quét đầy đủ: data → prefilter → brain → risk gate → execute → journal.

Fail-closed từng tầng: symbol lỗi không giết cycle; LLM lỗi → bỏ candidate;
risk reject → ghi journal, không lệnh. Cuối cycle luôn ghi heartbeat.

Chạy: python -m zuntrading.scanner --profile day [--executor auto|paper|mt5] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import brain, mode, notify
from .calibration import threshold_for
from .config import Settings, SymbolConfig, load_settings
from .data import get_candles, market_open, scan_window_open
from .executor import ExecutorUnavailable, MT5Executor, PaperExecutor
from .indicators import enrich
from .journal import Journal
from .news import news_blackout
from .prefilter import find_candidates
from .risk import evaluate

log = logging.getLogger(__name__)


@dataclass
class CycleStats:
    scanned: int = 0
    candidates: int = 0
    signals_approved: int = 0
    orders_placed: int = 0
    closed_by_sync: int = 0
    errors: int = 0


def setup_logging(level: int = logging.INFO) -> None:
    # console Windows mặc định cp1252 — ép utf-8 để log tiếng Việt không nổ
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    Path("logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("logs/zuntrading.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def pick_executor(settings: Settings, journal: Journal, choice: str):
    """auto: theo mode. DEMO cho phép fallback paper; LIVE thì KHÔNG BAO GIỜ fallback lặng lẽ."""
    current_mode = mode.get_mode()
    if choice == "paper":
        return PaperExecutor(settings, journal)
    if choice == "mt5":
        return MT5Executor(settings, current_mode)
    # auto
    if current_mode == "live":
        # live: MT5 hoặc không gì cả — fallback paper lặng lẽ ở mode live là silent failure
        ex = MT5Executor(settings, "live")
        ex.equity()  # raise ExecutorUnavailable nếu không sẵn sàng → abort cycle
        log.info("executor=mt5 LIVE (login %s)", settings.mt5_live.login)
        if settings.risk_profile_name == "mao_hiem":
            log.warning(
                "⚠ LIVE + profile MẠO HIỂM: risk tới 4.5%%/lệnh TIỀN THẬT, dừng ngày 8%%. "
                "Khuyến nghị live chưa có track record: chuyển 'can_bang' trên dashboard."
            )
        return ex
    if settings.mt5.present:
        try:
            ex = MT5Executor(settings, "demo")
            ex.equity()  # probe kết nối thật
            log.info("executor=mt5 (Exness demo, login %s)", settings.mt5.login)
            return ex
        except ExecutorUnavailable as e:
            log.warning("MT5 không sẵn sàng (%s) → dùng paper", e)
    else:
        log.info("chưa có MT5 creds trong .env → executor=paper")
    return PaperExecutor(settings, journal)


def _hlc_lookup(settings: Settings):
    """price_lookup cho paper sync: nhận TÊN symbol (string từ journal) → (high, low, close)."""
    by_name = {s.mt5: s for s in settings.symbols}

    def lookup(symbol: str):
        sym = by_name.get(symbol)
        if sym is None:
            raise KeyError(f"{symbol} không còn trong config — không sync được giá")
        # n=40: guard của get_candles từ chối <30 nến (đủ cho indicator) — lookup chỉ cần nến cuối
        df = get_candles(sym, "M15", 40)
        row = df.iloc[-1]
        return float(row["high"]), float(row["low"]), float(row["close"])

    return lookup


def _analyze_symbol(sym: SymbolConfig, profile_name: str, settings: Settings):
    """Phase A (thread-safe): data + indicators + prefilter — KHÔNG journal, KHÔNG LLM."""
    profile = settings.profiles[profile_name]
    tf_ctx, tf_entry = profile.timeframes.context, profile.timeframes.entry
    # nến đang chạy chưa đóng → bỏ dòng cuối trước khi phân tích
    df_ctx = enrich(get_candles(sym, tf_ctx, 260).iloc[:-1])
    df_entry = enrich(get_candles(sym, tf_entry, 260).iloc[:-1])
    return sym, find_candidates(df_ctx, df_entry, sym, profile_name, tf_entry)


def _brain_one(sym: SymbolConfig, cand, track: dict, settings: Settings):
    """Phase B (thread-safe): triage + decide — chỉ LLM, journal đã đọc xong từ trước."""
    if not brain.triage(cand, settings):
        return sym, cand, None, "triage"
    sig = brain.decide(cand, settings, track_record=track)
    if sig is None:
        return sym, cand, None, "invalid"
    if sig.action == "skip":
        return sym, cand, sig, "skip"
    return sym, cand, sig, None


def _execute_one(
    sym: SymbolConfig, cand, sig, profile_name: str, settings: Settings,
    journal: Journal, executor, equity: float, threshold: float,
    stats: CycleStats, dry_run: bool,
) -> None:
    """Phase C (TUẦN TỰ — risk gate đếm quota/positions, không được race)."""
    profile = settings.profiles[profile_name]
    vpp = executor.value_per_point(sym)
    verdict = evaluate(
        sig, sym, equity, journal.open_positions(executor=executor.name),
        journal.today_stats(executor=executor.name), threshold, settings,
        value_per_point=vpp,
    )
    journal.record_signal(cand, sig, verdict)

    if not verdict.approved:
        log.info("%s: risk gate chặn: %s", cand.symbol, "; ".join(verdict.reject_reasons))
        return

    stats.signals_approved += 1
    res = executor.place(
        sig, sym, verdict.lots,
        atr=cand.atr, limit_expiry_min=profile.scan_interval_minutes * 2,
    )
    if not res.ok:
        stats.errors += 1
        log.error("%s: đặt lệnh FAIL: %s", cand.symbol, res.message)
        if not dry_run:
            notify.alert(f"Đặt lệnh {cand.symbol} fail: {res.message}", settings)
        return

    sid_row = journal.conn.execute("SELECT MAX(id) AS m FROM signals").fetchone()
    journal.record_order(
        int(sid_row["m"]), executor.name, res.ticket, sym.mt5, sym.market,
        sig, verdict.lots, verdict.risk_amount,
    )
    stats.orders_placed += 1
    text = notify.format_signal(sig, sym, verdict.lots, profile_name, executor.name, res.ticket)
    log.info("ORDER: %s", text.replace("\n", " | "))
    if not dry_run:
        notify.send(text, settings)


def run_cycle(
    profile_name: str, settings: Settings, journal: Journal, executor, dry_run: bool = False
) -> CycleStats:
    stats = CycleStats()

    if mode.is_paused():
        log.info("bot đang PAUSED — bỏ cycle %s", profile_name)
        journal.heartbeat(profile_name, 0, 0, 0, 0)
        return stats

    # 0. chốt outcome các lệnh đã đóng từ cycle trước — sync CẢ paper lẫn executor
    # active (lệnh paper cũ phải được đóng dần kể cả khi bot đã chuyển sang MT5)
    try:
        paper_sync = (
            executor if isinstance(executor, PaperExecutor) else PaperExecutor(settings, journal)
        )
        stats.closed_by_sync = paper_sync.sync_outcomes(_hlc_lookup(settings))
        if not isinstance(executor, PaperExecutor):
            stats.closed_by_sync += executor.sync_outcomes(journal)
    except Exception as e:  # noqa: BLE001
        stats.errors += 1
        log.error("sync outcomes lỗi: %s", e)

    # 1. equity — không có equity thật thì không trade gì cả (fail-closed)
    try:
        equity = executor.equity()
    except Exception as e:  # noqa: BLE001
        stats.errors += 1
        log.error("không lấy được equity: %s — bỏ cycle", e)
        if not dry_run:
            notify.alert(f"Không lấy được equity ({e}) — bỏ cycle", settings)
        journal.heartbeat(profile_name, 0, 0, 0, stats.errors)
        return stats

    # chọn symbols (main thread): market hours + né tin
    active: list[SymbolConfig] = []
    for sym in settings.symbols:
        if not market_open(sym.session):
            continue
        if not scan_window_open(sym):
            continue  # ngoài giờ vàng của symbol — không đốt não cho chợ ngủ
        if settings.news.enabled:
            ev = news_blackout(sym, settings.news.window_minutes)
            if ev:
                log.info("%s: NÉ TIN — %s (cửa sổ ±%d')", sym.mt5, ev, settings.news.window_minutes)
                continue
        active.append(sym)
    stats.scanned = len(active)

    # Phase A — data+prefilter SONG SONG (IO bound, không journal/LLM)
    pending: list[tuple[SymbolConfig, object]] = []
    if active:
        with ThreadPoolExecutor(max_workers=min(6, len(active))) as pool:
            futures = {pool.submit(_analyze_symbol, s, profile_name, settings): s for s in active}
            for fut in as_completed(futures):
                try:
                    sym, cands = fut.result()
                    stats.candidates += len(cands)
                    pending.extend((sym, c) for c in cands)
                except Exception as e:  # noqa: BLE001 — 1 symbol lỗi không giết cycle
                    stats.errors += 1
                    log.error("symbol %s lỗi: %s", futures[fut].mt5, e)

    # đọc journal Ở MAIN THREAD (sqlite không thread-safe): track record + threshold
    tracks = [journal.setup_stats(c.symbol, c.setup_type) for _, c in pending]
    thresholds = {m: threshold_for(journal, m, settings) for m in {s.market for s, _ in pending}}

    # Phase B — não SONG SONG (3 luồng: cycle nhiều candidate nhanh ~3x)
    decisions = []
    if pending:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(_brain_one, sym, cand, tracks[i], settings)
                for i, (sym, cand) in enumerate(pending)
            ]
            for fut in as_completed(futures):
                try:
                    decisions.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    stats.errors += 1
                    log.error("brain lỗi: %s", e)

    # Phase C — risk gate + khớp lệnh TUẦN TỰ (quota/positions không được race)
    for sym, cand, sig, skip in decisions:
        if skip == "triage":
            log.info("%s %s: triage bỏ", cand.symbol, cand.setup_type)
            continue
        if skip == "invalid":
            log.info("%s %s: decision không hợp lệ/timeout → bỏ", cand.symbol, cand.setup_type)
            continue
        if skip == "skip":
            log.info("%s %s: model chọn skip (%s)", cand.symbol, cand.setup_type, sig.reason)
            continue
        try:
            _execute_one(
                sym, cand, sig, profile_name, settings, journal, executor,
                equity, thresholds[sym.market], stats, dry_run,
            )
        except Exception as e:  # noqa: BLE001
            stats.errors += 1
            log.error("execute %s lỗi: %s", cand.symbol, e)

    journal.heartbeat(
        profile_name, stats.scanned, stats.candidates, stats.signals_approved, stats.errors
    )
    log.info(
        "cycle %s xong: scanned=%d candidates=%d approved=%d orders=%d closed=%d errors=%d",
        profile_name, stats.scanned, stats.candidates, stats.signals_approved,
        stats.orders_placed, stats.closed_by_sync, stats.errors,
    )
    return stats


def run_sync_only(settings: Settings, journal: Journal, executor) -> int:
    """Chốt outcome lệnh đã đóng/pending hết hạn — KHÔNG scan, KHÔNG LLM, ~2 giây.

    Chạy mỗi 5 phút: lệnh đóng được ghi sổ nhanh, dashboard/curve tươi,
    còn não vẫn quét theo nhịp nến 15' (quét dày hơn nến = chấm lại nến cũ)."""
    closed = 0
    try:
        paper_sync = (
            executor if isinstance(executor, PaperExecutor) else PaperExecutor(settings, journal)
        )
        closed = paper_sync.sync_outcomes(_hlc_lookup(settings))
        moved = 0
        if not isinstance(executor, PaperExecutor):
            closed += executor.sync_outcomes(journal)
            moved = executor.manage_positions(journal)  # breakeven/trailing mỗi 5'
        log.info("sync-only xong: closed=%d sl_moved=%d", closed, moved)
        return 0
    except Exception as e:  # noqa: BLE001
        log.error("sync-only lỗi: %s", e)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ZunTrading scanner — 1 cycle")
    parser.add_argument("--profile", default="day", help="day | swing")
    parser.add_argument("--executor", default="auto", choices=["auto", "paper", "mt5"])
    parser.add_argument("--dry-run", action="store_true", help="không Telegram, executor=paper")
    parser.add_argument("--sync-only", action="store_true", help="chỉ chốt outcome, không scan/LLM")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings(args.config)
    if args.profile not in settings.profiles:
        log.error("profile '%s' không tồn tại", args.profile)
        return 2
    journal = Journal(settings.journal_db)
    executor = pick_executor(settings, journal, "paper" if args.dry_run else args.executor)
    if args.sync_only:
        rc = run_sync_only(settings, journal, executor)
        journal.close()
        return rc
    stats = run_cycle(args.profile, settings, journal, executor, dry_run=args.dry_run)
    journal.close()
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
