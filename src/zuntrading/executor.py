"""Executors — nơi DUY NHẤT chạm lệnh. Paper (mô phỏng) và MT5 (Exness demo).

MT5Executor đặt SL/TP đính kèm NGAY trong order (server-side):
bot crash, mất mạng, tắt máy — SL/TP vẫn sống trên server Exness.

PaperExecutor mô phỏng theo NẾN (không phải tick): chạm SL/TP xét bằng high/low
nến gần nhất; nến chạm cả hai → tính SL trước (quy ước bi quan, không tự khen).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .brain import Signal
from .config import Settings, SymbolConfig
from .journal import Journal

log = logging.getLogger(__name__)

MAGIC = 20260611  # định danh lệnh của bot trên MT5


class ExecutorUnavailable(Exception):
    """Executor không sẵn sàng (MT5 chưa cài/chưa login...)."""


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    ticket: str | None
    fill_price: float | None
    message: str


class PaperExecutor:
    """Mô phỏng trong journal — mặc định cho dry-run và giai đoạn chưa có MT5."""

    name = "paper"

    def __init__(self, settings: Settings, journal: Journal):
        self.settings = settings
        self.journal = journal
        self._seq = 0

    def equity(self) -> float:
        """Equity tham chiếu + tổng P&L paper đã đóng (equity ảo trôi theo kết quả)."""
        row = self.journal.conn.execute(
            "SELECT COALESCE(SUM(oc.pnl), 0) AS s FROM outcomes oc"
            " JOIN orders o ON o.id = oc.order_id WHERE o.executor = 'paper'"
        ).fetchone()
        return self.settings.reference_equity + float(row["s"])

    def value_per_point(self, sym: SymbolConfig) -> float | None:
        return None  # dùng giá trị config

    def place(self, sig: Signal, sym: SymbolConfig, lots: float) -> ExecutionResult:
        self._seq += 1
        ticket = f"paper-{self._seq}"
        return ExecutionResult(True, ticket, sig.entry, "paper fill tại entry")

    def sync_outcomes(self, price_lookup) -> int:
        """Xét các lệnh paper đang mở với nến mới nhất. price_lookup(symbol)->(high,low,close)."""
        closed = 0
        for row in self.journal.open_orders_rows():
            if row["executor"] != self.name:
                continue
            try:
                high, low, _close = price_lookup(row["symbol"])
            except Exception as e:  # noqa: BLE001 — thiếu giá thì để lệnh mở, thử cycle sau
                log.warning("paper sync: không lấy được giá %s: %s", row["symbol"], e)
                continue
            direction, entry = row["direction"], row["entry"]
            sl, tp, lots = row["sl"], row["tp"], row["lots"]
            sym_vpp = self._vpp_for(row["symbol"])
            exit_price = None
            if direction == "long":
                if low <= sl:
                    exit_price = sl  # SL trước — quy ước bi quan
                elif high >= tp:
                    exit_price = tp
            else:
                if high >= sl:
                    exit_price = sl
                elif low <= tp:
                    exit_price = tp
            if exit_price is None:
                continue
            sign = 1.0 if direction == "long" else -1.0
            pnl = round((exit_price - entry) * sign * lots * sym_vpp, 2)
            self.journal.record_outcome(row["id"], exit_price, pnl)
            closed += 1
            log.info("paper close #%s %s %s @ %s pnl=%.2f", row["id"], row["symbol"], direction, exit_price, pnl)
        return closed

    def _vpp_for(self, symbol: str) -> float:
        for s in self.settings.symbols:
            if s.mt5 == symbol:
                return s.value_per_point
        return 1.0


class MT5Executor:
    """Đặt lệnh thật trên terminal MT5 (Exness DEMO). Import + login lazy."""

    name = "mt5"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connected = False

    def _mt5(self):
        try:
            import MetaTrader5 as mt5
        except ImportError as e:
            raise ExecutorUnavailable("package MetaTrader5 chưa cài") from e
        if not self._connected:
            creds = self.settings.mt5
            if not creds.present:
                raise ExecutorUnavailable("thiếu MT5_LOGIN/MT5_PASSWORD/MT5_SERVER trong .env")
            ok = mt5.initialize(
                login=int(creds.login), password=creds.password, server=creds.server
            )
            if not ok:
                raise ExecutorUnavailable(f"mt5.initialize fail: {mt5.last_error()}")
            self._connected = True
        return mt5

    def equity(self) -> float:
        mt5 = self._mt5()
        info = mt5.account_info()
        if info is None:
            raise ExecutorUnavailable(f"account_info fail: {mt5.last_error()}")
        return float(info.equity)

    def value_per_point(self, sym: SymbolConfig) -> float | None:
        """USD cho 1.0 đơn vị giá × 1 lot, từ tick_value/tick_size THẬT của broker."""
        mt5 = self._mt5()
        info = mt5.symbol_info(sym.mt5)
        if info is None or info.trade_tick_size <= 0:
            return None  # fallback config
        return float(info.trade_tick_value) / float(info.trade_tick_size)

    def _filling_mode(self, mt5, sym_name: str) -> int:
        info = mt5.symbol_info(sym_name)
        fm = getattr(info, "filling_mode", 0) if info else 0
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def place(self, sig: Signal, sym: SymbolConfig, lots: float) -> ExecutionResult:
        mt5 = self._mt5()
        if not mt5.symbol_select(sym.mt5, True):
            return ExecutionResult(False, None, None, f"symbol {sym.mt5} không có trên server")
        tick = mt5.symbol_info_tick(sym.mt5)
        if tick is None:
            return ExecutionResult(False, None, None, f"không có tick cho {sym.mt5}")
        is_long = sig.direction == "long"
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym.mt5,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
            "price": float(tick.ask if is_long else tick.bid),
            "sl": float(sig.sl),
            "tp": float(sig.tp),
            "deviation": 20,
            "magic": MAGIC,
            "comment": "ZunTrading",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(mt5, sym.mt5),
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(False, None, None, f"order_send None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(
                False, None, None, f"retcode {result.retcode}: {getattr(result, 'comment', '')}"
            )
        return ExecutionResult(True, str(result.order), float(result.price), "filled")

    def sync_outcomes(self, journal: Journal) -> int:
        """Lệnh mở trong journal mà MT5 không còn giữ position → đã đóng, ghi P&L thật."""
        mt5 = self._mt5()
        closed = 0
        for row in journal.open_orders_rows():
            if row["executor"] != self.name or not row["ticket"]:
                continue
            ticket = int(row["ticket"])
            if mt5.positions_get(ticket=ticket):
                continue  # vẫn mở
            deals = mt5.history_deals_get(position=ticket)
            if not deals:
                log.warning("mt5 sync: ticket %s không còn position nhưng chưa thấy deal", ticket)
                continue
            pnl = sum(float(d.profit) + float(d.swap) + float(d.commission) for d in deals)
            exit_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            exit_price = float(exit_deals[-1].price) if exit_deals else 0.0
            journal.record_outcome(row["id"], exit_price, round(pnl, 2))
            closed += 1
            log.info("mt5 close #%s ticket=%s pnl=%.2f", row["id"], ticket, pnl)
        return closed
