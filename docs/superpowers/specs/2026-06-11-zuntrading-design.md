# ZunTrading — Design Spec

- **Date:** 2026-06-11 (rev 3)
- **Rev 3:** Telegram → tùy chọn; thêm **local dashboard** (FastAPI + vanilla, http://127.0.0.1:8420) làm kênh theo dõi/điều khiển chính: equity curve, positions, signals (kèm lý do reject), pause/scan, và **mode DEMO⇄LIVE** với guardrails: mặc định demo vĩnh viễn (file state hỏng → demo), creds live tách riêng (`MT5_LIVE_*`), confirm phrase "TRADE LIVE" + hiển thị readiness (số liệu demo + cảnh báo), executor đối chiếu account login terminal trước mọi lệnh, mode live KHÔNG fallback paper lặng lẽ. Non-goal "không tiền thật" được thay bằng: **demo-first, chuyển live là hành động có ý thức của user qua friction có chủ đích** — bot không bao giờ tự chuyển.
- **Rev 2:** pivot executor sang Exness MT5, user approved.
- **Status:** Implemented.
- **Owner:** Zun

## 1. Mục tiêu

Bot phân tích thị trường chạy 24/7 trên máy Windows của user, một bộ não — hai đầu ra:

1. **Exness MT5 Demo Auto-Trader** — bot tự đặt lệnh full-auto qua MetaTrader 5 (Python package `MetaTrader5`) trên **tài khoản demo Exness** (vốn ảo). Automation là tính năng chính thức của MT5 — không vi phạm ToS. Trade đúng markets user muốn: XAUUSD, forex majors, BTCUSD/ETHUSD, indices.
2. **Telegram notify/signal** — mọi lệnh bot vào/ra + signal đầy đủ (Long/Short, entry, SL, TP, size, lý do) bắn về điện thoại user. User muốn mirror tay trên Mitrade thì dùng như signal; không thì dùng để giám sát bot.

Bybit Demo (REST API) hạ xuống **optional Phase 3** — chỉ khi user muốn thêm nhánh crypto 24/7 native.

### Non-goals (đã chốt, không mở lại)

- **KHÔNG automation UI Mitrade** dưới mọi hình thức (browser MCP, click bot) — vi phạm ToS Mitrade (platform không API/EA có chủ đích), rủi ro khóa account + misclick lệnh leverage.
- **KHÔNG tiền thật** ở mọi phase của project này. Chuyển demo→real là quyết định riêng của user, ngoài scope, chỉ nên cân nhắc sau ≥2–4 tuần demo có số liệu đạt chuẩn.
- Không hứa hẹn lợi nhuận. Đầu ra cam kết: kỷ luật risk + số liệu trung thực.

## 2. Markets & nhịp quét

| Market | Symbol MT5 (Exness) | Nguồn data phân tích (free) | Bot trade demo? |
|---|---|---|---|
| Vàng | XAUUSD | MT5 candles (chính) / yfinance `GC=F` (fallback) | ✓ |
| Forex | EURUSD, GBPUSD, USDJPY | MT5 candles / yfinance fallback | ✓ |
| Crypto | BTCUSD, ETHUSD | MT5 candles / Binance public fallback | ✓ |
| Indices | USTEC (NAS100), US30 | MT5 candles / yfinance fallback | ✓ |

Tên symbol Exness xác minh runtime bằng `mt5.symbols_get()` — config có bảng map, bot tự resolve + báo lỗi rõ nếu symbol không tồn tại. Khi MT5 chưa kết nối (chưa cài/chưa login), data layer tự fallback nguồn free để **dry-run vẫn chạy được 100%**.

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
       │    • Executor interface: PaperExecutor (dry-run, mặc định) │ MT5Executor (Exness demo)
       │      MT5Executor: order_send với SL/TP đính kèm SERVER-SIDE → bot crash thì SL vẫn sống
       │    • Telegram: mọi lệnh + signal format chuẩn (mirror tay Mitrade được nếu muốn)
       └─ journal: SQLite — mọi signal, mọi lệnh demo, outcome tracking
       └─ calibration: đối chiếu confidence Claude nói vs kết quả thật theo bucket,
          tự nâng/hạ ngưỡng confidence per-market theo evidence (không tin lời LLM suông)
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

1. **Phase 1 (build ngay):** repo scaffold, data fetch (free sources), indicators, pre-filter, Claude brain, risk gate, journal, PaperExecutor, Telegram notify, scanner, reporter, unit tests, dry-run chạy được ngay không cần MT5.
2. **Phase 2 (build ngay luôn, kích hoạt khi user cài xong):** MT5Executor (Exness demo) + script cài Windows Task Scheduler + hướng dẫn user từng bước (README tiếng Việt).
3. **Phase 3 (optional, sau):** Bybit Demo nhánh crypto 24/7; user tự quyết demo→real (đổi account login, ngoài scope).

## 8. User cần chuẩn bị (chi tiết trong README)

1. Telegram: tạo bot qua @BotFather (2 phút) → token + chat_id vào `.env`.
2. Exness: tạo account miễn phí → tạo **tài khoản DEMO MT5** → cài MT5 terminal trên máy → login demo.
3. Máy treo 24/7 (đã có), Claude Code đã đăng nhập (đã có), Python 3.12+ (README có lệnh cài).
