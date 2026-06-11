"""Brain: parse/validate fail-closed. Unit test KHÔNG gọi CLI thật (mock subprocess)."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from zuntrading import brain
from zuntrading.brain import Signal, _extract_json, parse_signal, triage
from zuntrading.config import load_settings
from zuntrading.prefilter import Candidate

CAND = Candidate(
    symbol="XAUUSD", market="gold", profile="day", setup_type="pullback_trend",
    direction="long", tf_entry="M15", price=2000.0, atr=8.0,
    context={"context_trend": "bullish"},
)


def sig_json(**kw):
    d = {"action": "trade", "direction": "long", "entry": 2001.0, "sl": 1993.0,
         "tp": 2017.0, "confidence": 0.72, "reason": "pullback về ema20 trong uptrend"}
    d.update(kw)
    return json.dumps(d)


# --- _extract_json ---

def test_extract_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_inside_fence_and_prose():
    text = 'Đây là phân tích:\n```json\n{"a": {"b": 2}}\n```\nxong.'
    assert _extract_json(text) == {"a": {"b": 2}}


@pytest.mark.parametrize("bad", ["", "không có json", '{"hỏng": ', '["list", "не dict"]'])
def test_extract_rejects_garbage(bad):
    assert _extract_json(bad) is None


# --- parse_signal: hợp lệ ---

def test_valid_long_trade():
    s = parse_signal(sig_json(), CAND)
    assert isinstance(s, Signal)
    assert s.action == "trade" and s.direction == "long"


def test_valid_short_trade():
    cand = Candidate(**{**CAND.__dict__, "direction": "short"})
    s = parse_signal(sig_json(direction="short", entry=1999.0, sl=2007.0, tp=1983.0), cand)
    assert s is not None and s.direction == "short"


def test_skip_is_valid_even_with_zero_prices():
    s = parse_signal(sig_json(action="skip", entry=0, sl=0, tp=0), CAND)
    assert s is not None and s.action == "skip"


# --- parse_signal: fail-closed ---

@pytest.mark.parametrize(
    "mutation",
    [
        {"action": "yolo"},                      # action lạ
        {"direction": "short"},                  # LLM tự đổi hướng
        {"confidence": 1.7},                     # ngoài [0,1]
        {"sl": 2005.0},                          # long nhưng SL trên entry
        {"tp": 1990.0},                          # long nhưng TP dưới entry
        {"entry": 2100.0},                       # lệch 100 > 3*ATR=24 → ảo giác
    ],
)
def test_invalid_trade_rejected(mutation):
    assert parse_signal(sig_json(**mutation), CAND) is None


def test_missing_field_rejected():
    d = json.loads(sig_json())
    del d["sl"]
    assert parse_signal(json.dumps(d), CAND) is None


def test_none_and_nonjson_rejected():
    assert parse_signal(None, CAND) is None
    assert parse_signal("xin lỗi tôi không chắc", CAND) is None


# --- triage qua CLI mock ---

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return load_settings(REPO_CONFIG, env_path=None, risk_profile="can_bang")


def fake_cli(result_payload, returncode=0, is_error=False, api_error_status=None):
    envelope = {"result": result_payload, "is_error": is_error}
    if api_error_status:
        envelope["api_error_status"] = api_error_status

    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stderr="", stdout=json.dumps(envelope))
    return run


def test_triage_yes(monkeypatch, settings):
    monkeypatch.setattr(brain.subprocess, "run", fake_cli('{"worth_analysis": true, "note": "ok"}'))
    assert triage(CAND, settings) is True


def test_triage_no(monkeypatch, settings):
    monkeypatch.setattr(brain.subprocess, "run", fake_cli('{"worth_analysis": false, "note": "nhiễu"}'))
    assert triage(CAND, settings) is False


def test_triage_garbage_fails_closed(monkeypatch, settings):
    monkeypatch.setattr(brain.subprocess, "run", fake_cli("tôi nghĩ là nên..."))
    assert triage(CAND, settings) is False


def test_triage_cli_error_fails_closed(monkeypatch, settings):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(brain.subprocess, "run", run)
    assert triage(CAND, settings) is False


def test_triage_timeout_fails_closed(monkeypatch, settings):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=60)
    monkeypatch.setattr(brain.subprocess, "run", run)
    assert triage(CAND, settings) is False


def test_missing_cli_fails_closed(monkeypatch, settings):
    monkeypatch.setattr(brain.shutil, "which", lambda _: None)
    assert triage(CAND, settings) is False


def test_hook_poisoned_exit_code_still_uses_valid_envelope(monkeypatch, settings):
    # hook SessionEnd fail → exit 1, nhưng envelope hợp lệ → vẫn dùng
    monkeypatch.setattr(
        brain.subprocess, "run",
        fake_cli('{"worth_analysis": true, "note": "ok"}', returncode=1),
    )
    assert triage(CAND, settings) is True


def test_api_401_fails_closed_with_clear_log(monkeypatch, settings, caplog):
    monkeypatch.setattr(
        brain.subprocess, "run",
        fake_cli("Failed to authenticate", is_error=True, api_error_status=401),
    )
    assert triage(CAND, settings) is False
    assert "CHƯA ĐĂNG NHẬP" in caplog.text


# --- live smoke (pytest -m live): gọi haiku thật, FAIL nếu CLI chưa login ---

@pytest.mark.live
def test_triage_live_real_call():
    s = load_settings(REPO_CONFIG, env_path=None, risk_profile="can_bang")
    text = brain._ask(
        brain.TRIAGE_PROMPT.format(candidate="{}"), s.models.triage, s
    )
    assert text is not None, "claude -p fail — kiểm tra `claude` CLI đã /login chưa"
    assert brain._extract_json(text) is not None, f"không phải JSON: {text[:200]}"
