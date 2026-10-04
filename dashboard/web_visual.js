"use strict";

import { requestJSON } from "/assets/api.js";

const VISUAL_SVG = "http://www.w3.org/2000/svg";
const visualNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const visualCompact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const v$ = (id) => document.getElementById(id);
const vText = (id, value) => { if (v$(id).textContent !== value) v$(id).textContent = value; };
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
const chartLabelLayouts = new WeakMap();
function prepareChartLabels(svg) {
  // The plot stretches independently on each axis. Counter-scale its labels so
  // a 10px glyph remains a 10px glyph at mobile widths. Read layout once/render.
  const rect = svg.getBoundingClientRect();
  const viewBox = svg.viewBox?.baseVal;
  const width = viewBox?.width || 1100;
  const height = viewBox?.height || 430;
  const measurable = rect.width > 0 && rect.height > 0;
  const layout = {
    width, height, measurable,
    scaleX: measurable ? width / rect.width : 1,
    scaleY: measurable ? height / rect.height : 1,
    ticks: measurable ? Math.min(4, Math.max(1, Math.floor(rect.width / 140))) : 4,
  };
  chartLabelLayouts.set(svg, layout);
  return layout;
}
function svgText(svg, x, y, label, anchor = "start") {
  const layout = chartLabelLayouts.get(svg) || { width: 1100, height: 430, scaleX: 1, scaleY: 1 };
  const { scaleX, scaleY } = layout;
  // Monospaced ASCII is ~0.6em; allow 0.7em plus full em CJK for safe edge padding.
  const glyphWidth = [...String(label)].reduce((sum, char) => sum + (char.charCodeAt(0) > 255 ? 10 : 7), 0) * scaleX;
  const pad = 3 * scaleX;
  const left = anchor === "middle" ? glyphWidth / 2 : anchor === "end" ? glyphWidth : 0;
  const right = anchor === "middle" ? glyphWidth / 2 : anchor === "end" ? 0 : glyphWidth;
  const labelX = Math.max(pad + left, Math.min(layout.width - pad - right, x));
  const labelY = Math.max(12 * scaleY, Math.min(layout.height - 3 * scaleY, y));
  svg.append(svgEl("text", {
    x: 0, y: 0, "text-anchor": anchor,
    transform: `translate(${labelX} ${labelY}) scale(${scaleX} ${scaleY})`,
    class: "chart-label",
  }, label));
}
function showVisualEmpty(id, message) { v$(id).hidden = false; v$(id).textContent = message; }

