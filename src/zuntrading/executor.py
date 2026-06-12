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

    def place(
        self, sig: Signal, sym: SymbolConfig, lots: float,
        atr: float | None = None, limit_expiry_min: int = 30,
    ) -> ExecutionResult:
        # paper giả định limit luôn khớp tại entry — mô phỏng lạc quan, ghi chú trong spec
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
    """Đặt lệnh trên terminal MT5 (Exness). mode='demo' dùng MT5_*, mode='live' dùng MT5_LIVE_*.

    Sau khi connect LUÔN đối chiếu account login thật của terminal với creds yêu cầu —
    không bao giờ đặt lệnh nhầm tài khoản.
    """

    name = "mt5"

    def __init__(self, settings: Settings, mode: str = "demo"):
        self.settings = settings
        self.mode = mode
        self._connected = False

    def _creds(self):
        if self.mode == "live":
            creds = self.settings.mt5_live
            if not creds.present:
                raise ExecutorUnavailable(
                    "mode LIVE nhưng thiếu MT5_LIVE_LOGIN/MT5_LIVE_PASSWORD/MT5_LIVE_SERVER"
                )
            return creds
        creds = self.settings.mt5
        if not creds.present:
            raise ExecutorUnavailable("thiếu MT5_LOGIN/MT5_PASSWORD/MT5_SERVER trong .env")
        return creds

    def _mt5(self):
        """Attach NHẸ trước (không re-login). Re-login mỗi lần khiến terminal coi là
        'account changed' và TỰ TẮT algo trading → IPC chết. Chỉ login khi account lệch."""
        try:
            import MetaTrader5 as mt5
        except ImportError as e:
            raise ExecutorUnavailable("package MetaTrader5 chưa cài") from e
        if not self._connected:
            creds = self._creds()
            attached = mt5.initialize()  # attach vào terminal đang chạy, KHÔNG đổi account
            info = mt5.account_info() if attached else None
            if info is not None and int(info.login) == int(creds.login):
                self._connected = True  # đúng account sẵn rồi — không re-login
                return mt5
            if attached:
                # terminal chạy nhưng sai account → đổi bằng login() (không respawn)
                if mt5.login(int(creds.login), password=creds.password, server=creds.server):
                    self._connected = True
                    return mt5
                got = getattr(info, "login", None)
                mt5.shutdown()
                raise ExecutorUnavailable(
                    f"terminal ở account {got}, login {creds.login} ({self.mode}) fail: "
                    f"{mt5.last_error()} — từ chối đặt lệnh"
                )
            # terminal chưa chạy → spawn + login đầy đủ (1 lần duy nhất)
            ok = mt5.initialize(
                login=int(creds.login), password=creds.password, server=creds.server
            )
            if not ok:
                raise ExecutorUnavailable(f"mt5.initialize fail: {mt5.last_error()}")
            info = mt5.account_info()
            if info is None or int(info.login) != int(creds.login):
                got = getattr(info, "login", None)
                mt5.shutdown()
                raise ExecutorUnavailable(
                    f"terminal đang ở account {got}, không phải {creds.login} ({self.mode}) — từ chối đặt lệnh"
                )
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

    LIMIT_MIN_ATR = 0.10  # entry lệch giá thị trường quá mức này (theo ATR) → treo limit chờ retest

    def place(
        self, sig: Signal, sym: SymbolConfig, lots: float,
        atr: float | None = None, limit_expiry_min: int = 30,
    ) -> ExecutionResult:
        """Đặt lệnh: market khi entry sát giá; PENDING LIMIT chờ retest khi não đặt
        entry xa giá hiện tại đúng phía (long: dưới giá, short: trên giá) — cách
        trader xử lý breakout muộn: không đuổi đỉnh, treo lệnh chờ giá quay lại."""
        mt5 = self._mt5()
        if not mt5.symbol_select(sym.mt5, True):
            return ExecutionResult(False, None, None, f"symbol {sym.mt5} không có trên server")
        tick = mt5.symbol_info_tick(sym.mt5)
        if tick is None:
            return ExecutionResult(False, None, None, f"không có tick cho {sym.mt5}")
        is_long = sig.direction == "long"
        market_price = float(tick.ask if is_long else tick.bid)

        # retest-limit: entry đúng phía limit và lệch đáng kể so với giá
        limit_side_ok = (sig.entry < market_price) if is_long else (sig.entry > market_price)
        far = atr and abs(sig.entry - market_price) > self.LIMIT_MIN_ATR * atr
        if limit_side_ok and far:
            return self._place_pending_limit(mt5, sig, sym, lots, limit_expiry_min)

        order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL
        price = market_price
        # margin guard: không ăn quá 80% free margin — thiếu margin để server reject là quá muộn
        try:
            need = mt5.order_calc_margin(order_type, sym.mt5, float(lots), price)
            acc = mt5.account_info()
            if need is not None and acc is not None and need > float(acc.margin_free) * 0.8:
                return ExecutionResult(
                    False, None, None,
                    f"margin cần {need:.2f} > 80% free margin ({float(acc.margin_free):.2f})",
                )
        except Exception as e:  # noqa: BLE001 — margin check lỗi thì để server quyết
            log.warning("order_calc_margin lỗi (%s) — tiếp tục, server sẽ kiểm", e)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym.mt5,
            "volume": float(lots),
            "type": order_type,
            "price": price,
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

    def _place_pending_limit(
        self, mt5, sig: Signal, sym: SymbolConfig, lots: float, expiry_min: int
    ) -> ExecutionResult:
        from datetime import UTC, datetime, timedelta

        is_long = sig.direction == "long"
        expiration = datetime.now(UTC) + timedelta(minutes=expiry_min)
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": sym.mt5,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY_LIMIT if is_long else mt5.ORDER_TYPE_SELL_LIMIT,
            "price": float(sig.entry),
            "sl": float(sig.sl),
            "tp": float(sig.tp),
            "magic": MAGIC,
            "comment": "ZunTrading-retest",
            "type_time": mt5.ORDER_TIME_SPECIFIED,
            "expiration": int(expiration.timestamp()),
            "type_filling": self._filling_mode(mt5, sym.mt5),
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(False, None, None, f"pending order_send None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(
                False, None, None,
                f"pending retcode {result.retcode}: {getattr(result, 'comment', '')}",
            )
        log.info(
            "pending LIMIT %s %s @ %s (chờ retest, hết hạn %d')",
            sig.direction, sym.mt5, sig.entry, expiry_min,
        )
        return ExecutionResult(
            True, str(result.order), None, f"limit chờ retest @ {sig.entry:g} (hết hạn {expiry_min}')"
        )

    # Quản lý lệnh đang chạy (chạy mỗi 5' qua task Sync):
    #   +1R → dời SL về entry (breakeven: từ đây hết thua)
    #   +2R → trailing SL giữ khoảng cách 1R sau giá (chỉ dời theo hướng lời, không bao giờ lùi)
    BREAKEVEN_AT_R = 1.0
    TRAIL_START_R = 2.0
    TRAIL_DIST_R = 1.0

    def manage_positions(self, journal: Journal) -> int:
        """Breakeven + trailing cho positions của bot. Trả số lệnh được dời SL."""
        mt5 = self._mt5()
        # SL GỐC lấy từ journal (position.sl trên server đã bị các lần dời trước thay đổi)
        orig_sl = {
            str(r["ticket"]): (float(r["entry"]), float(r["sl"]), r["direction"])
            for r in journal.open_orders_rows()
            if r["executor"] == self.name and r["ticket"]
        }
        moved = 0
        for p in mt5.positions_get() or ():
            if getattr(p, "magic", 0) != MAGIC:
                continue  # không đụng lệnh user vào tay
            rec = orig_sl.get(str(p.ticket))
            if rec is None:
                continue
            entry, sl0, direction = rec
            risk_dist = abs(entry - sl0)
            if risk_dist <= 0:
                continue
            sign = 1.0 if direction == "long" else -1.0
            profit_dist = (float(p.price_current) - entry) * sign
            r_multiple = profit_dist / risk_dist

            new_sl = None
            if r_multiple >= self.TRAIL_START_R:
                candidate = float(p.price_current) - sign * self.TRAIL_DIST_R * risk_dist
                if (candidate - float(p.sl)) * sign > 0:  # chỉ dời THEO hướng lời
                    new_sl = candidate
            elif r_multiple >= self.BREAKEVEN_AT_R and (entry - float(p.sl)) * sign > 0:
                new_sl = entry  # breakeven

            if new_sl is None:
                continue
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "symbol": p.symbol,
                "sl": round(new_sl, 5),
                "tp": float(p.tp),
            }
            result = mt5.order_send(req)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                moved += 1
                log.info(
                    "manage: %s #%s +%.1fR → SL %.5f (%s)",
                    p.symbol, p.ticket, r_multiple, new_sl,
                    "trailing" if r_multiple >= self.TRAIL_START_R else "breakeven",
                )
            else:
                log.warning("manage: dời SL %s #%s fail: %s", p.symbol, p.ticket,
                            getattr(result, "retcode", None))
        return moved

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
            if mt5.orders_get(ticket=ticket):
                continue  # pending limit còn treo chờ retest
            deals = mt5.history_deals_get(position=ticket)
            if not deals:
                # không position, không pending, không deal → pending hết hạn/bị hủy
                journal.mark_order_failed(row["id"])
                log.info("mt5 sync: pending #%s (%s) hết hạn không khớp — đóng sổ", row["id"], row["symbol"])
                continue
            pnl = sum(float(d.profit) + float(d.swap) + float(d.commission) for d in deals)
            exit_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            exit_price = float(exit_deals[-1].price) if exit_deals else 0.0
            journal.record_outcome(row["id"], exit_price, round(pnl, 2))
            closed += 1
            log.info("mt5 close #%s ticket=%s pnl=%.2f", row["id"], ticket, pnl)
        return closed
