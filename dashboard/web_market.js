import { requestJSON, el, element, formatNumber } from "./api.js";
import { lineChart, table, stats } from "./charts.js";

let marketPayload;
let generation = 0;
let lastSymbol;
let lastLimit = 120;
const studies = {
  trend: [["close", "收盘"], ["sma20", "SMA 20"], ["bb_upper", "布林上轨"], ["bb_lower", "布林下轨"]],
  averages: [["close", "收盘"], ["sma20", "SMA 20"], ["sma50", "SMA 50"]],
  exponential: [["close", "收盘"], ["ema12", "EMA 12"], ["ema26", "EMA 26"]],
  rsi: [["rsi14", "RSI 14"]], macd: [["macd", "MACD"], ["macd_signal", "Signal 9"], ["macd_hist", "Histogram"]],
  atr: [["atr14", "ATR 14"]], adx: [["adx14", "ADX 14"]], stoch: [["stoch_k", "%K 14"], ["stoch_d", "%D 3"]],
  obv: [["obv", "OBV"]], roc: [["roc12", "ROC 12 · %"]], cci: [["cci20", "CCI 20"]], williams: [["williams_r14", "Williams %R 14"]],
};
const labels = { sma20: "SMA 20", sma50: "SMA 50", ema12: "EMA 12", ema26: "EMA 26", rsi14: "RSI 14", macd: "MACD", macd_signal: "MACD Signal", macd_hist: "MACD Histogram", bb_upper: "布林上轨", bb_mid: "布林中轨", bb_lower: "布林下轨", atr14: "ATR 14", adx14: "ADX 14", stoch_k: "Stochastic %K", stoch_d: "Stochastic %D", obv: "OBV", roc12: "ROC 12 · %", cci20: "CCI 20", williams_r14: "Williams %R" };

function methodology(container, value) {
  const rows = Array.isArray(value) ? value : typeof value === "object" && value ? Object.entries(value).map(([name, detail]) => `${name}：${typeof detail === "object" ? JSON.stringify(detail) : detail}`) : [value];
  container.replaceChildren(...rows.filter(Boolean).map((text) => element("p", "", String(text))));
}
function draw() {
  if (!marketPayload) return;
  const selected = studies[el("indicatorStudy").value] || studies.trend;
  lineChart(el("indicatorChart"), selected.map(([key, name]) => ({ name, points: marketPayload.points.map((row) => ({ timestamp: row.timestamp, value: row[key] })) })), { label: "历史技术指标" });
  const entries = ([key, label]) => [label, marketPayload.latest?.[key] ?? (key === "close" ? marketPayload.points.at(-1)?.close : null), null, { type: "number" }];
  stats(el("indicatorStats"), selected.map(entries));
  el("indicatorStats").dataset.columns = String(selected.length);
}
async function loadIndicators(symbol = el("marketSymbol").value, limit = lastLimit) {
  if (!symbol?.includes("/")) return;
  const seq = ++generation; lastSymbol = symbol; lastLimit = limit;
  marketPayload = null;
  el("indicatorContext").textContent = `正在计算 ${symbol} 技术指标…`;
  el("indicatorChart").replaceChildren(); el("indicatorStats").replaceChildren(); el("indicatorAllStats").replaceChildren();
  try {
    const data = await requestJSON(`/api/market-analysis?symbol=${encodeURIComponent(symbol)}&limit=${limit}`, { key: "indicators" });
    if (seq !== generation) return;
    marketPayload = data;
    el("indicatorContext").textContent = `${data.symbol} · 日线 · 截至 ${data.last_candle?.slice(0, 10) || "—"} · 未预热的指标显示 —`;
    stats(el("indicatorAllStats"), Object.entries(labels).map(([key, label]) => [label, data.latest?.[key], data.latest?.[key] == null ? "有效连续样本不足" : null, { type: "number" }]));
    methodology(el("indicatorMethodology"), data.methodology);
    draw();
  } catch (error) {
    if (error.name !== "AbortError" && seq === generation) el("indicatorContext").textContent = `指标读取失败：${error.message}`;
  }
}
async function loadQuality() {
  el("qualityMessage").textContent = "正在核验本地日线文件…";
  try {
    const data = await requestJSON("/api/data-quality", { key: "data-quality", timeout: 60000 });
    const rows = data.symbols || [];
    const range = data.common_range;
    const usable = data.common_usable_range;
    el("qualityMessage").textContent = range ? `全部标的首尾重合范围：${range.start} → ${range.end}；其中共同缺失 ${range.missing_days || 0} 日。${usable ? `最长共同连续可用区间：${usable.start} → ${usable.end}。` : "没有共同连续可用区间。"}回测按所选标的与日期再次检查。` : "当前标的没有共同覆盖区间，选择标的后检查各自范围。";
    stats(el("qualityStats"), [["本地标的", formatNumber(rows.length)], ["共同开始", range?.start || "—"], ["共同结束", range?.end || "—"], ["各标的缺失日合计", formatNumber(data.summary?.missing_days)], ["无效行合计", formatNumber(data.summary?.invalid_rows)], ["共同连续日数", formatNumber(usable?.days)]]);
    table(el("qualityTable"), [
      { key: "symbol", label: "交易对" }, { key: "start", label: "开始日期" }, { key: "end", label: "结束日期" },
      { key: "rows", label: "总行数", format: formatNumber }, { key: "valid_rows", label: "有效行", format: formatNumber },
      { key: "missing_days", label: "缺失日", format: formatNumber }, { key: "duplicate_timestamps", label: "重复时间", format: formatNumber },
      { key: "out_of_order", label: "乱序行", format: formatNumber }, { key: "invalid_rows", label: "无效行", format: formatNumber },
      { key: "error", label: "读取状态", format: (value, row) => value || (row.available ? "可读取" : "不可用") },
    ], rows);
    methodology(el("qualityMethodology"), data.methodology);
  } catch (error) {
    if (error.name !== "AbortError") { el("qualityMessage").textContent = `数据检查失败：${error.message}`; el("qualityTable").replaceChildren(); }
  }
}
export function initMarket() {
  el("indicatorStudy").addEventListener("change", draw);
  el("qualityRefresh").addEventListener("click", loadQuality);
  document.addEventListener("dashboard:market-loaded", ({ detail }) => loadIndicators(detail.symbol, detail.limit));
  document.addEventListener("dashboard:market-loading", () => { ++generation; marketPayload = null; el("indicatorChart").replaceChildren(); el("indicatorStats").replaceChildren(); el("indicatorAllStats").replaceChildren(); el("indicatorContext").textContent = "正在切换行情…"; });
  document.addEventListener("dashboard:market-error", () => { ++generation; marketPayload = null; el("indicatorChart").replaceChildren(); el("indicatorStats").replaceChildren(); el("indicatorAllStats").replaceChildren(); el("indicatorContext").textContent = "行情读取失败，技术指标暂不可用。请刷新行情重试。"; });
  document.addEventListener("dashboard:view", ({ detail }) => { if (detail.view === "data") loadQuality(); if (detail.view === "market" && lastSymbol) draw(); });
  document.addEventListener("dashboard:refresh", () => { if (document.documentElement.dataset.view === "data") loadQuality(); });
  let frame;
  window.addEventListener("resize", () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { if (document.documentElement.dataset.view === "market") draw(); }); });
  if (document.documentElement.dataset.view === "data") loadQuality();
  if (document.documentElement.dataset.view === "market") loadIndicators();
}
