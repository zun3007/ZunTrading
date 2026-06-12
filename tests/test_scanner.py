"""Scanner: pipeline mock toàn tầng — happy path, tiết kiệm token, fail-closed."""

import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from zuntrading import scanner
from zuntrading.brain import Signal
from zuntrading.config import load_settings
from zuntrading.executor import PaperExecutor
from zuntrading.journal import Journal
from zuntrading.prefilter import Candidate

FULL = load_settings(Path(__file__).resolve().parents[1] / "config.yaml", env_path=None, risk_profile="can_bang")
XAU = next(s for s in FULL.symbols if s.mt5 == "XAUUSD")
SETTINGS = dataclasses.replace(FULL, symbols=[XAU])  # 1 symbol cho test

CAND = Candidate(symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
                 direction="long", tf_entry="M15", price=2000.0, atr=8.0, context={})
GOOD_SIG = Signal(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
                  confidence=0.75, reason="t")


def fake_candles(sym, timeframe, n=200, sources=None):
    i = np.arange(60)
    closes = 2000 + 0.1 * i + np.sin(i / 3)
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-06-01", periods=60, freq="15min", tz="UTC"),
            "open": closes, "high": closes + 1, "low": closes - 1, "close": closes,
            "volume": 10.0,
        }
    )


@pytest.fixture(autouse=True)
def isolated_run_state(tmp_path, monkeypatch):
    """Pause/mode flags là runtime state của máy — test không được phụ thuộc."""
    from zuntrading import mode

    monkeypatch.setattr(mode, "DATA_DIR", tmp_path / "state")
    monkeypatch.setattr(mode, "MODE_FILE", tmp_path / "state" / "mode.json")
    monkeypatch.setattr(mode, "PAUSE_FILE", tmp_path / "state" / "paused.flag")


@pytest.fixture
def j(tmp_path):
    journal = Journal(tmp_path / "t.db")
    yield journal
    journal.close()


@pytest.fixture
def wired(monkeypatch, j):
    """Pipeline với data/market/brain mock sẵn happy path; test chỉnh từng chỗ."""
    calls = {"triage": 0, "decide": 0, "sent": []}
    monkeypatch.setattr(scanner, "get_candles", fake_candles)
    monkeypatch.setattr(scanner, "market_open", lambda session, now=None: True)
    monkeypatch.setattr(scanner, "scan_window_open", lambda sym, now=None: True)
    monkeypatch.setattr(scanner, "find_candidates", lambda *a, **k: [CAND])
    monkeypatch.setattr(
        scanner.brain, "triage",
        lambda c, s: calls.__setitem__("triage", calls["triage"] + 1) or True,
    )
    monkeypatch.setattr(
        scanner.brain, "decide",
        lambda c, s, **kw: calls.__setitem__("decide", calls["decide"] + 1) or GOOD_SIG,
    )
    monkeypatch.setattr(
        scanner.notify, "send", lambda text, s: calls["sent"].append(text) or True
    )
    monkeypatch.setattr(scanner, "news_blackout", lambda sym, w, **kw: None)  # không network
    return calls


def run(j, dry_run=False):
    ex = PaperExecutor(SETTINGS, j)
    return scanner.run_cycle("day", SETTINGS, j, ex, dry_run=dry_run)


def test_happy_path_places_order_and_notifies(wired, j):
    stats = run(j)
    assert stats.scanned == 1 and stats.candidates == 1
    assert stats.signals_approved == 1 and stats.orders_placed == 1
    assert stats.errors == 0
    assert len(j.open_positions()) == 1
    assert len(wired["sent"]) == 1 and "LONG" in wired["sent"][0]
    # heartbeat đã ghi
    hb = j.conn.execute("SELECT * FROM heartbeats").fetchone()
    assert hb["scanned"] == 1 and hb["errors"] == 0


def test_no_candidates_means_no_llm_calls(wired, monkeypatch, j):
    monkeypatch.setattr(scanner, "find_candidates", lambda *a, **k: [])
    stats = run(j)
    assert stats.candidates == 0
    assert wired["triage"] == 0 and wired["decide"] == 0  # không đốt token vô ích


def test_triage_no_skips_decision(wired, monkeypatch, j):
    # hành vi khi triage BẬT (sonnet) — bật lại để test tầng lọc chặn đúng
    import dataclasses as dc
    s_triage = dc.replace(SETTINGS, models=dc.replace(SETTINGS.models, triage="sonnet"))
    monkeypatch.setattr(scanner.brain, "triage", lambda c, s: False)
    ex = PaperExecutor(s_triage, j)
    stats = scanner.run_cycle("day", s_triage, j, ex)
    assert wired["decide"] == 0
    assert stats.signals_approved == 0 and stats.orders_placed == 0


