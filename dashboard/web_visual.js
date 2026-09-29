"use strict";

const VISUAL_SVG = "http://www.w3.org/2000/svg";
const visualNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const visualCompact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const v$ = (id) => document.getElementById(id);
const vText = (id, value) => { v$(id).textContent = value; };
const vMoney = (value) => Number.isFinite(value) ? visualNumber.format(value) : "—";
const vPercent = (value, signed = false) => Number.isFinite(value)
  ? `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%` : "—";
const vTime = (value) => {
  const point = new Date(value);
  return Number.isNaN(point.getTime()) ? String(value || "—")
    : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(point);
};

function svgEl(tag, attributes = {}, label) {
  const item = document.createElementNS(VISUAL_SVG, tag);
  Object.entries(attributes).forEach(([key, value]) => item.setAttribute(key, String(value)));
  if (label !== undefined) item.textContent = label;
  return item;
}
function svgLine(svg, x1, y1, x2, y2, stroke, width = 1, dash = "") {
  const item = svgEl("line", { x1, y1, x2, y2, stroke, "stroke-width": width });
  if (dash) item.setAttribute("stroke-dasharray", dash);
  svg.append(item);
  return item;
}
function svgText(svg, x, y, label, anchor = "start") {
  svg.append(svgEl("text", { x, y, "text-anchor": anchor }, label));
}
function showVisualEmpty(id, message) { v$(id).hidden = false; v$(id).textContent = message; }
async function getVisualJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function visualPalette() {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  return {
    grid: color("--chart-grid"),
    up: color("--chart-up"),
    down: color("--chart-down"),
    benchmark: color("--chart-benchmark"),
    crosshair: color("--chart-crosshair"),
    fill: color("--chart-fill"),
  };
}

let candleData = [];
let candlePayload = null;
let selectedCandle = -1;
let candleCrosshair = null;
let candleRequest = 0;
let candleLimit = window.innerWidth <= 650 ? 60 : 120;
const CANDLE_PLOT = { left: 58, right: 1030, top: 22, bottom: 303, volumeTop: 336, volumeBottom: 402 };

function selectCandle(index) {
  if (!candleData.length) return;
  selectedCandle = Math.max(0, Math.min(candleData.length - 1, index));
  const item = candleData[selectedCandle];
  vText("candleDate", item.timestamp);
  vText("candleOpen", vMoney(item.open));
  vText("candleHigh", vMoney(item.high));
  vText("candleLow", vMoney(item.low));
  vText("candleClose", vMoney(item.close));
  vText("candleVolume", visualCompact.format(item.volume));
  if (candleCrosshair) {
    const x = CANDLE_PLOT.left + (selectedCandle + .5) * (CANDLE_PLOT.right - CANDLE_PLOT.left) / candleData.length;
    candleCrosshair.setAttribute("x1", x.toFixed(2));
    candleCrosshair.setAttribute("x2", x.toFixed(2));
  }
}

