# ZunTrading 🤖

Bot trade tự động trên **Exness MT5** — não Claude, risk gate bằng code cứng, **dashboard local** theo dõi và điều khiển. Chạy 24/7 trên máy Windows. Demo mặc định; chuyển Live có bước xác nhận riêng.

```
Data (MT5/Binance/yfinance) → Indicators → Pre-filter (code)
  → Claude triage (haiku) → Claude decision (sonnet)
  → RISK GATE (code thuần, LLM không override được)
  → Lệnh MT5 (SL/TP nằm trên server) → Journal SQLite → Dashboard (+ Telegram tùy chọn)
```

## Dashboard

```powershell
.\scripts\run_ui.ps1     # mở http://127.0.0.1:8420
```

- **Equity curve**, vị thế mở, lịch sử lệnh (kèm thắng/thua), mọi signal kể cả bị risk gate chặn (với lý do).
- Nút **Scan ngay** / **Tạm dừng** (pause = bot bỏ cycle nhưng lệnh mở vẫn có SL/TP trên server).
- **DEMO ⇄ LIVE**: click badge. Chuyển LIVE bắt buộc: tài khoản live riêng trong `.env`, nhìn số liệu demo của chính bot (lệnh đóng, win rate, P&L, cảnh báo đỏ nếu mẫu nhỏ/đang lỗ), và gõ đúng `TRADE LIVE`. Khi LIVE: viền đỏ trên cùng + badge đỏ — không thể nhìn nhầm. Live KHÔNG BAO GIỜ fallback lặng lẽ về paper; sai account là bot từ chối đặt lệnh.

**Đọc 3 dòng này trước:**
- Bot chạy **tài khoản DEMO** (tiền ảo). Số liệu sau 2–4 tuần mới là căn cứ để cân nhắc bất cứ gì xa hơn — quyết định đó là của bạn, không phải của bot.
- 77% tài khoản retail mất tiền khi trade CFD (số Mitrade tự công bố). Bot không phải máy in tiền; bot là **kỷ luật + đo lường**.
- Bot **fail-closed**: thiếu data, LLM lỗi, không chắc chắn → đứng im. Bot im lặng cả ngày là *hành vi đúng* khi không có setup — không phải bug.

---

## Cài đặt lần đầu (≈ 20 phút)

### Bước 0 — Yêu cầu có sẵn
- Windows + Python 3.12+ (`python --version`)
- **Claude CLI đã đăng nhập**: mở terminal, gõ `claude` → gõ `/login` làm theo hướng dẫn (1 lần duy nhất). Kiểm tra nhanh não bot sống chưa:
  ```powershell
  python -m pytest -m live -q   # 2 passed = data + não đều OK
  ```
- Đã chạy: `pip install -r requirements.txt` và `pip install -e .`

### Bước 1 — Tạo Telegram bot (2 phút)
1. Mở Telegram, chat với **@BotFather** → gõ `/newbot` → đặt tên → nhận **token** (dạng `123456:ABC-...`).
2. Chat 1 tin bất kỳ với bot vừa tạo (để mở chat).
3. Mở trình duyệt: `https://api.telegram.org/bot<TOKEN>/getUpdates` → tìm `"chat":{"id":XXXX}` → đó là **chat_id**.

### Bước 2 — Tạo tài khoản Exness DEMO + cài MT5 (10 phút)
1. Vào **exness.com** → Đăng ký (email + mật khẩu, demo không cần KYC).
2. Trong Personal Area → **My Accounts** → **Open New Account** → chọn **Demo** → loại **MT5** → ghi lại: **số login**, **mật khẩu trading**, **server** (dạng `Exness-MT5Trial...`).
3. Tải **MetaTrader 5** từ trang Exness → cài → mở MT5 → **File → Login to Trade Account** → nhập login/mật khẩu/server demo.
4. Trong MT5: **Tools → Options → Expert Advisors** → tick **Allow algorithmic trading**.
5. Để MT5 **mở** (terminal phải chạy thì bot mới đặt lệnh được).

### Bước 3 — Điền `.env`
```powershell
Copy-Item .env.example .env
notepad .env
```
Điền: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`.

### Bước 4 — Chạy thử (không lệnh thật, không Telegram)
```powershell
.\scripts\dry_run.ps1
```
Thấy dòng `cycle day xong: scanned=... errors=0` là pipeline sống. Chạy `.\scripts\verify.ps1` nếu muốn full test suite.

### Bước 5 — Bật bot 24/7
```powershell
.\scripts\install_task.ps1
```
Xong. Bot tự quét mỗi 15 phút (day) + mỗi 4h (swing), báo cáo 21:00 hằng ngày qua Telegram. Gỡ: `.\scripts\install_task.ps1 -Unregister`.

---

## Vận hành hằng ngày

| Việc | Lệnh / nơi xem |
|---|---|
| Xem bot sống không | Telegram báo cáo 21:00, hoặc `logs\zuntrading.log` |
| Chạy tay 1 cycle | `.\scripts\run_scan.ps1` (hoặc `-TradeProfile swing`) |
| Báo cáo ngay | `python -m zuntrading.reporter` |
| Đổi risk/markets | sửa `config.yaml` (risk %, bật/tắt market) |
| Lịch sử đầy đủ | `data\zuntrading.db` (SQLite — mọi signal/lệnh/kết quả) |

## Risk gate — luật không thương lượng (code, không phải prompt)

3 profile, chuyển ngay trên dashboard (panel Risk gate). Mạo hiểm = **lỗ nhanh hơn khi sai**, không phải "lời nhiều hơn":

| Rule | 🛡 An toàn | ⚖ Cân bằng (mặc định) | 🔥 Mạo hiểm |
|---|---|---|---|
| Risk mỗi lệnh | 0.5% | 1% | 2% |
| Tổng risk các lệnh mở | 2% | 3% | 6% |
| Reward:Risk tối thiểu | 2.0 | 1.5 | 1.3 |
| Lệnh/ngày/market | 2 | 3 | 5 |
| Ngừng khi lỗ ngày chạm | 2% | 3% | 5% |
| Ngưỡng confidence khởi điểm | 0.75 | 0.65 | 0.60 |

Mọi profile: SL/TP server-side, 1 vị thế/symbol, confidence tự siết khi thua. Không tồn tại profile "all-in".

## Vốn tối thiểu (tính từ chính công thức sizing của bot)

Lot nhỏ nhất Exness = 0.01. Bot chỉ vào lệnh khi risk ≤ X% equity — equity nhỏ quá thì 0.01 lot đã vượt budget → bot **từ chối lệnh** (R1), không phải bot hỏng:

| Equity | Cân bằng (1%/lệnh = budget) | Kết quả |
|---|---|---|
| $100 | $1/lệnh | Hầu hết signal bị R1 chặn (0.01 lot vàng SL hợp lý đã risk $3–8). Bot gần như đứng im. |
| $500 | $5/lệnh | Trade được 0.01 lot với SL chuẩn trên đa số markets — **mức sàn kỹ thuật** |
| $1.000+ | $10/lệnh | Thoải mái 0.01–0.03 lot, đủ chỗ cho cả 4 markets — **khuyến nghị** |

Vốn dưới $500 mà vẫn muốn chạy: cân nhắc tài khoản **Exness Standard Cent** (contract nhỏ hơn 100×) — cần đổi symbol map trong `config.yaml`, hỏi bot khi cần.

SL/TP gắn **trên server Exness** ngay lúc đặt lệnh — bot crash, mất mạng, tắt máy thì lệnh vẫn có SL/TP bảo vệ.

## FAQ

**Bot cả ngày không ra lệnh nào?** Đúng thiết kế — pre-filter + triage + risk gate loại phần lớn nhiễu. Kiểm tra heartbeat trong báo cáo 21:00: `Heartbeats > 0` nghĩa là bot vẫn quét đều.

**`MT5 không sẵn sàng → dùng paper`?** MT5 terminal chưa mở hoặc `.env` sai login/server. Bot tự hạ về chế độ paper (mô phỏng) thay vì chết — mở MT5 lên là cycle sau tự dùng MT5.

**Muốn bot bớt/thêm liều?** `config.yaml` → `risk:`. Giảm `max_risk_per_trade_pct` xuống 0.5 là cách lành mạnh nhất để bớt đau tim.

**Demo lời rồi, lên tiền thật được chưa?** Tối thiểu 2–4 tuần + xem `win_rate`, `realized_pnl`, max drawdown trong journal. Và đó là quyết định của bạn — repo này cố tình không có hướng dẫn "one-click lên real".

**Token Claude tốn không?** Pre-filter chặn trước nên đa số cycle không gọi LLM nào; khi có setup mới gọi haiku (~rẻ), đạt ngưỡng mới gọi sonnet. Dùng login Claude Code subscription sẵn có.

## Cấu trúc repo

```
src/zuntrading/   config, data, indicators, prefilter, brain, risk, journal,
                  calibration, executor, notify, scanner, reporter
tests/            117+ unit tests (risk gate 100% branch coverage)
scripts/          dry_run / run_scan / verify / install_task (.ps1)
docs/superpowers/ spec + implementation plan
config.yaml       markets, risk, models, profiles
.env              secrets (KHÔNG commit)
```