// Bind once, and limit pointer work to one update per animation frame.
function bindChartInteraction(id, plot, rows, selected, select, round = Math.round) {
  const svg = v$(id);
  let frame = 0;
  let clientX = 0;
  const cancel = () => { cancelAnimationFrame(frame); frame = 0; };
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("pointermove", (event) => {
    clientX = event.clientX;
    if (frame || !rows().length) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const count = rows().length;
      const rect = svg.getBoundingClientRect();
      if (!count || !rect.width) return;
      const width = svg.viewBox.baseVal.width || 1100;
      const x = (clientX - rect.left) / rect.width * width;
      const index = round((x - plot.left) / (plot.right - plot.left) * (round === Math.floor ? count : count - 1));
      select(index);
    });
  });
  svg.addEventListener("pointerleave", () => { cancel(); select(rows().length - 1); });
  svg.addEventListener("keydown", (event) => {
    if (!rows().length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    cancel();
    const index = event.key === "Home" ? 0 : event.key === "End" ? rows().length - 1
      : selected() + (event.key === "ArrowLeft" ? -1 : 1);
    select(index);
  });
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
let candleCatalogRequest = 0;
let candleReady = false;
let candleInitializing = false;
let candleThemeDirty = false;
let candleLimit = window.innerWidth <= 650 ? 60 : 120;
const CANDLE_PLOT = { left: 58, right: 1030, top: 22, bottom: 303, volumeTop: 336, volumeBottom: 402 };

function selectCandle(index) {
  if (!candleData.length) return;
  const next = Math.max(0, Math.min(candleData.length - 1, index));
  if (selectedCandle === next) return;
  selectedCandle = next;
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
  candleThemeDirty = false;
  candlePayload = payload;
  candleData = Array.isArray(payload.candles) ? payload.candles : [];
  const svg = v$("candleChart");
  svg.replaceChildren();
  candleCrosshair = null;
  selectedCandle = -1;
  if (!candleData.length) {
    clearCandles("当前交易对没有可显示的 K 线");
    vText("marketStatus", "当前交易对没有可用行情");
    return;
  }
  v$("candleEmpty").hidden = true;
  const labelLayout = prepareChartLabels(svg);
  const p = CANDLE_PLOT;
  const colors = visualPalette();
  const lowRaw = Math.min(...candleData.map((row) => row.low));
  const highRaw = Math.max(...candleData.map((row) => row.high));
  const pad = Math.max((highRaw - lowRaw) * .055, Math.abs(highRaw) * .001, .000001);
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
  for (let tick = 0; tick <= labelLayout.ticks; tick += 1) {
    const index = Math.min(candleData.length - 1, Math.round(tick * (candleData.length - 1) / labelLayout.ticks));
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

function clearCandles(message) {
  candleData = [];
  candlePayload = null;
  candleCrosshair = null;
  selectedCandle = -1;
  v$("candleChart").replaceChildren();
  ["candleDate", "candleOpen", "candleHigh", "candleLow", "candleClose", "candleVolume", "candlePeriod"].forEach((id) => vText(id, "—"));
  showVisualEmpty("candleEmpty", message);
}

async function loadCandles() {
  const symbol = v$("marketSymbol").value;
  if (!symbol) return false;
  const request = ++candleRequest;
  candleReady = false;
  clearCandles("正在读取行情…");
  document.dispatchEvent(new CustomEvent("dashboard:market-loading", { detail: { symbol, limit: candleLimit } }));
  try {
    vText("marketStatus", "正在读取本地行情缓存...");
    const payload = await requestJSON(`/api/candles?symbol=${encodeURIComponent(symbol)}&limit=${candleLimit}`, { key: "market-candles", timeout: 15000 });
    if (request !== candleRequest) return false;
    renderCandles(payload);
    document.dispatchEvent(new CustomEvent("dashboard:market-loaded", { detail: { symbol, limit: candleLimit } }));
    candleReady = candleData.length > 0;
    return candleReady;
  } catch (_) {
    if (request !== candleRequest) return false;
    candleReady = false;
    clearCandles("无法读取此交易对的本地 K 线，可刷新后重试");
    vText("marketStatus", "行情缓存读取失败");
    document.dispatchEvent(new CustomEvent("dashboard:market-error", { detail: { symbol } }));
    return false;
  }
}
async function initCandles(force = false) {
  if (candleInitializing && !force) return;
  candleInitializing = true;
  candleReady = false;
  v$("marketSymbol").disabled = true;
  const request = ++candleCatalogRequest;
  ++candleRequest;
  try {
    const payload = await requestJSON("/api/markets", { key: "market-catalog", timeout: 10000 });
    if (request !== candleCatalogRequest) return;
    const symbols = Array.isArray(payload.markets) ? payload.markets : [];
    const select = v$("marketSymbol");
    const previous = select.value;
    select.replaceChildren();
    symbols.forEach((symbol) => {
      const option = document.createElement("option");
      option.value = symbol; option.textContent = symbol;
      select.append(option);
    });
    if (!symbols.length) { vText("marketStatus", "没有本地日线缓存"); clearCandles("没有找到本地日线缓存"); return; }
    if (symbols.includes(previous)) select.value = previous;
    await loadCandles();
  } catch (_) {
    if (request !== candleCatalogRequest) return;
    clearCandles("无法读取本地行情目录，可刷新后重试");
    vText("marketStatus", "行情目录不可用");
  } finally {
    if (request === candleCatalogRequest) {
      candleInitializing = false;
      v$("marketSymbol").disabled = v$("marketSymbol").options.length === 0;
    }
  }
}

let backtestData = [];
let backtestCrosshair = null;
let backtestStartEquity = null;
let backtestStartBenchmark = null;
let backtestPayload = null;
let backtestRequest = 0;
let backtestCatalogRequest = 0;
let backtestReady = false;
let backtestInitializing = false;
let backtestThemeDirty = false;
let selectedBacktest = -1;
const BACKTEST_PLOT = { left: 58, right: 1030, top: 22, equityBottom: 244, drawdownTop: 292, bottom: 355 };

function selectBacktest(index) {
  if (!backtestData.length) return;
  index = Math.max(0, Math.min(backtestData.length - 1, index));
  if (selectedBacktest === index) return;
  selectedBacktest = index;
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
  backtestThemeDirty = false;
  backtestPayload = payload;
  backtestData = Array.isArray(payload.points) ? payload.points : [];
  selectedBacktest = -1;
  backtestCrosshair = null;
  const svg = v$("backtestChart");
  svg.replaceChildren();
  const metrics = payload.metrics || {};
  vText("backtestReturn", vPercent(metrics.total_return, true));
  vText("backtestDrawdown", vPercent(metrics.max_drawdown));
  vText("backtestEndEquity", vMoney(metrics.end_equity));
  vText("backtestFills", Number.isFinite(metrics.fills_count) ? String(metrics.fills_count) : "—");
  v$("backtestReturn").classList.toggle("negative", metrics.total_return < 0);
  vText("backtestStatus", `${payload.id} · ${backtestData.length} 条权益记录${payload.invalid_rows ? ` · 忽略 ${payload.invalid_rows} 条无效记录` : ""}`);
  vText("backtestPeriod", `${payload.period_start || "—"} 至 ${payload.period_end || "—"} · equity.csv`);
  if (backtestData.length < 2) {
    v$("benchmarkToggle").disabled = true;
    vText("backtestHover", "至少需要两条权益记录才能绘制趋势");
    showVisualEmpty("backtestEmpty", "该报告没有足够的权益记录");
    return;
  }
  v$("backtestEmpty").hidden = true;
  const labelLayout = prepareChartLabels(svg);
  const p = BACKTEST_PLOT;
  const colors = visualPalette();
  backtestStartEquity = backtestData[0].equity;
  backtestStartBenchmark = payload.benchmark_aligned === true ? backtestData[0]?.benchmark || null : null;
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
  for (let tick = 0; tick <= labelLayout.ticks; tick += 1) {
    const index = Math.min(backtestData.length - 1, Math.round(tick * (backtestData.length - 1) / labelLayout.ticks));
    svgText(svg, xFor(index), 382, backtestData[index].timestamp.slice(0, 10), "middle");
  }
  const stride = Math.max(1, Math.ceil(backtestData.length / 850));
  const sampled = [];
  let trough = 0;
  for (let index = 1; index < backtestData.length; index += 1) {
    if (backtestData[index].drawdown < backtestData[trough].drawdown) trough = index;
  }
  for (let index = 0; index < backtestData.length; index += 1) {
    if (index % stride === 0 || index === backtestData.length - 1 || index === trough) {
      sampled.push({ ...backtestData[index], index });
    }
  }
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
  vText("backtestPeriod", `${payload.period_start} 至 ${payload.period_end} · equity.csv${showBenchmark ? " · 虚线为基准（同轴）" : ""}`);
  selectBacktest(backtestData.length - 1);
}

function clearBacktest(message) {
  backtestData = [];
  backtestPayload = null;
  backtestCrosshair = null;
  backtestStartEquity = null;
  backtestStartBenchmark = null;
  selectedBacktest = -1;
  v$("backtestChart").replaceChildren();
  v$("backtestReturn").classList.remove("negative");
  v$("benchmarkToggle").disabled = true;
  ["backtestReturn", "backtestDrawdown", "backtestEndEquity", "backtestFills", "backtestPeriod", "backtestHover"].forEach((id) => vText(id, "—"));
  showVisualEmpty("backtestEmpty", message);
}

async function loadBacktest(preloaded = null) {
  const id = v$("backtestRun").value;
  if (!id) return false;
  const request = ++backtestRequest;
  backtestReady = false;
  clearBacktest("正在读取回测报告…");
  document.dispatchEvent(new CustomEvent("dashboard:backtest-loading", { detail: { id } }));
  try {
    vText("backtestStatus", "正在读取回测权益文件...");
    const payload = preloaded?.id === id && Array.isArray(preloaded.points) ? preloaded
      : await requestJSON(`/api/backtest?id=${encodeURIComponent(id)}`, { key: "backtest-result", timeout: 20000 });
    if (request !== backtestRequest) return false;
    renderBacktest(payload);
    backtestReady = true;
    document.dispatchEvent(new CustomEvent("dashboard:backtest-loaded", { detail: payload }));
    return true;
  } catch (error) {
    if (request !== backtestRequest) return false;
    backtestReady = false;
    clearBacktest("无法读取所选回测报告，可刷新后重试");
    vText("backtestStatus", "回测报告读取失败");
    document.dispatchEvent(new CustomEvent("dashboard:backtest-error", { detail: { id, message: error.message || "回测报告读取失败" } }));
    return false;
  }
}
async function initBacktests({ force = false, runId = null } = {}) {
  if (backtestInitializing && !force) return;
  backtestInitializing = true;
  backtestReady = false;
  v$("backtestRun").disabled = true;
  const request = ++backtestCatalogRequest;
  ++backtestRequest;
  clearBacktest("正在读取回测目录…");
  document.dispatchEvent(new CustomEvent("dashboard:backtest-loading", { detail: { id: runId || v$("backtestRun").value } }));
  try {
    const payload = await requestJSON("/api/backtests", { key: "backtest-catalog", timeout: 10000 });
    if (request !== backtestCatalogRequest) return;
    const runs = Array.isArray(payload.runs) ? payload.runs : [];
    let archived = null;
    if (runId && !runs.some((run) => run.id === runId)) {
      archived = await requestJSON(`/api/backtest?id=${encodeURIComponent(runId)}`, { key: "backtest-result", timeout: 20000 });
      if (request !== backtestCatalogRequest) return;
      runs.unshift({ id: archived.id, source: archived.parameters?.source });
    }
    const select = v$("backtestRun");
    const previous = runId || select.value;
    select.replaceChildren();
    runs.forEach((run) => {
      const option = document.createElement("option");
      option.value = run.id;
      const source = run.source === "synthetic" ? "合成 · " : run.source === "local" ? "历史 · " : "";
      option.textContent = source + run.id.replaceAll("_", " · ");
      select.append(option);
    });
    if (!runs.length) {
      vText("backtestStatus", "没有本地回测权益报告");
      clearBacktest("没有找到可视化的 equity.csv 回测报告");
      document.dispatchEvent(new CustomEvent("dashboard:backtest-error", { detail: { id: runId, message: "没有可用的回测报告" } }));
      return;
    }
    if (runs.some((run) => run.id === previous)) select.value = previous;
    if (runId && select.value !== runId) throw new Error("新报告尚未出现在目录中，请刷新后重试");
    await loadBacktest(archived);
  } catch (error) {
    if (request !== backtestCatalogRequest) return;
    clearBacktest("无法读取本地回测报告目录，可刷新后重试");
    vText("backtestStatus", "回测目录不可用");
    document.dispatchEvent(new CustomEvent("dashboard:backtest-error", { detail: { id: runId, message: error.message || "回测目录不可用" } }));
  } finally {
    if (request === backtestCatalogRequest) {
      backtestInitializing = false;
      v$("backtestRun").disabled = v$("backtestRun").options.length === 0;
    }
  }
}

let previousObservation = null;
let seenAlerts = new Set();
let activityEvents = [];
let activityVersion = "";
function addActivity(title, detail, level = "normal", time = new Date().toISOString()) {
  activityEvents.unshift({ title, detail, level, time });
  activityEvents = activityEvents.slice(0, 20);
}
function renderActivity() {
  const version = JSON.stringify([previousObservation?.mode, activityEvents]);
  if (version === activityVersion) return;
  activityVersion = version;
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
  const view = document.documentElement.dataset.view;
  candleThemeDirty = true;
  backtestThemeDirty = true;
  if (view === "market" && candlePayload) renderCandles(candlePayload);
  if (view === "backtest" && backtestPayload) renderBacktest(backtestPayload);
});

v$("marketSymbol").addEventListener("change", loadCandles);
const candleRangeButtons = document.querySelectorAll(".range-buttons button[data-limit]");
function syncCandleRangeButtons() {
  candleRangeButtons.forEach((button) => {
    const selected = Number(button.dataset.limit) === candleLimit;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}
syncCandleRangeButtons();
candleRangeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    candleLimit = Number(button.dataset.limit);
    syncCandleRangeButtons();
    if (!candleInitializing) loadCandles();
  });
});
v$("backtestRun").addEventListener("change", loadBacktest);
v$("benchmarkToggle").addEventListener("change", () => { if (backtestPayload) renderBacktest(backtestPayload); });
bindChartInteraction("candleChart", CANDLE_PLOT, () => candleData, () => selectedCandle, selectCandle, Math.floor);
bindChartInteraction("backtestChart", BACKTEST_PLOT, () => backtestData, () => selectedBacktest, selectBacktest);

