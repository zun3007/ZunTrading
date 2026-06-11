"""Mode manager + dashboard API. State files trỏ vào tmp_path — không đụng data thật."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zuntrading import api, mode
from zuntrading.brain import Signal
from zuntrading.config import load_settings
from zuntrading.journal import Journal
from zuntrading.prefilter import Candidate
from zuntrading.risk import Verdict

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

CAND = Candidate(symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
                 direction="long", tf_entry="M15", price=2000.0, atr=8.0, context={})
SIG = Signal(action="trade", direction="long", entry=2000.0, sl=1996.0, tp=2008.0,
             confidence=0.75, reason="t")
OK = Verdict(approved=True, lots=0.25, risk_amount=100.0, reject_reasons=[])


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Cô lập mode files + journal vào tmp; settings không Telegram/MT5."""
    import zuntrading.config as cfg

    monkeypatch.setattr(mode, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mode, "MODE_FILE", tmp_path / "mode.json")
    monkeypatch.setattr(mode, "PAUSE_FILE", tmp_path / "paused.flag")
    monkeypatch.setattr(cfg, "RISK_PROFILE_STATE", tmp_path / "risk_profile.json")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MT5_LOGIN", "MT5_PASSWORD",
                "MT5_SERVER", "MT5_LIVE_LOGIN", "MT5_LIVE_PASSWORD", "MT5_LIVE_SERVER"):
        monkeypatch.delenv(var, raising=False)
    settings = load_settings(REPO_CONFIG, env_path=None)
    db = tmp_path / "j.db"
    # get_settings load ĐỘNG như production — risk profile switch có hiệu lực ngay
    monkeypatch.setattr(api, "get_settings", lambda: load_settings(REPO_CONFIG, env_path=None))
    monkeypatch.setattr(api, "get_journal", lambda: Journal(db))
    return {"settings": settings, "db": db, "tmp": tmp_path}


@pytest.fixture
def client(env):
    return TestClient(api.app)


def seed(env, pnl=None):
    j = Journal(env["db"])
    sid = j.record_signal(CAND, SIG, OK)
    oid = j.record_order(sid, "paper", "paper-1", "XAUUSD", "gold", SIG, 0.25, 100.0)
    if pnl is not None:
        j.record_outcome(oid, 2008.0, pnl)
    j.close()


# --- mode manager ---

def test_default_mode_is_demo_failsafe(env):
    assert mode.get_mode() == "demo"
    mode.MODE_FILE.write_text("{hỏng json", encoding="utf-8")
    assert mode.get_mode() == "demo"  # file rác → demo


def test_live_requires_confirm_phrase(env):
    with pytest.raises(ValueError, match="TRADE LIVE"):
        mode.set_mode("live", env["settings"], confirm="yes")


def test_live_requires_live_creds(env):
    with pytest.raises(ValueError, match="MT5_LIVE"):
        mode.set_mode("live", env["settings"], confirm="TRADE LIVE")


def test_live_ok_with_creds_and_phrase(env, monkeypatch):
    monkeypatch.setenv("MT5_LIVE_LOGIN", "999")
    monkeypatch.setenv("MT5_LIVE_PASSWORD", "x")
    monkeypatch.setenv("MT5_LIVE_SERVER", "Exness-MT5Real")
    settings = load_settings(REPO_CONFIG, env_path=None)
    mode.set_mode("live", settings, confirm="TRADE LIVE")
    assert mode.get_mode() == "live"
    mode.set_mode("demo", settings)  # về demo không cần phrase
    assert mode.get_mode() == "demo"


def test_pause_roundtrip(env):
    assert mode.is_paused() is False
    mode.set_paused(True)
    assert mode.is_paused() is True
    mode.set_paused(False)
    assert mode.is_paused() is False


def test_readiness_warnings_when_no_data(env):
    j = Journal(env["db"])
    r = mode.live_readiness(j, env["settings"])
    j.close()
    assert r["closed_trades"] == 0
    assert any("MT5_LIVE" in w for w in r["warnings"])
    assert any("mẫu quá nhỏ" in w for w in r["warnings"])


