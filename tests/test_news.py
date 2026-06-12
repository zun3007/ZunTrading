"""News guard: currency map, cửa sổ blackout, fail-open. Không network trong unit test."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zuntrading import news
from zuntrading.config import SymbolConfig, load_settings
from zuntrading.news import currencies_for, get_high_impact_events, news_blackout

SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config.yaml", env_path=None, risk_profile="can_bang"
)


def sym(mt5="XAUUSD", session="forex"):
    return SymbolConfig(mt5=mt5, market="x", session=session, yfinance="GC=F", binance=None,
                        value_per_point=100, lot_step=0.01, min_lot=0.01, max_lot=5)


NOW = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
CPI = {"title": "CPI m/m", "country": "USD", "ts": datetime(2026, 6, 10, 12, 30, tzinfo=UTC)}
ECB = {"title": "ECB Rate", "country": "EUR", "ts": datetime(2026, 6, 10, 12, 45, tzinfo=UTC)}


# --- currency map ---

@pytest.mark.parametrize(("name", "session", "expected"), [
    ("EURUSD", "forex", {"EUR", "USD"}),
    ("USDJPY", "forex", {"USD", "JPY"}),
    ("XAUUSD", "forex", {"USD"}),       # XAU không phải ccy → neo USD
    ("XAGUSD", "forex", {"USD"}),
    ("USOIL", "forex", {"USD"}),
    ("USTEC", "indices", {"USD"}),
    ("BTCUSD", "crypto", set()),         # crypto miễn nhiễm calendar
])
def test_currencies_for(name, session, expected):
    assert currencies_for(sym(name, session)) == expected


# --- blackout window ---

def test_blackout_inside_window():
    got = news_blackout(sym("XAUUSD"), 30, now=NOW + timedelta(minutes=20), events=[CPI])
    assert got and "CPI" in got


def test_blackout_before_event_also_blocked():
    assert news_blackout(sym("EURUSD"), 30, now=NOW - timedelta(minutes=29), events=[CPI])


def test_no_blackout_outside_window():
    assert news_blackout(sym("XAUUSD"), 30, now=NOW + timedelta(minutes=31), events=[CPI]) is None


def test_blackout_only_for_related_currency():
    # tin EUR không chặn XAUUSD (neo USD), nhưng chặn EURUSD
    assert news_blackout(sym("XAUUSD"), 30, now=ECB["ts"], events=[ECB]) is None
    assert news_blackout(sym("EURUSD"), 30, now=ECB["ts"], events=[ECB])


def test_crypto_never_blocked():
    assert news_blackout(sym("BTCUSD", "crypto"), 30, now=NOW, events=[CPI]) is None


# --- fetch/cache fail-open ---

def test_fetch_failure_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "CACHE_FILE", tmp_path / "c.json")

    def boom():
        raise RuntimeError("network down")

    assert get_high_impact_events(NOW, fetch=boom) == []  # không raise, không chặn


def test_fetch_parses_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "CACHE_FILE", tmp_path / "c.json")
    raw = [
        {"title": "CPI m/m", "country": "USD", "date": "2026-06-10T08:30:00-04:00", "impact": "High"},
        {"title": "Minor", "country": "USD", "date": "2026-06-10T09:00:00-04:00", "impact": "Low"},
    ]
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return raw

    ev = get_high_impact_events(NOW, fetch=fetch)
    assert len(ev) == 1 and ev[0]["ts"] == datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    ev2 = get_high_impact_events(NOW + timedelta(hours=1), fetch=fetch)
    assert len(ev2) == 1 and calls["n"] == 1  # lần 2 từ cache, không fetch lại


# --- config wiring ---

def test_news_config_loaded():
    assert SETTINGS.news.enabled is True
    assert SETTINGS.news.window_minutes == 30


def test_new_markets_loaded():
    names = {s.mt5 for s in SETTINGS.symbols}
    assert {"XAGUSD", "USOIL", "UKOIL", "XNGUSD"} <= names
    assert len(SETTINGS.symbols) == 12  # 8 cũ + 4 mới