function renderCandles(payload) {
  candlePayload = payload;
  candleData = Array.isArray(payload.candles) ? payload.candles : [];
  const svg = v$("candleChart");
  svg.replaceChildren();
  if (!candleData.length) { showVisualEmpty("candleEmpty", "当前交易对没有可显示的 K 线"); return; }
  v$("candleEmpty").hidden = true;
  const p = CANDLE_PLOT;
  const colors = visualPalette();
  const lowRaw = Math.min(...candleData.map((row) => row.low));
  const highRaw = Math.max(...candleData.map((row) => row.high));
  const pad = Math.max((highRaw - lowRaw) * .055, highRaw * .001);
  const low = lowRaw - pad;
  const high = highRaw + pad;
  const maxVolume = Math.max(...candleData.map((row) => row.volume), 1);
  const toY = (value) => p.top + (high - value) / (high - low) * (p.bottom - p.top);
  const step = (p.right - p.left) / candleData.length;
  const width = Math.max(2, Math.min(step * .62, 12));

  for (let tick = 0; tick <= 4; tick += 1) {
    const y = p.top + tick * (p.bottom - p.top) / 4;
    svgLine(svg, p.left, y, p.right, y, colors.grid, 1, "4 6");
    svgText(svg, p.right + 11, y + 4, visualCompact.format(high - tick * (high - low) / 4));
  }
  svgLine(svg, p.left, 320, p.right, 320, colors.grid);
  for (let tick = 0; tick <= 4; tick += 1) {
    const index = Math.min(candleData.length - 1, Math.round(tick * (candleData.length - 1) / 4));
    const x = p.left + (index + .5) * step;
    svgText(svg, x, 422, candleData[index].timestamp.slice(0, 10), "middle");
  }
  candleData.forEach((row, index) => {
    const x = p.left + (index + .5) * step;
    const rising = row.close >= row.open;
    const color = rising ? colors.up : colors.down;
    svgLine(svg, x, toY(row.high), x, toY(row.low), color, Math.max(1, width * .17));
    const bodyTop = Math.min(toY(row.open), toY(row.close));
    const bodyHeight = Math.max(1.7, Math.abs(toY(row.open) - toY(row.close)));
    svg.append(svgEl("rect", { x: x - width / 2, y: bodyTop, width, height: bodyHeight, rx: 1, fill: color }));
    const volumeHeight = row.volume / maxVolume * (p.volumeBottom - p.volumeTop);
    svg.append(svgEl("rect", { x: x - width / 2, y: p.volumeBottom - volumeHeight, width, height: volumeHeight, fill: color, opacity: .38 }));
  });
  candleCrosshair = svgLine(svg, p.left, p.top, p.left, p.volumeBottom, colors.crosshair, 1, "4 4");
  candleCrosshair.setAttribute("pointer-events", "none");
  selectCandle(candleData.length - 1);
  const days = Number.isFinite(payload.age_days) ? payload.age_days : "?";
  vText("marketStatus", `${payload.symbol} · 历史日线 · 最后一根 ${payload.last_candle} · 距今 ${days} 天`);
  vText("candlePeriod", `${candleData[0].timestamp} 至 ${candleData[candleData.length - 1].timestamp} · 本地缓存`);
}

async function loadCandles() {
  const symbol = v$("marketSymbol").value;
  if (!symbol) return;
  const request = ++candleRequest;
  try {
    vText("marketStatus", "正在读取本地行情缓存...");
    const payload = await getVisualJson(`/api/candles?symbol=${encodeURIComponent(symbol)}&limit=${candleLimit}`);
    if (request === candleRequest) renderCandles(payload);
  } catch (_) {
    if (request !== candleRequest) return;
    candleData = [];
    vText("marketStatus", "行情缓存读取失败");
    showVisualEmpty("candleEmpty", "无法读取此交易对的本地 K 线");
  }
}
async function initCandles() {
  try {
    const payload = await getVisualJson("/api/markets");
    const symbols = Array.isArray(payload.markets) ? payload.markets : [];
    const select = v$("marketSymbol");
    select.replaceChildren();
    symbols.forEach((symbol) => {
      const option = document.createElement("option");
      option.value = symbol; option.textContent = symbol;
      select.append(option);
    });
    if (!symbols.length) { vText("marketStatus", "没有本地日线缓存"); showVisualEmpty("candleEmpty", "没有找到本地日线缓存"); return; }
    select.addEventListener("change", loadCandles);
    document.querySelectorAll(".range-buttons button").forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.limit) === candleLimit);
      button.addEventListener("click", () => {
        candleLimit = Number(button.dataset.limit);
        document.querySelectorAll(".range-buttons button").forEach((item) => item.classList.toggle("active", item === button));
        loadCandles();
      });
    });
    v$("candleChart").addEventListener("pointermove", (event) => {
      if (!candleData.length) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width * 1100;
      const index = Math.floor((x - CANDLE_PLOT.left) / (CANDLE_PLOT.right - CANDLE_PLOT.left) * candleData.length);
      selectCandle(index);
    });
    v$("candleChart").addEventListener("pointerleave", () => selectCandle(candleData.length - 1));
    loadCandles();
  } catch (_) {
    vText("marketStatus", "行情目录不可用");
    showVisualEmpty("candleEmpty", "无法读取本地行情目录");
  }
}

let backtestData = [];
let backtestCrosshair = null;
let backtestStartEquity = null;
let backtestStartBenchmark = null;
let backtestPayload = null;
let backtestRequest = 0;
const BACKTEST_PLOT = { left: 58, right: 1030, top: 22, equityBottom: 244, drawdownTop: 292, bottom: 355 };