function activateView(view) {
  if (view === "market") {
    if (!candleReady) initCandles();
    else if ((candleThemeDirty || !chartLabelLayouts.get(v$("candleChart"))?.measurable) && candlePayload) renderCandles(candlePayload);
  }
  if (view === "backtest") {
    if (!backtestReady) initBacktests();
    else if ((backtestThemeDirty || !chartLabelLayouts.get(v$("backtestChart"))?.measurable) && backtestPayload) renderBacktest(backtestPayload);
  }
}
document.addEventListener("dashboard:view", (event) => activateView(event.detail.view));
document.addEventListener("dashboard:refresh", () => {
  const view = document.documentElement.dataset.view;
  if (view === "market") initCandles(true);
  if (view === "backtest") initBacktests({ force: true });
});
document.addEventListener("dashboard:backtest-complete", (event) => {
  if (event.detail?.run_id) initBacktests({ force: true, runId: event.detail.run_id });
});
// A lazily imported analysis module can request the already loaded result.
document.addEventListener("dashboard:backtest-current", () => {
  if (backtestPayload) document.dispatchEvent(new CustomEvent("dashboard:backtest-loaded", { detail: backtestPayload }));
});
let chartResizeFrame = 0;
window.addEventListener("resize", () => {
  candleThemeDirty = true;
  backtestThemeDirty = true;
  if (chartResizeFrame) return;
  chartResizeFrame = requestAnimationFrame(() => {
    chartResizeFrame = 0;
    const view = document.documentElement.dataset.view;
    if (view === "market" && candlePayload) renderCandles(candlePayload);
    if (view === "backtest" && backtestPayload) renderBacktest(backtestPayload);
  });
});
// Modules may execute before or after workspace.js emits its initial event.
activateView(document.documentElement.dataset.view || "overview");
