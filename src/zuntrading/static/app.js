/* ZunTrading dashboard — poll trạng thái, vẽ equity, điều khiển bot. Không framework, không build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const fmtUsd = (v) =>
  (v >= 0 ? "" : "−") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtNum = (v, d = 2) => Number(v).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tsLocal = (iso) => (iso ? new Date(iso).toLocaleString("vi-VN", { hour12: false }) : "—");

const state = { status: null, chart: null, series: null, lastEquity: null, activeTab: "positions", scanPolling: null };

/* ---------- API ---------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status, body: await res.text() });
  return res.json();
}

/* ---------- Equity chart (lightweight-charts v5) ---------- */
function initChart() {
  const el = $("#chart");
  state.chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#5f6a7e", fontFamily: "ui-monospace, Consolas, monospace", fontSize: 11 },
    grid: { vertLines: { color: "#1a1f29" }, horzLines: { color: "#1a1f29" } },
    rightPriceScale: { borderColor: "#222936" },
    timeScale: { borderColor: "#222936", timeVisible: true, secondsVisible: false },
    crosshair: { horzLine: { color: "#e8a33d", labelBackgroundColor: "#e8a33d" }, vertLine: { color: "#2f3848", labelBackgroundColor: "#171b23" } },
  });
  state.series = state.chart.addSeries(LightweightCharts.AreaSeries, {
    lineColor: "#e8a33d", lineWidth: 2,
    topColor: "rgba(232,163,61,0.22)", bottomColor: "rgba(232,163,61,0.01)",
    priceLineVisible: false,
  });
}

async function loadCurve() {
  const data = await api("/api/equity-curve?executor=paper");
  const empty = $("#chartEmpty"), chartEl = $("#chart");
  if (!data.points.length) {
    empty.hidden = false; chartEl.style.display = "none";
    $("#curveHint").textContent = `bắt đầu từ ${fmtUsd(data.start)}`;
    return;
  }
  empty.hidden = true; chartEl.style.display = "";
  let prev = 0;
  const pts = data.points.map((p) => {
    let t = Math.floor(Date.parse(p.time) / 1000);
    if (t <= prev) t = prev + 1; // time phải tăng nghiêm ngặt
    prev = t;
    return { time: t, value: p.value };
  });
  state.series.setData(pts);
  state.chart.timeScale().fitContent();
  const last = pts[pts.length - 1].value;
  const delta = last - data.start;
  $("#curveHint").innerHTML = `${pts.length} lệnh đóng · <span class="${delta >= 0 ? "pnl-pos" : "pnl-neg"} num">${fmtUsd(delta)}</span> so với vốn gốc`;
}

/* ---------- Status ---------- */
function setEquity(el, value) {
  const prevVal = state.lastEquity;
  el.textContent = fmtUsd(value);
  if (prevVal !== null && value !== prevVal) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth; // restart animation
    el.classList.add(value > prevVal ? "flash-up" : "flash-down");
  }
  state.lastEquity = value;
}

