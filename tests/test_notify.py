"""Notify: retry, nuốt lỗi, format. Mock requests — không gọi Telegram thật."""

from pathlib import Path
from types import SimpleNamespace

from zuntrading import notify
from zuntrading.brain import Signal
from zuntrading.config import load_settings

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
SIG = Signal(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
             confidence=0.72, reason="pullback <ema20> & \"test\"")


def settings_with_tg(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    return load_settings(REPO_CONFIG, env_path=None)


def test_send_success(monkeypatch):
    s = settings_with_tg(monkeypatch)
    calls = []
    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json, timeout: calls.append((url, json)) or SimpleNamespace(status_code=200, text="ok"),
    )
    assert notify.send("hello", s) is True
    assert "bottok/sendMessage" in calls[0][0]
    assert calls[0][1]["chat_id"] == "42"


def test_send_retries_then_succeeds(monkeypatch):
    s = settings_with_tg(monkeypatch)
    monkeypatch.setattr(notify.time, "sleep", lambda x: None)
    attempts = {"n": 0}

    def post(url, json, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise notify.requests.ConnectionError("down")
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify.send("hello", s) is True
    assert attempts["n"] == 3


def test_send_all_fail_returns_false_no_raise(monkeypatch):
    s = settings_with_tg(monkeypatch)
    monkeypatch.setattr(notify.time, "sleep", lambda x: None)

    def post(url, json, timeout):
        raise notify.requests.ConnectionError("down")

    monkeypatch.setattr(notify.requests, "post", post)
    assert notify.send("hello", s) is False  # không raise — notify không giết pipeline


def test_send_skips_when_unconfigured(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings(REPO_CONFIG, env_path=None)
    assert notify.send("hello", s) is False


def test_format_signal_escapes_html_and_shows_rr(monkeypatch):
    s = settings_with_tg(monkeypatch)
    sym = next(x for x in s.symbols if x.mt5 == "XAUUSD")
    text = notify.format_signal(SIG, sym, 0.25, "day", "mt5", "777")
    assert "🟢 LONG" in text and "XAUUSD" in text
    assert "RR 2.0" in text
    assert "&lt;ema20&gt;" in text  # reason được escape
    assert "0.25 lot" in text


def test_format_report_handles_no_trades():
    text = notify.format_report(
        {"day_vn": "2026-06-11", "signals_total": 0, "signals_approved": 0,
         "trades_closed": 0, "wins": 0, "losses": 0, "win_rate": None,
         "realized_pnl": 0.0, "open_positions": 0, "heartbeats": 4, "errors": 0}
    )
    assert "WR: n/a" in text and "+0.00" in text