def test_readiness_flags_losing_demo(env):
    seed(env, pnl=-500.0)
    j = Journal(env["db"])
    r = mode.live_readiness(j, env["settings"])
    j.close()
    assert any("LỖ" in w for w in r["warnings"])


# --- API ---

def test_status_endpoint_shape(client, env):
    seed(env)
    s = client.get("/api/status").json()
    assert s["mode"] == "demo" and s["paused"] is False
    assert s["paper_equity"] == env["settings"].reference_equity
    assert len(s["open_positions"]) == 1
    assert s["open_positions"][0]["symbol"] == "XAUUSD"
    assert s["risk"]["min_rr"] == 1.5


def test_equity_curve_endpoint(client, env):
    seed(env, pnl=200.0)
    data = client.get("/api/equity-curve?executor=paper").json()
    assert data["start"] == env["settings"].reference_equity
    assert data["points"][-1]["value"] == env["settings"].reference_equity + 200.0


def test_signals_and_orders_endpoints(client, env):
    seed(env, pnl=200.0)
    sigs = client.get("/api/signals").json()
    assert sigs and sigs[0]["symbol"] == "XAUUSD"
    orders = client.get("/api/orders").json()
    assert orders and orders[0]["pnl"] == 200.0


def test_pause_endpoint(client, env):
    assert client.post("/api/pause", json={"paused": True}).json()["paused"] is True
    assert client.post("/api/pause", json={"paused": False}).json()["paused"] is False


def test_mode_endpoint_rejects_bad_confirm(client, env):
    r = client.post("/api/mode", json={"mode": "live", "confirm": "sai"})
    assert r.status_code == 400
    assert "TRADE LIVE" in r.json()["detail"]


def test_mode_endpoint_live_roundtrip(client, env, monkeypatch):
    monkeypatch.setenv("MT5_LIVE_LOGIN", "999")
    monkeypatch.setenv("MT5_LIVE_PASSWORD", "x")
    monkeypatch.setenv("MT5_LIVE_SERVER", "Exness-MT5Real")
    settings = load_settings(REPO_CONFIG, env_path=None)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    r = client.post("/api/mode", json={"mode": "live", "confirm": "TRADE LIVE"})
    assert r.status_code == 200 and r.json()["mode"] == "live"
    assert client.get("/api/status").json()["mode"] == "live"
    client.post("/api/mode", json={"mode": "demo"})


def test_live_readiness_endpoint(client, env):
    r = client.get("/api/live-readiness").json()
    assert r["confirm_phrase"] == "TRADE LIVE"
    assert isinstance(r["warnings"], list)


def test_scan_endpoint_validates_profile(client, env):
    r = client.post("/api/scan", json={"profile": "không-tồn-tại"})
    assert r.status_code == 400


def test_risk_profile_endpoint_roundtrip(client, env):
    assert client.get("/api/status").json()["risk_profile"] == "can_bang"
    r = client.post("/api/risk-profile", json={"profile": "an_toan"})
    assert r.status_code == 200 and r.json()["risk_profile"] == "an_toan"
    s = client.get("/api/status").json()
    assert s["risk_profile"] == "an_toan"
    assert s["risk"]["max_risk_per_trade_pct"] == 0.5  # gate thật sự đổi


def test_risk_profile_endpoint_rejects_unknown(client, env):
    r = client.post("/api/risk-profile", json={"profile": "all_in"})
    assert r.status_code == 400


def test_index_serves_html(client, env):
    r = client.get("/")
    assert r.status_code == 200
    assert "ZUN" in r.text


# --- scanner tôn trọng pause ---

def test_run_cycle_skips_when_paused(env, monkeypatch):
    import dataclasses

    from zuntrading import scanner
    from zuntrading.executor import PaperExecutor

    mode.set_paused(True)
    settings = dataclasses.replace(env["settings"], symbols=env["settings"].symbols[:1])
    j = Journal(env["db"])
    called = {"n": 0}
    monkeypatch.setattr(scanner, "get_candles", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    stats = scanner.run_cycle("day", settings, j, PaperExecutor(settings, j))
    assert stats.scanned == 0 and called["n"] == 0  # không quét gì
    assert j.conn.execute("SELECT COUNT(*) c FROM heartbeats").fetchone()["c"] == 1  # vẫn heartbeat
    j.close()