function renderStatus(s) {
  document.body.dataset.mode = s.mode;
  const badge = $("#modeBadge");
  badge.className = "badge " + (s.paused ? "badge-paused" : s.mode === "live" ? "badge-live" : "badge-demo");
  badge.textContent = s.paused ? "PAUSED" : s.mode.toUpperCase();

  setEquity($("#equity"), s.paper_equity);
  const pnl = s.today.realized_pnl;
  const pnlEl = $("#todayPnl");
  pnlEl.textContent = fmtUsd(pnl);
  pnlEl.className = "num num-lg " + (pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "");
  $("#openCount").textContent = s.open_positions.length;

  $("#pauseBanner").hidden = !s.paused;
  $("#pauseBtn").textContent = s.paused ? "Chạy lại" : "Tạm dừng";
  const scanBtn = $("#scanBtn");
  scanBtn.disabled = s.scan_running;
  scanBtn.textContent = s.scan_running ? "Đang quét…" : "Scan ngay";

  // Hôm nay
  const sum = s.summary;
  $("#dayLabel").textContent = sum.day_vn;
  $("#todayStats").innerHTML = [
    ["Signals", sum.signals_total], ["Được duyệt", sum.signals_approved],
    ["Lệnh đóng", sum.trades_closed], ["Win rate", sum.win_rate === null ? "—" : Math.round(sum.win_rate * 100) + "%"],
    ["P&L", fmtUsd(sum.realized_pnl)], ["Heartbeats", sum.heartbeats],
  ].map(([l, v]) => `<div class="stat"><span class="label">${l}</span><span class="num">${v}</span></div>`).join("");

  // Risk gate + profile switch
  const r = s.risk;
  $("#riskList").innerHTML = [
    ["Risk mỗi lệnh", `≤ ${r.max_risk_per_trade_pct}%`],
    ["Tổng risk mở", `≤ ${r.max_total_open_risk_pct}%`],
    ["RR tối thiểu", r.min_rr], ["Lệnh/ngày/market", `≤ ${r.max_trades_per_day_per_market}`],
    ["Dừng khi lỗ ngày", `${r.daily_loss_stop_pct}%`],
  ].map(([l, v]) => `<li><span>${l}</span><span class="num">${v}</span></li>`).join("");

  const PROFILE_LABELS = { an_toan: "An toàn", can_bang: "Cân bằng", mao_hiem: "Mạo hiểm" };
  $("#riskProfileSeg").innerHTML = (s.risk_profiles || []).map((p) =>
    `<button data-profile="${esc(p)}" class="${p === s.risk_profile ? "active" : ""} ${p === "mao_hiem" ? "seg-danger" : ""}"
      role="radio" aria-checked="${p === s.risk_profile}">${PROFILE_LABELS[p] || esc(p)}</button>`).join("");
  document.querySelectorAll("#riskProfileSeg button").forEach((b) =>
    b.addEventListener("click", async () => {
      const p = b.dataset.profile;
      if (p === s.risk_profile) return;
      if (p === "mao_hiem" && !confirm("Mạo hiểm = risk 2%/lệnh, dừng ngày ở 5%. Lỗ NHANH HƠN khi sai. Chắc chưa?")) return;
      await api("/api/risk-profile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile: p }) });
      refresh();
    })
  );

  // Hệ thống
  const hb = s.last_heartbeat;
  $("#sysInfo").innerHTML = `
    <div class="row"><span>Heartbeat cuối</span><span class="num">${hb ? tsLocal(hb.ts_utc) : "chưa có"}</span></div>
    <div class="row"><span>MT5 demo</span><span class="${s.mt5_demo_configured ? "tag-ok" : "tag-no"}">${s.mt5_demo_configured ? "đã cấu hình" : "chưa cấu hình"}</span></div>
    <div class="row"><span>MT5 live</span><span class="${s.mt5_live_configured ? "tag-ok" : "tag-no"}">${s.mt5_live_configured ? "đã cấu hình" : "chưa cấu hình"}</span></div>`;

  renderPositions(s.open_positions);
}

/* ---------- Tables ---------- */
const tableWrap = (head, rows, emptyMsg) =>
  rows.length
    ? `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : `<div class="empty">${emptyMsg}</div>`;

const dirBadge = (d) => `<span class="dir ${d === "long" ? "dir-long" : "dir-short"}">${d === "long" ? "▲ LONG" : "▼ SHORT"}</span>`;
const pnlCell = (v) => (v === null || v === undefined) ? `<span class="muted">đang mở</span>` : `<span class="num ${v >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(v)}</span>`;

function renderPositions(rows) {
  $("#tab-positions").innerHTML = tableWrap(
    `<th>Symbol</th><th>Hướng</th><th class="num">Lots</th><th class="num">Entry</th><th class="num">SL</th><th class="num">TP</th><th class="num">Risk $</th><th>Mở lúc</th>`,
    rows.map((p) => `<tr><td><b>${esc(p.symbol)}</b></td><td>${dirBadge(p.direction)}</td>
      <td class="num">${fmtNum(p.lots)}</td><td class="num">${fmtNum(p.entry, 5)}</td>
      <td class="num pnl-neg">${fmtNum(p.sl, 5)}</td><td class="num pnl-pos">${fmtNum(p.tp, 5)}</td>
      <td class="num">${fmtNum(p.risk_amount)}</td><td class="muted">${tsLocal(p.ts)}</td></tr>`),
    "Không có vị thế mở. Bot sẽ vào lệnh khi có setup vượt qua não + risk gate."
  );
}

async function loadOrders() {
  const rows = await api("/api/orders?limit=100");
  $("#tab-orders").innerHTML = tableWrap(
    `<th>#</th><th>Symbol</th><th>Hướng</th><th class="num">Lots</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&L</th><th>Executor</th><th>Lúc</th>`,
    rows.map((o) => `<tr><td class="muted num">${o.id}</td><td><b>${esc(o.symbol)}</b></td><td>${dirBadge(o.direction)}</td>
      <td class="num">${fmtNum(o.lots)}</td><td class="num">${fmtNum(o.entry, 5)}</td>
      <td class="num">${o.exit_price ? fmtNum(o.exit_price, 5) : "—"}</td><td>${pnlCell(o.pnl)}</td>
      <td class="muted">${esc(o.executor)}</td><td class="muted">${tsLocal(o.ts_utc)}</td></tr>`),
    "Chưa có lệnh nào. Lịch sử sẽ hiện ở đây — kể cả thắng lẫn thua, không giấu cái nào."
  );
}

async function loadSignals() {
  const rows = await api("/api/signals?limit=100");
  $("#tab-signals").innerHTML = tableWrap(
    `<th>#</th><th>Symbol</th><th>Setup</th><th>Hướng</th><th class="num">Conf</th><th>Kết quả gate</th><th>Lúc</th>`,
    rows.map((s) => `<tr><td class="muted num">${s.id}</td><td><b>${esc(s.symbol)}</b></td>
      <td class="muted">${esc(s.setup_type)}</td><td>${dirBadge(s.direction)}</td>
      <td class="num">${s.confidence ? Math.round(s.confidence * 100) + "%" : "—"}</td>
      <td>${s.approved ? `<span class="tag-ok">✓ duyệt</span>` : `<span class="reject">${esc(JSON.parse(s.reject_reasons || "[]").join(" · ") || "không qua não")}</span>`}</td>
      <td class="muted">${tsLocal(s.ts_utc)}</td></tr>`),
    "Chưa có signal nào — pre-filter đang loại nhiễu trước khi tốn token. Im lặng = đúng thiết kế."
  );
}

async function loadLogs() {
  const data = await api("/api/logs?lines=150");
  const box = $("#logBox");
  box.textContent = data.lines.length ? data.lines.join("\n") : "Log trống.";
  box.scrollTop = box.scrollHeight;
}

/* ---------- Tabs ---------- */
const tabLoaders = { positions: () => {}, orders: loadOrders, signals: loadSignals, logs: loadLogs };
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    state.activeTab = btn.dataset.tab;
    $(`#tab-${state.activeTab}`).classList.add("active");
    tabLoaders[state.activeTab]();
  })
);