function selectBacktest(index) {
  if (!backtestData.length) return;
  index = Math.max(0, Math.min(backtestData.length - 1, index));
  const item = backtestData[index];
  const cumulative = item.equity / backtestStartEquity - 1;
  const benchmark = Number.isFinite(item.benchmark) && backtestStartBenchmark
    ? ` · 基准 ${vPercent(item.benchmark / backtestStartBenchmark - 1, true)}` : "";
  vText("backtestHover", `${item.timestamp} · 权益 ${vMoney(item.equity)} · 累计 ${vPercent(cumulative, true)} · 回撤 ${vPercent(item.drawdown)}${benchmark}`);
  if (backtestCrosshair) {
    const x = BACKTEST_PLOT.left + index * (BACKTEST_PLOT.right - BACKTEST_PLOT.left) / Math.max(1, backtestData.length - 1);
    backtestCrosshair.setAttribute("x1", x.toFixed(2));
    backtestCrosshair.setAttribute("x2", x.toFixed(2));
  }
}

function renderBacktest(payload) {
  backtestPayload = payload;
  backtestData = Array.isArray(payload.points) ? payload.points : [];
  const svg = v$("backtestChart");
  svg.replaceChildren();
  if (backtestData.length < 2) { showVisualEmpty("backtestEmpty", "该报告没有足够的权益记录"); return; }
  v$("backtestEmpty").hidden = true;
  const p = BACKTEST_PLOT;
  const colors = visualPalette();
  const metrics = payload.metrics || {};
  backtestStartEquity = backtestData[0].equity;
  backtestStartBenchmark = backtestData.find((point) => Number.isFinite(point.benchmark))?.benchmark || null;
  const returns = backtestData.map((item) => item.equity / backtestStartEquity - 1);
  const benchmarkReturns = backtestStartBenchmark
    ? backtestData.filter((item) => Number.isFinite(item.benchmark)).map((item) => item.benchmark / backtestStartBenchmark - 1) : [];
  const showBenchmark = v$("benchmarkToggle").checked && benchmarkReturns.length > 1;
  v$("benchmarkToggle").disabled = benchmarkReturns.length < 2;
  const displayReturns = showBenchmark ? returns.concat(benchmarkReturns) : returns;
  const minRaw = Math.min(...displayReturns);
  const maxRaw = Math.max(...displayReturns);
  const padding = Math.max((maxRaw - minRaw) * .1, .02);
  const min = minRaw - padding;
  const max = maxRaw + padding;
  const xFor = (index) => p.left + index * (p.right - p.left) / (backtestData.length - 1);
  const yFor = (value) => p.top + (max - value) / (max - min) * (p.equityBottom - p.top);
  const deepest = Math.min(metrics.max_drawdown || 0, -.001);
  const ddFor = (value) => p.drawdownTop + Math.abs(value) / Math.abs(deepest) * (p.bottom - p.drawdownTop);
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = p.top + tick * (p.equityBottom - p.top) / 4;
    svgLine(svg, p.left, y, p.right, y, colors.grid, 1, "4 6");
    svgText(svg, p.right + 10, y + 4, vPercent(max - tick * (max - min) / 4));
  }
  svgText(svg, p.left, 15, "累计收益");
  svgLine(svg, p.left, p.drawdownTop, p.right, p.drawdownTop, colors.grid);
  svgText(svg, p.left, p.drawdownTop - 8, "回撤");
  svgText(svg, p.right + 10, p.bottom + 3, vPercent(deepest));
  for (let tick = 0; tick <= 4; tick += 1) {
    const index = Math.min(backtestData.length - 1, Math.round(tick * (backtestData.length - 1) / 4));
    svgText(svg, xFor(index), 382, backtestData[index].timestamp.slice(0, 10), "middle");
  }
  const stride = Math.max(1, Math.ceil(backtestData.length / 850));
  const sampled = backtestData.map((item, index) => ({ ...item, index }))
    .filter((item, index) => index % stride === 0 || index === backtestData.length - 1 || item.drawdown === metrics.max_drawdown);
  const equityPath = sampled.map((item, index) => `${index ? "L" : "M"}${xFor(item.index).toFixed(2)} ${yFor(item.equity / backtestStartEquity - 1).toFixed(2)}`).join(" ");
  svg.append(svgEl("path", { d: `${equityPath} L${p.right} ${p.equityBottom} L${p.left} ${p.equityBottom} Z`, fill: colors.fill, opacity: .09 }));
  const benchmarkSample = sampled.filter((item) => Number.isFinite(item.benchmark));
  if (showBenchmark && benchmarkSample.length > 1 && backtestStartBenchmark) {
    const benchmarkPath = benchmarkSample.map((item, index) => `${index ? "L" : "M"}${xFor(item.index).toFixed(2)} ${yFor(item.benchmark / backtestStartBenchmark - 1).toFixed(2)}`).join(" ");
    svg.append(svgEl("path", { d: benchmarkPath, fill: "none", stroke: colors.benchmark, "stroke-width": 2, "stroke-dasharray": "5 5" }));
  }
  svg.append(svgEl("path", { d: equityPath, fill: "none", stroke: colors.up, "stroke-width": 2.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  const ddPath = sampled.map((item, index) => `${index ? "L" : "M"}${xFor(item.index).toFixed(2)} ${ddFor(item.drawdown).toFixed(2)}`).join(" ");
  svg.append(svgEl("path", { d: `${ddPath} L${p.right} ${p.drawdownTop} L${p.left} ${p.drawdownTop} Z`, fill: colors.down, opacity: .13 }));
  svg.append(svgEl("path", { d: ddPath, fill: "none", stroke: colors.down, "stroke-width": 2 }));
  backtestCrosshair = svgLine(svg, p.left, p.top, p.left, p.bottom, colors.crosshair, 1, "4 4");
  backtestCrosshair.setAttribute("pointer-events", "none");
  vText("backtestReturn", vPercent(metrics.total_return, true));
  vText("backtestDrawdown", vPercent(metrics.max_drawdown));
  vText("backtestEndEquity", vMoney(metrics.end_equity));
  vText("backtestFills", Number.isFinite(metrics.fills_count) ? String(metrics.fills_count) : "—");
  v$("backtestReturn").classList.toggle("negative", metrics.total_return < 0);
  vText("backtestStatus", `${payload.id} · ${backtestData.length} 条权益记录${payload.invalid_rows ? ` · 忽略 ${payload.invalid_rows} 条无效记录` : ""}`);
  vText("backtestPeriod", `${payload.period_start} 至 ${payload.period_end} · equity.csv${showBenchmark ? " · 虚线为基准（同轴）" : ""}`);
  selectBacktest(backtestData.length - 1);
}

async function loadBacktest() {
  const id = v$("backtestRun").value;
  if (!id) return;
  const request = ++backtestRequest;
  try {
    vText("backtestStatus", "正在读取回测权益文件...");
    const payload = await getVisualJson(`/api/backtest?id=${encodeURIComponent(id)}`);
    if (request === backtestRequest) renderBacktest(payload);
  } catch (_) {
    if (request !== backtestRequest) return;
    backtestData = [];
    backtestPayload = null;
    vText("backtestStatus", "回测报告读取失败");
    showVisualEmpty("backtestEmpty", "无法读取所选回测报告");
    ["backtestReturn", "backtestDrawdown", "backtestEndEquity", "backtestFills", "backtestPeriod"].forEach((id) => vText(id, "—"));
  }
}
async function initBacktests() {
  try {
    v$("benchmarkToggle").addEventListener("change", () => { if (backtestPayload) renderBacktest(backtestPayload); });
    const payload = await getVisualJson("/api/backtests");
    const runs = Array.isArray(payload.runs) ? payload.runs : [];
    const select = v$("backtestRun");
    select.replaceChildren();
    runs.forEach((run) => {
      const option = document.createElement("option");
      option.value = run.id; option.textContent = run.id.replaceAll("_", " · ");
      select.append(option);
    });
    if (!runs.length) { vText("backtestStatus", "没有本地回测权益报告"); showVisualEmpty("backtestEmpty", "没有找到可视化的 equity.csv 回测报告"); return; }
    select.addEventListener("change", loadBacktest);
    v$("backtestChart").addEventListener("pointermove", (event) => {
      if (!backtestData.length) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width * 1100;
      const index = Math.round((x - BACKTEST_PLOT.left) / (BACKTEST_PLOT.right - BACKTEST_PLOT.left) * (backtestData.length - 1));
      selectBacktest(index);
    });
    v$("backtestChart").addEventListener("pointerleave", () => selectBacktest(backtestData.length - 1));
    loadBacktest();
  } catch (_) {
    vText("backtestStatus", "回测目录不可用");
    showVisualEmpty("backtestEmpty", "无法读取本地回测报告目录");
  }
}

let previousObservation = null;
let seenAlerts = new Set();
let activityEvents = [];
function addActivity(title, detail, level = "normal", time = new Date().toISOString()) {
  activityEvents.unshift({ title, detail, level, time });
  activityEvents = activityEvents.slice(0, 20);
}
function renderActivity() {
  const list = v$("activityList");
  list.replaceChildren();
  vText("activityCount", `${activityEvents.length} 条`);
  if (!activityEvents.length) {
    const empty = document.createElement("div");
    empty.className = "panel-empty";
    empty.textContent = previousObservation?.mode === "demo" ? "演示模式不生成真实监控变化" : "本次会话尚未观察到新变化";
    list.append(empty);
    return;
  }
  activityEvents.forEach((event) => {
    const row = document.createElement("div"); row.className = `feed-item ${event.level}`;
    const content = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = event.title;
    const detail = document.createElement("small"); detail.textContent = event.detail;
    content.append(title, detail);
    const time = document.createElement("time"); time.textContent = vTime(event.time);
    row.append(content, time); list.append(row);
  });
}
function observeStatus(data) {
  vText("activityChecked", vTime(data.server_time || new Date().toISOString()));
  vText("activitySnapshot", data.status_valid ? vTime(data.timestamp) : "无有效快照");
  const alerts = Array.isArray(data.recent_alerts) ? data.recent_alerts : [];
  const latest = alerts[alerts.length - 1];
  vText("activityAlert", latest ? `${latest.event || "事件"} · ${vTime(latest.timestamp)}` : "无告警记录");
  const alertIds = new Set(alerts.map((alert) => `${alert.timestamp}|${alert.event}|${alert.level}`));
  if (data.mode === "demo") {
    activityEvents = [];
    previousObservation = data;
    seenAlerts = alertIds;
    renderActivity();
    return;
  }
  if (previousObservation) {
    if (data.status_valid && data.timestamp !== previousObservation.timestamp) {
      const delta = Number.isFinite(data.equity) && Number.isFinite(previousObservation.equity)
        ? ` · 较上次 ${data.equity >= previousObservation.equity ? "+" : "−"}${vMoney(Math.abs(data.equity - previousObservation.equity))}` : "";
      addActivity("状态快照更新", `记录时间 ${vTime(data.timestamp)} · 权益 ${vMoney(data.equity)}${delta}`);
    }
    if (data.operational_state !== previousObservation.operational_state && data.status_valid) {
      addActivity("运行状态变化", `${previousObservation.operational_state || "未知"} → ${data.operational_state || "未知"}`, data.healthy ? "normal" : "danger");
    }
    if (data.status_valid !== previousObservation.status_valid) {
      addActivity("快照可用性变化", data.status_valid ? "已恢复读取状态快照" : "状态快照不可用", data.status_valid ? "normal" : "danger");
    }
    if (data.status_valid && previousObservation.status_valid) {
      const currentGate = data.details?.account_entry_gate?.allows_new_risk;
      const previousGate = previousObservation.details?.account_entry_gate?.allows_new_risk;
      if (typeof currentGate === "boolean" && typeof previousGate === "boolean" && currentGate !== previousGate) {
        addActivity("账户开仓门控变化", currentGate ? "变为允许开仓" : "变为禁止开仓", currentGate ? "normal" : "danger");
      }
      const currentPositions = Object.keys(data.positions || {}).length;
      const previousPositions = Object.keys(previousObservation.positions || {}).length;
      if (currentPositions !== previousPositions) {
        addActivity("持仓数量变化", `${previousPositions} → ${currentPositions} 个标的`, "warning");
      }
    }
    alerts.forEach((alert) => {
      const key = `${alert.timestamp}|${alert.event}|${alert.level}`;
      if (!seenAlerts.has(key)) addActivity("新告警", String(alert.event || "未命名事件"), ["error", "critical"].includes(String(alert.level).toLowerCase()) ? "danger" : "warning", alert.timestamp);
    });
  }
  previousObservation = data;
  seenAlerts = alertIds;
  renderActivity();
}

document.addEventListener("dashboard:status", (event) => observeStatus(event.detail));
document.addEventListener("dashboard:theme", () => {
  if (candlePayload) renderCandles(candlePayload);
  if (backtestPayload) renderBacktest(backtestPayload);
});
initCandles();
initBacktests();
