"""Telegram notify — báo signal/lệnh/report. Notify lỗi KHÔNG được giết pipeline.

Mọi hàm trả bool (gửi được hay không), retry 3 lần exponential, nuốt exception + log.
"""

from __future__ import annotations

import html
import logging
import time

import requests

from .brain import Signal
from .config import Settings, SymbolConfig

log = logging.getLogger(__name__)

RETRIES = 3


def send(text: str, settings: Settings) -> bool:
    tg = settings.telegram
    if not tg.present:
        log.debug("telegram chưa cấu hình — bỏ qua notify")
        return False
    url = f"https://api.telegram.org/bot{tg.token}/sendMessage"
    payload = {"chat_id": tg.chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in range(RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            log.warning("telegram HTTP %s: %.200s", resp.status_code, resp.text)
        except requests.RequestException as e:
            log.warning("telegram lỗi mạng (lần %d): %s", attempt + 1, e)
        if attempt < RETRIES - 1:
            time.sleep(2**attempt)
    return False


def format_signal(
    sig: Signal, sym: SymbolConfig, lots: float, profile: str, executor: str,
    ticket: str | None,
) -> str:
    arrow = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
    rr = abs(sig.tp - sig.entry) / abs(sig.entry - sig.sl)
    lines = [
        f"<b>{arrow} {sym.mt5}</b> [{profile}] — {executor}" + (f" #{ticket}" if ticket else ""),
        f"Entry: <code>{sig.entry:g}</code>",
        f"SL: <code>{sig.sl:g}</code> | TP: <code>{sig.tp:g}</code> (RR {rr:.1f})",
        f"Size: <code>{lots:g} lot</code> | Conf: {sig.confidence:.0%}",
        f"Lý do: {html.escape(sig.reason)}",
    ]
    return "\n".join(lines)


def format_reject(symbol: str, reasons: list[str]) -> str:
    return f"⛔ <b>{symbol}</b> bị risk gate chặn:\n" + "\n".join(
        f"• {html.escape(r)}" for r in reasons
    )


def format_report(s: dict) -> str:
    wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "n/a"
    pnl = s["realized_pnl"]
    pnl_icon = "📈" if pnl >= 0 else "📉"
    return "\n".join(
        [
            f"<b>📊 ZunTrading — báo cáo {s['day_vn']}</b>",
            f"Signals: {s['signals_total']} (duyệt {s['signals_approved']})",
            f"Lệnh đóng: {s['trades_closed']} | Win: {s['wins']} | Loss: {s['losses']} | WR: {wr}",
            f"{pnl_icon} P&L realized: <code>{pnl:+.2f} USD</code>",
            f"Đang mở: {s['open_positions']} | Heartbeats: {s['heartbeats']} | Lỗi: {s['errors']}",
        ]
    )


def alert(text: str, settings: Settings) -> bool:
    return send(f"⚠️ <b>ZunTrading</b>: {html.escape(text)}", settings)
