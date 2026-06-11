"""Load and validate config.yaml + .env into immutable Settings.

Fail loud: config sai thì raise ValueError ngay lúc load, không để bot chạy với config hỏng.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

VALID_SESSIONS = {"forex", "crypto", "indices"}


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float
    max_total_open_risk_pct: float
    min_rr: float
    max_trades_per_day_per_market: int
    max_open_positions_per_symbol: int
    daily_loss_stop_pct: float
    default_confidence: float
    target_winrate: float


@dataclass(frozen=True)
class ModelConfig:
    triage: str
    decision: str
    timeout_seconds: int


@dataclass(frozen=True)
class Timeframes:
    context: str
    entry: str


@dataclass(frozen=True)
class Profile:
    name: str
    timeframes: Timeframes
    scan_interval_minutes: int


@dataclass(frozen=True)
class SymbolConfig:
    mt5: str
    market: str
    session: str
    yfinance: str | None
    binance: str | None
    value_per_point: float
    lot_step: float
    min_lot: float
    max_lot: float


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @property
    def present(self) -> bool:
        return bool(self.token and self.chat_id)


@dataclass(frozen=True)
class MT5Credentials:
    login: str
    password: str
    server: str

    @property
    def present(self) -> bool:
        return bool(self.login and self.password and self.server)


@dataclass(frozen=True)
class Settings:
    reference_equity: float
    risk: RiskConfig
    risk_profile_name: str
    risk_profile_names: list[str]
    models: ModelConfig
    profiles: dict[str, Profile]
    symbols: list[SymbolConfig]
    journal_db: str
    report_at_local: str
    telegram: TelegramConfig
    mt5: MT5Credentials
    mt5_live: MT5Credentials
    anthropic_api_key: str

    def symbols_for_session_check(self) -> list[SymbolConfig]:
        return self.symbols


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(f"Config error: {msg}")


def _pct_ok(v: float) -> bool:
    return 0 < v <= 10


RISK_PROFILE_STATE = Path("data/risk_profile.json")


def _resolve_risk_profile(raw: dict, override: str | None) -> tuple[dict, str]:
    """Chọn block risk theo: override param > state file > active_risk_profile > 'risk' cũ."""
    profiles = raw.get("risk_profiles")
    if not profiles:
        return raw["risk"], "custom"  # config kiểu cũ (1 block risk)
    name = override
    if name is None and RISK_PROFILE_STATE.exists():
        try:
            import json as _json

            name = _json.loads(RISK_PROFILE_STATE.read_text(encoding="utf-8")).get("profile")
        except (OSError, ValueError):
            name = None
    if name is None:
        name = raw.get("active_risk_profile", "can_bang")
    _require(name in profiles, f"risk profile '{name}' không tồn tại (có: {sorted(profiles)})")
    return profiles[name], name


def load_settings(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
    risk_profile: str | None = None,
) -> Settings:
    config_path = Path(config_path)
    _require(config_path.exists(), f"không tìm thấy {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    if env_path is not None and Path(env_path).exists():
        load_dotenv(env_path, override=False)

    risk_raw, profile_name = _resolve_risk_profile(raw, risk_profile)
    risk = RiskConfig(
        max_risk_per_trade_pct=float(risk_raw["max_risk_per_trade_pct"]),
        max_total_open_risk_pct=float(risk_raw["max_total_open_risk_pct"]),
        min_rr=float(risk_raw["min_rr"]),
        max_trades_per_day_per_market=int(risk_raw["max_trades_per_day_per_market"]),
        max_open_positions_per_symbol=int(risk_raw["max_open_positions_per_symbol"]),
        daily_loss_stop_pct=float(risk_raw["daily_loss_stop_pct"]),
        default_confidence=float(risk_raw["default_confidence"]),
        target_winrate=float(risk_raw["target_winrate"]),
    )
    _require(_pct_ok(risk.max_risk_per_trade_pct), "max_risk_per_trade_pct phải trong (0, 10]")
    _require(_pct_ok(risk.max_total_open_risk_pct), "max_total_open_risk_pct phải trong (0, 10]")
    _require(_pct_ok(risk.daily_loss_stop_pct), "daily_loss_stop_pct phải trong (0, 10]")
    _require(risk.min_rr >= 1.0, "min_rr phải >= 1.0")
    _require(risk.max_trades_per_day_per_market >= 1, "max_trades_per_day_per_market >= 1")
    _require(0.5 <= risk.default_confidence < 1.0, "default_confidence phải trong [0.5, 1.0)")

    models_raw = raw["models"]
    models = ModelConfig(
        triage=str(models_raw["triage"]),
        decision=str(models_raw["decision"]),
        timeout_seconds=int(models_raw["timeout_seconds"]),
    )
    _require(models.timeout_seconds >= 10, "models.timeout_seconds >= 10")

    profiles: dict[str, Profile] = {}
    for pname, p in raw["profiles"].items():
        tfs = p["timeframes"]
        profiles[pname] = Profile(
            name=pname,
            timeframes=Timeframes(context=str(tfs["context"]), entry=str(tfs["entry"])),
            scan_interval_minutes=int(p["scan_interval_minutes"]),
        )
    _require(len(profiles) > 0, "cần ít nhất 1 profile")

    symbols: list[SymbolConfig] = []
    for mname, m in raw["markets"].items():
        if not m.get("enabled", False):
            continue
        session = str(m["session"])
        _require(session in VALID_SESSIONS, f"session '{session}' không hợp lệ (market {mname})")
        for s in m["symbols"]:
            sym = SymbolConfig(
                mt5=str(s["mt5"]),
                market=mname,
                session=session,
                yfinance=s.get("yfinance"),
                binance=s.get("binance"),
                value_per_point=float(s["value_per_point"]),
                lot_step=float(s["lot_step"]),
                min_lot=float(s["min_lot"]),
                max_lot=float(s["max_lot"]),
            )
            _require(sym.value_per_point > 0, f"{sym.mt5}: value_per_point > 0")
            _require(
                0 < sym.min_lot <= sym.max_lot, f"{sym.mt5}: cần 0 < min_lot <= max_lot"
            )
            _require(sym.lot_step > 0, f"{sym.mt5}: lot_step > 0")
            _require(
                bool(sym.yfinance or sym.binance),
                f"{sym.mt5}: cần ít nhất 1 nguồn data fallback (yfinance/binance)",
            )
            symbols.append(sym)
    _require(len(symbols) > 0, "không có market nào enabled")

    return Settings(
        reference_equity=float(raw["account"]["reference_equity"]),
        risk=risk,
        risk_profile_name=profile_name,
        risk_profile_names=sorted(raw.get("risk_profiles", {})),
        models=models,
        profiles=profiles,
        symbols=symbols,
        journal_db=str(raw["journal"]["db_path"]),
        report_at_local=str(raw["report"]["daily_at_local"]),
        telegram=TelegramConfig(
            token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        ),
        mt5=MT5Credentials(
            login=os.environ.get("MT5_LOGIN", ""),
            password=os.environ.get("MT5_PASSWORD", ""),
            server=os.environ.get("MT5_SERVER", ""),
        ),
        mt5_live=MT5Credentials(
            login=os.environ.get("MT5_LIVE_LOGIN", ""),
            password=os.environ.get("MT5_LIVE_PASSWORD", ""),
            server=os.environ.get("MT5_LIVE_SERVER", ""),
        ),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )
