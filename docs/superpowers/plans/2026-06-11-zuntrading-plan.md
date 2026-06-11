# ZunTrading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline execution chosen — single session, builder = plan author). Steps use checkbox (`- [ ]`) syntax for tracking.
> Note: per cost discipline in user CLAUDE.md, code bodies live in the repo commits, not duplicated here; this plan pins the **contracts** (interfaces, JSON schemas, rules, test cases, commands) that implementation must satisfy.

**Goal:** Bot trade full-auto trên Exness MT5 Demo + Telegram notify, chạy 24/7 trên Windows, fail-closed, risk gate bằng code thuần.

**Architecture:** Pipeline mỗi cycle: data → indicators → prefilter (code) → Claude triage (haiku) → Claude decision (sonnet) → risk gate (code, không override được) → executor (Paper|MT5) → journal (SQLite) → Telegram. Task Scheduler gọi cycle mỗi 15'. Reporter daily 21:00.

**Tech Stack:** Python 3.14 (verified trên máy: 3.14.5), pandas + numpy (indicators tự viết, KHÔNG pandas-ta — package bỏ hoang), requests (Binance public + Telegram), yfinance (XAU/FX/indices), MetaTrader5 (wheel 3.14 cần verify khi cài — plan B bên dưới), pytest + ruff, claude CLI 2.1.152 headless cho brain.

**Môi trường verified (2026-06-11):** `python 3.14.5` tại `C:\Python314`, `pip 26.1.1`, `claude 2.1.152` trên PATH.

---

## File map

| File | Trách nhiệm duy nhất |
|---|---|
| `config.yaml` | Markets, profiles, risk params, model names, ngưỡng confidence |
| `.env` (gitignored) / `.env.example` | TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MT5_LOGIN/PASSWORD/SERVER |
| `src/zuntrading/config.py` | Load + validate yaml/env → dataclass `Settings`. Fail loud nếu thiếu. |
| `src/zuntrading/data.py` | `get_candles(symbol_cfg, timeframe, n) -> pd.DataFrame[time,open,high,low,close,volume]`. Chain: MT5 → fallback free. `market_open(symbol_cfg, now) -> bool`. |
| `src/zuntrading/indicators.py` | `enrich(df) -> df` thêm ema20/50/200, rsi14, atr14, adx14, swing_high/low. Pure pandas/numpy. |
| `src/zuntrading/prefilter.py` | `find_candidates(df_by_tf, symbol) -> list[Candidate]` — code-only: trend alignment, pullback zone, range edge. Không LLM. |
| `src/zuntrading/brain.py` | `triage(candidate) -> bool` (haiku), `decide(candidate) -> Signal\|None` (sonnet). Gọi `claude -p` headless, JSON strict, fail-closed. |
| `src/zuntrading/risk.py` | `RiskGate.evaluate(signal, account, open_positions, today_stats) -> Verdict` + `position_size(...)`. PURE code. |
| `src/zuntrading/journal.py` | SQLite: bảng `signals`, `orders`, `outcomes`, `heartbeats`. API ghi/đọc + thống kê. |
| `src/zuntrading/calibration.py` | `threshold_for(market) -> float` từ win-rate theo confidence bucket trong journal (min 20 mẫu, mặc định config). |
| `src/zuntrading/executor.py` | `ExecutorBase` / `PaperExecutor` / `MT5Executor`. MT5: `order_send` kèm SL/TP server-side. |
| `src/zuntrading/notify.py` | `send(text)` Telegram Bot API. Nuốt lỗi mạng sau 3 retry → log (notify không được giết pipeline). |
| `src/zuntrading/scanner.py` | `run_cycle(profile)` orchestrate toàn pipeline, mọi exception → log + heartbeat status. |
| `src/zuntrading/reporter.py` | Daily report từ journal → Telegram. |
| `scripts/dry_run.ps1`, `run_scan.ps1`, `verify.ps1`, `install_task.ps1` | Vận hành. `install_task.ps1` đăng ký 2 scheduled tasks. |
| `tests/test_*.py` | Per-module, risk gate 100% branch. |

## Contracts (pin cứng)

**Candidate (prefilter → brain):**
```json
{"symbol":"XAUUSD","market":"gold","profile":"day","setup_type":"pullback_trend|range_edge|breakout",
 "direction":"long|short","tf_entry":"M15","price":2632.5,"atr":8.2,
 "context":{"ema_stack":"bullish","rsi":54.2,"adx":28.1,"swing_high":2641.0,"swing_low":2615.0}}
```

**Signal (brain → risk; brain JSON output bắt buộc đúng schema, sai = discard):**
```json
{"action":"trade|skip","direction":"long|short","entry":2632.5,"sl":2624.0,"tp":2649.0,
 "confidence":0.0,"reason":"<=200 chars"}
```

**Verdict (risk → executor):** `approved: bool`, `lots: float`, `reject_reasons: list[str]`.

**Risk rules (bất biến, test 100% branch):**
1. risk/lệnh = |entry−sl| × lots × value_per_point ≤ 1% equity
2. Tổng risk các vị thế đang mở + lệnh mới ≤ 3% equity
3. RR = |tp−entry|/|entry−sl| ≥ 1.5
4. ≤ 3 lệnh/ngày/market; ≤ 1 vị thế mở/symbol
5. Lỗ realized hôm nay ≥ 3% equity → reject tất cả tới 0h (giờ VN)
6. confidence < calibration.threshold_for(market) → reject
7. SL/TP bắt buộc tồn tại, đúng phía (long: sl<entry<tp); sai → reject
8. Mọi reject ghi journal kèm lý do.

**brain.py gọi CLI (đường chính, dùng auth Claude Code sẵn):**
```
claude -p "<prompt>" --model haiku|sonnet --output-format json --max-turns 1
```
- Parse `result` field → JSON trong đó. Bất kỳ lỗi parse/timeout (60s) → trả None (fail-closed), log.
- Fallback `anthropic` SDK nếu `ANTHROPIC_API_KEY` set (cùng prompt, cùng schema).

**MetaTrader5 wheel plan B (nếu pip install fail trên 3.14):** giữ MT5Executor code + guard import; README hướng dẫn cài Python 3.12 song song chỉ cho executor venv, HOẶC chờ wheel. PaperExecutor + toàn pipeline vẫn chạy 3.14. Quyết định khi có kết quả pip thật.

## Tasks

> **STATUS 2026-06-11: TẤT CẢ TASK HOÀN THÀNH.** 138 unit tests pass, ruff clean, risk gate 100% branch coverage, live haiku smoke pass, dry-run thật 8 symbols pass (errors=0). Deviation so với plan: thuật toán calibration đổi từ bucket-ladder sang evidence-only (siết khi thua / nới 1 nấc khi biên thấp nhất thắng vượt margin) — bucket-ladder cho phép nới vào vùng không có dữ liệu, không chấp nhận được; xem docstring `calibration.py`.

### Task 1: Plan doc ✅ (file này)

### Task 2: Scaffold + config
- [ ] `requirements.txt`: pandas, numpy, requests, yfinance, python-dotenv, PyYAML, pytest, ruff (pin major). MetaTrader5 ở `requirements-mt5.txt` riêng (wheel rủi ro 3.14).
- [ ] `pip install -r requirements.txt` — quote output, verify import OK trên 3.14
- [ ] Thử `pip install MetaTrader5` → ghi kết quả vào plan/README (quyết plan B)
- [ ] `config.yaml` đầy đủ markets/profiles/risk/models; `config.py` + `tests/test_config.py` (load OK, thiếu env fail loud, override risk từ yaml)
- [ ] `pytest tests/test_config.py -v` PASS → commit `feat: scaffold + config layer`

### Task 3: Data layer
- [ ] `data.py`: BinancePublicSource (requests, `/api/v3/klines`), YFinanceSource, MT5Source (guard import), chain theo `symbol_cfg.sources`; chuẩn hóa DataFrame; `market_open()` (forex/gold/indices: T2–T6 + giờ; crypto: luôn True)
- [ ] `tests/test_data.py`: normalize từ fixture JSON Binance, market_open các case biên (thứ 7, chủ nhật, giao phiên), chain fallback khi source raise
- [ ] pytest PASS → commit `feat: data layer with source fallback chain`

### Task 4: Indicators + prefilter
- [ ] `indicators.py`: ema(20/50/200), rsi14 (Wilder), atr14, adx14, swing high/low (fractal 2-2). Test đối chiếu giá trị tính tay trên fixture 50 nến.
- [ ] `prefilter.py`: 3 setup detectors thuần code (pullback_trend: ema stack + giá chạm zone ema20-50 + rsi 40–60; range_edge: adx<20 + giá sát swing ±0.5×ATR; breakout: đóng nến vượt swing + adx tăng). Mỗi detector có test positive + negative fixture.
- [ ] pytest PASS → commit `feat: indicators + code-only prefilter`

### Task 5: Brain
- [ ] `brain.py`: build prompt (system: vai trò trader kỷ luật, output JSON schema, KHÔNG markdown), subprocess gọi claude CLI, timeout 60s, parse strict (`json.loads` 2 lớp), validate schema bằng code (không lib mới)
- [ ] `tests/test_brain.py`: parse các output mẫu (đúng, thiếu field, markdown lẫn, action=skip), mock subprocess; KHÔNG gọi CLI thật trong unit test
- [ ] 1 smoke test thật đánh dấu `@pytest.mark.live` (skip mặc định): gọi haiku thật với candidate giả → JSON hợp lệ
- [ ] pytest PASS → commit `feat: claude brain, fail-closed JSON contract`

### Task 6: Risk gate
- [ ] `risk.py` đúng 8 rule + `position_size` (lots làm tròn xuống step, min/max lot theo symbol_cfg)
- [ ] `tests/test_risk.py`: mỗi rule ≥1 test pass + ≥1 test reject, combo (rule 5 chặn dù mọi thứ khác pass), sizing đúng số học (3 case tính tay), branch coverage 100% (`pytest --cov=src/zuntrading/risk --cov-branch`)
- [ ] PASS + coverage 100% → commit `feat: hard risk gate, 100% branch coverage`

### Task 7: Journal + calibration
- [ ] `journal.py`: schema 4 bảng, ghi signal/order/outcome/heartbeat, query thống kê ngày + per-market
- [ ] `calibration.py`: bucket confidence 0.5–1.0 step 0.1, threshold = bucket thấp nhất có win-rate ≥ config.target_winrate với ≥20 mẫu; chưa đủ mẫu → config.default_confidence
- [ ] tests: tmp_path sqlite, calibration với journal giả 40 outcomes
- [ ] PASS → commit `feat: sqlite journal + confidence calibration`

### Task 8: Executors + notify
- [ ] `executor.py`: PaperExecutor (fill giả lập ngay tại entry, ghi journal), MT5Executor (initialize/login từ env, resolve symbol, `order_send` ORDER_TYPE_BUY/SELL kèm sl/tp, đọc kết quả retcode — guard import MetaTrader5)
- [ ] `notify.py`: Telegram `sendMessage` qua requests, 3 retry exponential, lỗi → log không raise; format signal đẹp (emoji chiều, entry/SL/TP/size/reason)
- [ ] tests: PaperExecutor fill + journal; MT5Executor mock module (sys.modules injection) verify request dict đúng (sl/tp đính kèm, lots từ verdict); notify mock requests + test retry
- [ ] PASS → commit `feat: paper + mt5 executors, telegram notify`

### Task 9: Scanner + reporter
- [ ] `scanner.py`: `run_cycle(profile)` per market-open symbol: data→enrich→prefilter→(có candidate)→triage→decide→risk→execute→journal→notify; try/except per symbol (1 symbol lỗi không giết cycle); heartbeat cuối cycle; `--dry-run` flag → PaperExecutor + console
- [ ] `reporter.py`: stats hôm nay (signals, trades, win/loss, P&L paper/demo, uptime từ heartbeats) → Telegram
- [ ] `tests/test_scanner.py`: pipeline với mọi tầng mock — happy path, prefilter rỗng (không gọi brain — đếm call), brain None, risk reject
- [ ] **Dry-run THẬT:** `python -m zuntrading.scanner --profile day --dry-run` với data live free sources — quote output
- [ ] PASS → commit `feat: scanner orchestration + daily reporter`

### Task 10: Scripts + README VN
- [ ] 4 file `scripts/*.ps1`; `install_task.ps1` đăng ký "ZunTrading-Scan" (15') + "ZunTrading-Report" (21:00) qua `Register-ScheduledTask`, có `-Unregister`
- [ ] `README.md` tiếng Việt: tạo Exness demo từng bước, cài MT5 + login, BotFather, điền `.env`, dry-run, cài task, đọc report, FAQ (bot im lặng = đúng hành vi khi không có setup), cảnh báo demo-trước-tiền-thật
- [ ] commit `feat: ops scripts + vietnamese user guide`

### Task 11: Verify + hand-off
- [ ] `ruff check .` sạch; `pytest -q` toàn bộ PASS — quote
- [ ] Dry-run full 8 symbols 1 cycle — quote signal/no-signal output
- [ ] Cập nhật checkbox plan này, commit cuối, tổng kết + hướng dẫn 5 bước cho user

## Self-review (đã chạy)
- Spec coverage: §2 markets→T3/T4, §3 pipeline→T5/T6/T8/T9, §4 fail-closed→T5/T9, §5 testing→mỗi task, §6 criteria→T7 heartbeat+T9 reporter, §7 phases→T2–T10, §8 user prep→T10. Gap: không.
- Placeholder scan: không TBD; "plan B MT5 wheel" là quyết định có điều kiện với cả 2 nhánh ghi rõ.
- Type consistency: Candidate/Signal/Verdict dùng thống nhất T4→T5→T6→T8→T9.
