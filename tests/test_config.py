"""Config loader: load OK từ config thật, fail loud khi config hỏng."""

from pathlib import Path

import pytest

from zuntrading.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO_ROOT / "config.yaml"

MINIMAL = """
account: {{ reference_equity: 5000 }}
risk:
  max_risk_per_trade_pct: {risk_pct}
  max_total_open_risk_pct: 3.0
  min_rr: {min_rr}
  max_trades_per_day_per_market: 3
  max_open_positions_per_symbol: 1
  daily_loss_stop_pct: 3.0
  default_confidence: 0.65
  target_winrate: 0.5
models: {{ triage: haiku, decision: sonnet, timeout_seconds: 60 }}
profiles:
  day:
    timeframes: {{ context: H1, entry: M15 }}
    scan_interval_minutes: 15
markets:
  gold:
    enabled: {enabled}
    session: forex
    symbols:
      - {{ mt5: XAUUSD, yfinance: "GC=F", value_per_point: 100,
           lot_step: 0.01, min_lot: 0.01, max_lot: 5.0 }}
journal: {{ db_path: data/test.db }}
report: {{ daily_at_local: "21:00" }}
"""


def write_cfg(tmp_path, **kw):
    defaults = {"risk_pct": 1.0, "min_rr": 1.5, "enabled": "true"}
    defaults.update(kw)
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL.format(**defaults), encoding="utf-8")
    return p


def test_load_real_repo_config():
    s = load_settings(REAL_CONFIG, env_path=None)
    assert s.risk.max_risk_per_trade_pct == 1.0
    assert s.risk.min_rr == 1.5
    assert set(s.profiles) == {"day", "swing"}
    mt5_names = {sym.mt5 for sym in s.symbols}
    assert mt5_names == {"XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD", "USTEC", "US30"}
    # mọi symbol đều có fallback data source
    assert all(sym.yfinance or sym.binance for sym in s.symbols)


def test_minimal_config_loads(tmp_path):
    s = load_settings(write_cfg(tmp_path), env_path=None)
    assert s.reference_equity == 5000
    assert s.symbols[0].mt5 == "XAUUSD"
    assert s.symbols[0].market == "gold"
    assert s.symbols[0].session == "forex"


@pytest.mark.parametrize("bad", [0, -1, 11])
def test_invalid_risk_pct_raises(tmp_path, bad):
    with pytest.raises(ValueError, match="max_risk_per_trade_pct"):
        load_settings(write_cfg(tmp_path, risk_pct=bad), env_path=None)


def test_min_rr_below_1_raises(tmp_path):
    with pytest.raises(ValueError, match="min_rr"):
        load_settings(write_cfg(tmp_path, min_rr=0.8), env_path=None)


def test_no_enabled_market_raises(tmp_path):
    with pytest.raises(ValueError, match="enabled"):
        load_settings(write_cfg(tmp_path, enabled="false"), env_path=None)


def test_telegram_env_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    s = load_settings(write_cfg(tmp_path), env_path=None)
    assert s.telegram.present
    assert s.telegram.chat_id == "42"


def test_missing_telegram_env_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    s = load_settings(write_cfg(tmp_path), env_path=None)
    assert not s.telegram.present
    assert not s.mt5.present
