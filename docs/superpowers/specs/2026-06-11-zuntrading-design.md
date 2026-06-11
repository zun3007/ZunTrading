# ZunTrading — Design Spec

- **Date:** 2026-06-11
- **Status:** Approved hướng đi (signal bot + Bybit demo). Chờ user review spec.
- **Owner:** Zun

## 1. Mục tiêu

Bot phân tích thị trường chạy 24/7 trên máy Windows của user, một bộ não — hai đầu ra:

1. **Mitrade Signal Bot** — bắn signal qua Telegram (Long/Short, entry, SL, TP, size theo % vốn, lý do 1 dòng). User tự bấm lệnh trên Mitrade.
2. **Bybit Demo Auto-Trader** — cùng bộ não, tự đặt lệnh qua Bybit Demo Trading API (vốn ảo, endpoint `api-demo.bybit.com`). Vai trò: bằng chứng sống cho hiệu quả full-auto, zero rủi ro tiền thật.

### Non-goals (đã chốt, không mở lại)

- **KHÔNG automation UI Mitrade** dưới mọi hình thức (browser MCP, click bot) — vi phạm ToS Mitrade (platform không API/EA có chủ đích), rủi ro khóa account + misclick lệnh leverage.
- **KHÔNG tiền thật** ở mọi phase của project này. Chuyển demo→real là quyết định riêng của user, ngoài scope, chỉ nên cân nhắc sau ≥2–4 tuần demo có số liệu đạt chuẩn.
- Không hứa hẹn lợi nhuận. Đầu ra cam kết: kỷ luật risk + số liệu trung thực.

## 2. Markets & nhịp quét

| Market | Symbol data | Nguồn data (free) | Bybit demo tradeable? |
|---|---|---|---|
| Vàng | XAU/USD | yfinance (`GC=F`) | PAXGUSDT (verify khi build; nếu không có → signal-only) |
| Forex | EUR/USD, GBP/USD, USD/JPY | yfinance (`EURUSD=X`…) | Không → signal-only |
| Crypto | BTC/USDT, ETH/USDT | Binance public API | BTCUSDT, ETHUSDT perp ✓ |
| Indices | NAS100, US30 | yfinance (`^NDX`, `^DJI`) | Không → signal-only |

- 2 profile song song: **Day** (M15–H1, quét mỗi 15 phút) và **Swing** (H4–D1, quét mỗi 4 giờ). Signal ghi rõ profile.
- Forex/vàng/indices chỉ quét giờ thị trường mở; crypto 24/7.

## 3. Kiến trúc

```
[Windows Task Scheduler]
  └─ scanner (mỗi 15')
       ├─ data fetch: Binance public + yfinance
       ├─ indicators (code thuần): EMA 20/50/200, RSI, ATR, swing high/low
       ├─ pre-filter (code thuần): loại symbol không có setup thô → tiết kiệm token
       ├─ Claude brain (Claude Agent SDK headless, dùng auth Claude Code sẵn có):
       │    • Haiku — triage: "symbol này có setup đáng xem không?" (JSON)
       │    • Sonnet — chấm setup đạt ngưỡng → JSON {side, entry, sl, tp, confidence, reason}
       ├─ RISK GATE (code thuần — LLM KHÔNG thể override):
       │    • risk/lệnh ≤ 1% vốn │ tổng risk các lệnh mở ≤ 3%
       │    • RR tối thiểu 1:1.5 │ max 3 signal/ngày/market
       │    • lỗ ngày (demo) ≥ 3% → bot ngừng signal tới 0h hôm sau
       │    • confidence < ngưỡng config → bỏ
       ├─ outputs:
       │    • Telegram: signal format chuẩn, đủ thông tin bấm lệnh Mitrade trong 15s
       │    • Bybit Demo API v5: đặt lệnh + SL/TP (chỉ BTC/ETH/PAXG)
       └─ journal: SQLite — mọi signal, mọi lệnh demo, outcome tracking
  └─ reporter (daily 21:00 VN)
       └─ Telegram: win rate, P&L demo, max drawdown, signal nào ăn/thua, uptime
```

- **Config:** `config.yaml` — vốn tham chiếu, risk %, markets on/off, ngưỡng confidence, giờ quét.
- **Secrets:** `.env` (Telegram bot token, Bybit demo API keys) — gitignored, không bao giờ commit.
- **Ngôn ngữ:** Python 3.12+. Lý do: pandas/pandas-ta/ccxt/yfinance ecosystem.
- **Model routing:** Haiku cho scan tần suất cao (rẻ ~10×), Sonnet cho quyết định. Có thể nâng Opus cho daily review trong reporter.

## 4. Error handling (fail-closed)

- Data fetch fail → skip symbol cycle đó, log warning. Không bao giờ signal trên data thiếu.
- LLM timeout/fail → skip cycle. Thà lỡ cơ hội còn hơn signal ẩu.
- Bybit order fail → Telegram alert ngay.
- Exception lặp >3 lần liên tiếp → Telegram alert "bot cần kiểm tra".
- Mọi run ghi heartbeat — reporter tính uptime từ đây.

## 5. Testing

- **Unit (bắt buộc trước khi chạy thật):** risk gate 100% branch coverage; position size calc; signal JSON parser; market-hours logic.
- **Dry-run mode:** chạy full pipeline, in signal ra console, không gửi Telegram/không đặt lệnh.
- **Sanity backtest:** pre-filter chạy trên 3 tháng data lịch sử (code thuần, free); LLM chấm sample ~20 setup lịch sử để calibrate ngưỡng confidence.

## 6. Success criteria

- Uptime scanner ≥ 95% sau tuần đầu.
- 0 signal vi phạm risk gate (kiểm bằng unit test + audit journal).
- Sau 2–4 tuần: báo cáo trung thực win rate / profit factor / max drawdown từ journal + demo P&L. Số xấu cũng báo — đó là dữ liệu, không phải thất bại của project.

## 7. Phases

1. **Phase 1 (build ngay):** repo scaffold, data fetch, indicators, pre-filter, Claude brain, risk gate, Telegram signal, journal, dry-run + unit tests.
2. **Phase 2:** Bybit Demo executor + daily reporter + Task Scheduler setup.
3. **Phase 3 (ngoài scope build):** user tự quyết demo→real dựa trên số liệu; bot chỉ cần đổi endpoint+key nếu user quyết.

## 8. User cần chuẩn bị

1. Telegram: tạo bot qua @BotFather (2 phút) → đưa token + chat_id vào `.env`.
2. Bybit: tạo account (free) → bật Demo Trading → tạo Demo API key (cần ở Phase 2).
3. Máy treo 24/7 (đã có), Claude Code đã đăng nhập (đã có).