def test_decide_none_fails_closed(wired, monkeypatch, j):
    monkeypatch.setattr(scanner.brain, "decide", lambda c, s, **kw: None)
    stats = run(j)
    assert stats.orders_placed == 0 and stats.errors == 0  # bỏ qua êm, không phải lỗi


def test_model_skip_recorded_nothing_placed(wired, monkeypatch, j):
    skip = Signal(action="skip", direction="long", entry=0, sl=0, tp=0,
                  confidence=0.8, reason="không rõ ràng")
    monkeypatch.setattr(scanner.brain, "decide", lambda c, s, **kw: skip)
    stats = run(j)
    assert stats.orders_placed == 0


def test_risk_reject_recorded_no_order(wired, monkeypatch, j):
    low_conf = dataclasses.replace(GOOD_SIG, confidence=0.5)  # < default 0.65 → R6
    monkeypatch.setattr(scanner.brain, "decide", lambda c, s, **kw: low_conf)
    stats = run(j)
    assert stats.orders_placed == 0
    row = j.conn.execute("SELECT * FROM signals").fetchone()
    assert row["approved"] == 0 and "R6" in row["reject_reasons"]


def test_symbol_exception_counted_cycle_survives(wired, monkeypatch, j):
    def boom(*a, **k):
        raise RuntimeError("data down")
    monkeypatch.setattr(scanner, "get_candles", boom)
    stats = run(j)
    assert stats.errors >= 1
    assert j.conn.execute("SELECT COUNT(*) AS c FROM heartbeats").fetchone()["c"] == 1


def test_market_closed_skips_symbol(wired, monkeypatch, j):
    monkeypatch.setattr(scanner, "market_open", lambda session, now=None: False)
    stats = run(j)
    assert stats.scanned == 0 and stats.candidates == 0


def test_dry_run_no_telegram(wired, j):
    stats = run(j, dry_run=True)
    assert stats.orders_placed == 1
    assert wired["sent"] == []  # không gửi Telegram khi dry-run


def test_second_cycle_blocked_by_r4b_open_position(wired, j):
    run(j)
    stats2 = run(j)  # cùng symbol còn mở → R4b chặn
    assert stats2.orders_placed == 0
    rows = j.conn.execute("SELECT reject_reasons FROM signals WHERE approved=0").fetchall()
    assert any("R4b" in (r["reject_reasons"] or "") for r in rows)


def test_news_blackout_skips_symbol_before_data_fetch(wired, monkeypatch, j):
    monkeypatch.setattr(scanner, "news_blackout", lambda sym, w, **kw: "USD CPI @ 12:30 UTC")
    fetches = {"n": 0}
    monkeypatch.setattr(
        scanner, "get_candles", lambda *a, **k: fetches.__setitem__("n", fetches["n"] + 1)
    )
    stats = run(j)
    assert stats.scanned == 0          # symbol bị né hoàn toàn
    assert fetches["n"] == 0           # không tốn cả data fetch
    assert wired["triage"] == 0        # càng không tốn token


def test_paper_sync_resolves_symbol_string_no_error(wired, j):
    run(j)  # mở 1 paper position XAUUSD
    stats2 = run(j)  # cycle 2: dual-sync phải resolve "XAUUSD" (string) → SymbolConfig
    assert stats2.errors == 0  # trước đây: 'str' object has no attribute 'mt5'


def test_triage_none_skips_triage_goes_straight_to_decide(wired, monkeypatch, j):
    import dataclasses as dc
    no_triage = dc.replace(SETTINGS, models=dc.replace(SETTINGS.models, triage="none"))
    monkeypatch.setattr(scanner.brain, "triage",
                        lambda c, s: (_ for _ in ()).throw(AssertionError("triage KHÔNG được gọi")))
    ex = PaperExecutor(no_triage, j)
    stats = scanner.run_cycle("day", no_triage, j, ex)
    assert wired["triage"] == 0          # triage bị bỏ qua hoàn toàn
    assert stats.orders_placed == 1      # đi thẳng decide → lệnh vẫn vào


def test_decide_receives_track_record(wired, monkeypatch, j):
    seen = {}

    def spy_decide(c, s, track_record=None):
        seen["track"] = track_record
        return GOOD_SIG

    monkeypatch.setattr(scanner.brain, "decide", spy_decide)
    run(j)
    assert seen["track"] == {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0}  # memory rỗng nhưng được truyền
