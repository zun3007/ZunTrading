"""Journal SQLite — nguồn sự thật cho mọi signal/lệnh/kết quả/heartbeat.

Ngày giao dịch tính theo giờ VN (UTC+7) để khớp rule R5 "nghỉ tới 0h hôm sau".
Volume nhỏ (vài chục dòng/ngày) → query đơn giản, lọc bằng Python khi cần.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .brain import Signal
from .prefilter import Candidate
from .risk import OpenPosition, TodayStats, Verdict

VN_OFFSET = timedelta(hours=7)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  symbol TEXT NOT NULL, market TEXT NOT NULL, profile TEXT NOT NULL,
  setup_type TEXT NOT NULL, direction TEXT NOT NULL,
  entry REAL, sl REAL, tp REAL, confidence REAL, reason TEXT,
  approved INTEGER NOT NULL, reject_reasons TEXT, lots REAL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER REFERENCES signals(id),
  ts_utc TEXT NOT NULL,
  executor TEXT NOT NULL, ticket TEXT,
  symbol TEXT NOT NULL, market TEXT NOT NULL, direction TEXT NOT NULL,
  lots REAL NOT NULL, entry REAL NOT NULL, sl REAL NOT NULL, tp REAL NOT NULL,
  risk_amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'  -- open | closed | failed
);
CREATE TABLE IF NOT EXISTS outcomes (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  ts_closed_utc TEXT NOT NULL,
  exit_price REAL NOT NULL, pnl REAL NOT NULL,
  result TEXT NOT NULL  -- win | loss | breakeven
);
CREATE TABLE IF NOT EXISTS heartbeats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL, profile TEXT NOT NULL,
  scanned INTEGER, candidates INTEGER, signals INTEGER, errors INTEGER
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _vn_day(ts_iso: str) -> str:
    dt = datetime.fromisoformat(ts_iso)
    return (dt + VN_OFFSET).date().isoformat()


def today_vn(now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) + VN_OFFSET).date().isoformat()


class Journal:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- ghi ---

    def record_signal(self, cand: Candidate, sig: Signal | None, verdict: Verdict | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (ts_utc, symbol, market, profile, setup_type, direction,"
            " entry, sl, tp, confidence, reason, approved, reject_reasons, lots)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(), cand.symbol, cand.market, cand.profile, cand.setup_type, cand.direction,
                sig.entry if sig else None, sig.sl if sig else None, sig.tp if sig else None,
                sig.confidence if sig else None, sig.reason if sig else None,
                1 if (verdict and verdict.approved) else 0,
                json.dumps(verdict.reject_reasons, ensure_ascii=False) if verdict else None,
                verdict.lots if verdict else None,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_order(
        self, signal_id: int, executor: str, ticket: str | None, symbol: str, market: str,
        sig: Signal, lots: float, risk_amount: float, status: str = "open",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders (signal_id, ts_utc, executor, ticket, symbol, market,"
            " direction, lots, entry, sl, tp, risk_amount, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal_id, _now(), executor, ticket, symbol, market, sig.direction,
                lots, sig.entry, sig.sl, sig.tp, risk_amount, status,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_outcome(self, order_id: int, exit_price: float, pnl: float) -> None:
        result = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
        self.conn.execute(
            "INSERT OR REPLACE INTO outcomes (order_id, ts_closed_utc, exit_price, pnl, result)"
            " VALUES (?,?,?,?,?)",
            (order_id, _now(), exit_price, pnl, result),
        )
        self.conn.execute("UPDATE orders SET status='closed' WHERE id=?", (order_id,))
        self.conn.commit()

    def heartbeat(self, profile: str, scanned: int, candidates: int, signals: int, errors: int) -> None:
        self.conn.execute(
            "INSERT INTO heartbeats (ts_utc, profile, scanned, candidates, signals, errors)"
            " VALUES (?,?,?,?,?,?)",
            (_now(), profile, scanned, candidates, signals, errors),
        )
        self.conn.commit()

    # --- đọc ---

    def today_stats(self, now: datetime | None = None) -> TodayStats:
        day = today_vn(now)
        trades: dict[str, int] = {}
        for r in self.conn.execute("SELECT ts_utc, market FROM orders WHERE status != 'failed'"):
            if _vn_day(r["ts_utc"]) == day:
                trades[r["market"]] = trades.get(r["market"], 0) + 1
        pnl = 0.0
        for r in self.conn.execute(
            "SELECT o.ts_closed_utc, o.pnl FROM outcomes o"
        ):
            if _vn_day(r["ts_closed_utc"]) == day:
                pnl += r["pnl"]
        return TodayStats(trades_by_market=trades, realized_pnl=round(pnl, 2))

    def open_positions(self, executor: str | None = None) -> list[OpenPosition]:
        """Vị thế mở trong sổ. executor='mt5'/'paper' để lọc — risk gate chỉ được
        đếm vị thế CỦA executor đang trade, không để lệnh mô phỏng chiếm slot thật."""
        q = "SELECT * FROM orders WHERE status='open'"
        params: tuple = ()
        if executor:
            q += " AND executor=?"
            params = (executor,)
        return [
            OpenPosition(symbol=r["symbol"], market=r["market"], risk_amount=r["risk_amount"])
            for r in self.conn.execute(q, params)
        ]

    def open_orders_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM orders WHERE status='open'"))

    def setup_stats(self, symbol: str, setup_type: str, last_n: int = 10) -> dict:
        """Track record THẬT của bot với (symbol, setup_type) — trading memory cho não.

        Não nhìn thấy chính lịch sử thắng/thua của nó trước khi quyết — setup từng
        thua liên tục trên symbol này thì phải đòi hỏi chất lượng cao hơn hẳn."""
        rows = self.conn.execute(
            "SELECT oc.pnl, oc.result FROM outcomes oc"
            " JOIN orders o ON o.id = oc.order_id"
            " JOIN signals s ON s.id = o.signal_id"
            " WHERE o.symbol = ? AND s.setup_type = ?"
            " ORDER BY oc.ts_closed_utc DESC LIMIT ?",
            (symbol, setup_type, last_n),
        ).fetchall()
        return {
            "n": len(rows),
            "wins": sum(1 for r in rows if r["result"] == "win"),
            "losses": sum(1 for r in rows if r["result"] == "loss"),
            "pnl": round(sum(r["pnl"] for r in rows), 2),
        }

    def confidence_outcomes(self, market: str) -> list[tuple[float, bool]]:
        """[(confidence, win?)] của các lệnh ĐÃ ĐÓNG thuộc market — nguyên liệu calibration."""
        rows = self.conn.execute(
            "SELECT s.confidence AS conf, oc.result AS result"
            " FROM outcomes oc JOIN orders o ON o.id = oc.order_id"
            " JOIN signals s ON s.id = o.signal_id"
            " WHERE o.market = ? AND s.confidence IS NOT NULL",
            (market,),
        )
        return [(float(r["conf"]), r["result"] == "win") for r in rows]

    def daily_summary(self, now: datetime | None = None) -> dict:
        day = today_vn(now)
        sigs = [
            r for r in self.conn.execute("SELECT * FROM signals")
            if _vn_day(r["ts_utc"]) == day
        ]
        closed = [
            r for r in self.conn.execute(
                "SELECT o.market, o.symbol, oc.pnl, oc.result, oc.ts_closed_utc"
                " FROM outcomes oc JOIN orders o ON o.id = oc.order_id"
            )
            if _vn_day(r["ts_closed_utc"]) == day
        ]
        beats = [
            r for r in self.conn.execute("SELECT * FROM heartbeats")
            if _vn_day(r["ts_utc"]) == day
        ]
        wins = sum(1 for r in closed if r["result"] == "win")
        return {
            "day_vn": day,
            "signals_total": len(sigs),
            "signals_approved": sum(1 for r in sigs if r["approved"]),
            "trades_closed": len(closed),
            "wins": wins,
            "losses": sum(1 for r in closed if r["result"] == "loss"),
            "win_rate": round(wins / len(closed), 3) if closed else None,
            "realized_pnl": round(sum(r["pnl"] for r in closed), 2),
            "open_positions": len(self.open_positions()),
            "heartbeats": len(beats),
            "errors": sum(r["errors"] or 0 for r in beats),
        }

    def dump_signal(self, cand: Candidate) -> str:
        return json.dumps(asdict(cand), ensure_ascii=False)
