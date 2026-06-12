"""Probe MT5 IPC từ process độc lập (Task Scheduler) — ghi kết quả ra file."""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "ipc_probe.txt"


def main() -> None:
    lines = []
    try:
        import MetaTrader5 as mt5

        ok = mt5.initialize()
        if not ok:
            lines.append(f"attach FAIL: {mt5.last_error()}")
        else:
            a = mt5.account_info()
            t = mt5.terminal_info()
            lines.append(
                f"CONNECTED: {a.login}@{a.server} equity={a.equity} {a.currency} "
                f"algo={t.trade_allowed}"
            )
            pos = mt5.positions_get() or []
            lines.append(f"positions={len(pos)}")
            mt5.shutdown()
    except Exception as e:  # noqa: BLE001
        lines.append(f"EXC: {e!r}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