/* ---------- Controls ---------- */
$("#scanBtn").addEventListener("click", async () => {
  try {
    await api("/api/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile: "day" }) });
    pollScan();
  } catch (e) {
    if (e.status !== 409) alert("Không chạy được scan: " + e.body);
  }
  refresh();
});

function pollScan() {
  clearInterval(state.scanPolling);
  state.scanPolling = setInterval(async () => {
    const r = await api("/api/scan/last").catch(() => null);
    if (r && !r.running) {
      clearInterval(state.scanPolling);
      refresh(); loadCurve();
      if (state.activeTab !== "positions") tabLoaders[state.activeTab]();
    }
  }, 2500);
}

$("#pauseBtn").addEventListener("click", async () => {
  const paused = state.status?.paused;
  await api("/api/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paused: !paused }) });
  refresh();
});

/* ---------- Mode switch modal ---------- */
$("#modeBadge").addEventListener("click", async () => {
  const cur = state.status?.mode || "demo";
  if (cur === "live") {
    await api("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "demo" }) });
    refresh();
    return;
  }
  const r = await api("/api/live-readiness");
  $("#modalBody").innerHTML = `
    <p style="margin:0;color:var(--text-2);font-size:13px">
      LIVE = <b>tiền thật, mất thật</b>. Đây là số liệu demo của bot tới giờ — nhìn nó trước khi quyết:
    </p>
    <div class="modal-stats">
      <div class="stat"><span class="label">Lệnh đã đóng</span><span class="num">${r.closed_trades}</span></div>
      <div class="stat"><span class="label">Win rate</span><span class="num">${r.win_rate === null ? "—" : Math.round(r.win_rate * 100) + "%"}</span></div>
      <div class="stat"><span class="label">Tổng P&L demo</span><span class="num ${r.total_pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(r.total_pnl)}</span></div>
      <div class="stat"><span class="label">Ngày dữ liệu</span><span class="num">${r.days_of_data}</span></div>
    </div>
    ${r.warnings.length ? `<ul class="warn-list">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <p style="font-size:12.5px;color:var(--text-2)">Gõ <b class="num">${r.confirm_phrase}</b> để xác nhận:</p>
    <input id="confirmInput" class="confirm-input" autocomplete="off" spellcheck="false" placeholder="${r.confirm_phrase}" />`;
  $("#modal").hidden = false;
  const input = $("#confirmInput"), confirmBtn = $("#modalConfirm");
  confirmBtn.disabled = true;
  input.addEventListener("input", () => { confirmBtn.disabled = input.value !== r.confirm_phrase; });
  input.focus();
});

$("#modalCancel").addEventListener("click", () => ($("#modal").hidden = true));
$("#modal").addEventListener("click", (e) => { if (e.target === $("#modal")) $("#modal").hidden = true; });
$("#modalConfirm").addEventListener("click", async () => {
  try {
    await api("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "live", confirm: $("#confirmInput").value }) });
    $("#modal").hidden = true;
    refresh();
  } catch (e) {
    alert("Không chuyển được: " + (JSON.parse(e.body || "{}").detail || e.message));
  }
});

/* ---------- MT5 setup modal ---------- */
let mt5Target = "demo";

async function syncMt5Form() {
  const cfg = await api("/api/mt5-config");
  document.querySelectorAll("#mt5Modal .seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.target === mt5Target));
  const cur = cfg[mt5Target];
  $("#mt5Login").value = cur.login || "";
  $("#mt5Server").value = cur.server || "";
  $("#mt5Password").value = "";
  $("#mt5Password").placeholder = cur.password_set ? "•••••• (đã đặt — nhập để thay)" : "mật khẩu trading";
}

$("#mt5SetupBtn").addEventListener("click", async () => {
  mt5Target = "demo";
  await syncMt5Form();
  $("#mt5Modal").hidden = false;
  $("#mt5Login").focus();
});
document.querySelectorAll("#mt5Modal .seg button").forEach((b) =>
  b.addEventListener("click", async () => { mt5Target = b.dataset.target; await syncMt5Form(); })
);
$("#mt5Cancel").addEventListener("click", () => ($("#mt5Modal").hidden = true));
$("#mt5Modal").addEventListener("click", (e) => { if (e.target === $("#mt5Modal")) $("#mt5Modal").hidden = true; });
$("#mt5Save").addEventListener("click", async () => {
  try {
    await api("/api/mt5-config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target: mt5Target,
        login: $("#mt5Login").value,
        password: $("#mt5Password").value,
        server: $("#mt5Server").value,
      }),
    });
    $("#mt5Modal").hidden = true;
    refresh();
  } catch (e) {
    alert("Không lưu được: " + (JSON.parse(e.body || "{}").detail || e.message));
  }
});

/* ---------- Poll loop ---------- */
async function refresh() {
  try {
    const s = await api("/api/status");
    state.status = s;
    $("#connBanner").hidden = true;
    renderStatus(s);
  } catch {
    $("#connBanner").hidden = false;
  }
}

initChart();
refresh();
loadCurve();
setInterval(refresh, 5000);
setInterval(() => { if (state.activeTab === "logs") loadLogs(); }, 8000);
