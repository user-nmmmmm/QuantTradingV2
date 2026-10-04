/** Small, responsive SVG plots. No remote runtime; null values break paths. */
import { element, formatNumber } from "./api.js";

const NS = "http://www.w3.org/2000/svg";
const COLORS = ["#13877f", "#b66c35", "#7562b4", "#367ab9"];
const numberFormats = new Map();
const numericTypes = new Set(["number", "money", "price", "quantity", "count", "ratio", "percent", "hours", "days"]);
const fieldTypes = Object.fromEntries(Object.entries({
  money: ["net_pnl", "pnl", "gross_pnl", "gross_pnl_theoretical", "commission", "slippage", "fee", "slip", "initial_risk", "expectancy", "avg_win", "avg_loss", "end_equity", "capital", "depth_amount"],
  price: ["entry_price", "exit_price", "fill_price", "price"], quantity: ["qty", "quantity"],
  count: ["count", "closed_trades", "fills_count", "legs", "rows", "valid_rows", "missing_days", "duplicate_timestamps", "out_of_order", "invalid_rows", "duration_periods", "sample_size"],
  percent: ["win_rate", "underwater_ratio", "annualized_volatility", "fee_return_ratio", "time_in_market_ratio", "total_return", "annualized_return", "max_drawdown", "depth_pct", "spread_slippage_rate"],
  ratio: ["profit_factor", "sharpe_ratio", "sortino_ratio", "calmar_ratio", "mean_gross_leverage", "max_gross_leverage", "turnover_ratio", "profit_hhi"],
  number: ["mae", "mfe", "mean_mae", "mean_mfe", "slippage_bps", "spread_bps"],
  hours: ["holding_hours", "mean_holding_hours"], days: ["duration_days", "recovery_days", "max_drawdown_days"],
  time: ["timestamp", "entry_time", "exit_time", "fill_time", "peak", "trough", "recovery", "start", "end", "created_at", "finished_at"],
}).flatMap(([type, keys]) => keys.map((key) => [key, type])));

// Formatting is schema-driven: identifiers and numeric-looking text stay exact.
export function valueType(key) { return fieldTypes[key] || "text"; }
function decimal(value, minimum = 0, maximum = 2) {
  if (Object.is(value, -0)) value = 0;
  if (value !== 0 && Math.abs(value) < 10 ** -maximum) return value.toExponential(2);
  const key = `${minimum}:${maximum}`;
  if (!numberFormats.has(key)) numberFormats.set(key, new Intl.NumberFormat("zh-CN", { minimumFractionDigits: minimum, maximumFractionDigits: maximum }));
  return numberFormats.get(key).format(value);
}
export function formatValue(value, { type = "text", signed = false } = {}) {
  if (value == null || value === "" || (typeof value === "number" && !Number.isFinite(value))) return "—";
  if (type === "text") return typeof value === "object" ? JSON.stringify(value) : String(value);
  if (type === "boolean") return value === true ? "是" : value === false ? "否" : "—";
  if (type === "time") {
    const time = chartTime(value);
    if (!Number.isFinite(time)) return "—";
    const iso = new Date(time).toISOString();
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return iso.slice(0, 10);
    return `${iso.slice(0, 10)} ${iso.slice(11, iso.slice(17, 19) === "00" ? 16 : 19)} UTC`;
  }
  if (!Number.isFinite(value)) return "—";
  if (type === "hours" || type === "days") {
    const hours = type === "days" ? value * 24 : value;
    if (!Number.isFinite(hours) || hours < 0) return "—";
    if (hours === 0) return "0 小时";
    const minutes = Math.round(hours * 60);
    if (minutes === 0) return "< 1 分钟";
    if (!Number.isSafeInteger(minutes)) return `${decimal(value)} ${type === "days" ? "天" : "小时"}`;
    const days = Math.floor(minutes / 1440), rest = minutes % 1440;
    return [days ? `${decimal(days, 0, 0)} 天` : "", rest >= 60 ? `${Math.floor(rest / 60)} 小时` : "", rest % 60 ? `${rest % 60} 分钟` : ""].filter(Boolean).join(" ");
  }
  const prefix = signed && value > 0 ? "+" : "";
  if (type === "percent") return Number.isFinite(value * 100) ? `${prefix}${decimal(value * 100, 2, Math.abs(value * 100) < .01 ? 6 : 2)}%` : "—";
  if (type === "count") return decimal(value, 0, Number.isInteger(value) ? 0 : 2);
  if (type === "price") return `${prefix}${decimal(value, 2, Math.abs(value) >= 100 ? 2 : Math.abs(value) >= 1 ? 4 : 8)}`;
  if (type === "money") return `${prefix}${decimal(value, 2, value !== 0 && Math.abs(value) < .01 ? 8 : 2)}`;
  if (type === "quantity") return `${prefix}${decimal(value, 0, 8)}`;
  if (type === "ratio") return `${prefix}${decimal(value, 2, 2)}`;
  return `${prefix}${decimal(value, 0, 4)}`;
}
export function cellClass(column = {}, unavailable = false) {
  const type = typeof column === "string" ? column : typeof column.type === "function" ? "number" : column.type || valueType(column.key);
  return [numericTypes.has(type) ? "cell-number" : "", `cell-${["hours", "days"].includes(type) ? "duration" : type}`, unavailable ? "cell-empty" : ""].filter(Boolean).join(" ");
}
export function formatCell(value, column = {}, row = {}) {
  const type = typeof column.type === "function" ? column.type(row) : column.type || valueType(column.key);
  // Raw fills intentionally preserve CSV text. Only opted-in, schema-known
  // numeric cells may parse decimal notation; IDs and arbitrary text never do.
  if (column.csv && numericTypes.has(type) && typeof value === "string" && /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())) {
    const number = Number(value);
    if (Number.isFinite(number)) value = number;
  }
  return column.format ? column.format(value, row) : formatValue(value, { type, signed: column.signed });
}
function rawTitle(value, type) {
  if (value == null || value === "") return "";
  return `原始值：${typeof value === "object" ? JSON.stringify(value) : String(value)}${type === "time" ? "；显示时间为 UTC" : type === "hours" ? " 小时" : type === "days" ? " 天" : ""}`;
}
export function chartTime(value) {
  if (typeof value !== "string") return NaN;
  const normalized = value.trim().replace(" ", "T");
  // Report timestamps without an offset follow the engine's UTC convention.
  // Treating them as browser-local time shifts daily labels in UTC+8.
  const hasTime = normalized.includes("T");
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
  return Date.parse(hasTime && !hasOffset ? `${normalized}Z` : normalized);
}
function node(tag, attrs = {}, text) {
  const item = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([key, value]) => item.setAttribute(key, String(value)));
  if (text !== undefined) item.textContent = text;
  return item;
}
export function lineChart(container, series, { percent = false, label = "指标曲线", baseline } = {}) {
  const width = Math.max(280, container.clientWidth || 800), height = 250;
  const left = 58, right = width - 14, top = 18, bottom = height - 32;
  const valid = series.flatMap((line) => line.points.filter((p) => Number.isFinite(p.value) && Number.isFinite(chartTime(p.timestamp))));
  container.replaceChildren();
  if (!valid.length) { container.append(element("p", "panel-empty", "暂无足够的有效数据")); return; }
  const times = valid.map((p) => chartTime(p.timestamp));
  const values = valid.map((p) => p.value);
  const minT = Math.min(...times), maxT = Math.max(...times);
  let low = Math.min(...values, ...(baseline === undefined ? [] : [baseline]));
  let high = Math.max(...values, ...(baseline === undefined ? [] : [baseline]));
  const pad = Math.max((high - low) * .08, Math.abs(high) * .001, .001);
  low -= pad; high += pad;
  const x = (time) => left + (chartTime(time) - minT) / (maxT - minT || 1) * (right - left);
  const y = (value) => bottom - (value - low) / (high - low) * (bottom - top);
  const svg = node("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label, class: "research-plot" });
  const fmt = (v) => percent ? `${(v * 100).toFixed(1)}%` : formatNumber(v);
  for (let i = 0; i <= 4; i++) {
    const value = low + (high - low) * i / 4;
    svg.append(node("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "plot-grid" }),
      node("text", { x: left - 7, y: y(value) + 4, "text-anchor": "end", class: "plot-label" }, fmt(value)));
  }
  [minT, maxT].forEach((time, i) => svg.append(node("text", { x: i ? right : left, y: height - 7, "text-anchor": i ? "end" : "start", class: "plot-label" }, new Date(time).toISOString().slice(0, 10))));
  const legend = element("div", "plot-legend");
  series.forEach((line, index) => {
    let drawing = false, path = "";
    line.points.forEach((point) => {
      if (!Number.isFinite(point.value) || !Number.isFinite(chartTime(point.timestamp))) { drawing = false; return; }
      path += `${drawing ? "L" : "M"}${x(point.timestamp).toFixed(2)},${y(point.value).toFixed(2)} `;
      drawing = true;
    });
    const color = COLORS[index % COLORS.length];
    svg.append(node("path", { d: path, fill: "none", stroke: color, class: `line-series-${index % 4}`, "stroke-width": 1.8, "vector-effect": "non-scaling-stroke" }));
    const item = element("span", `series-${index % 4}`, line.name); legend.append(item);
  });
  container.append(svg, legend);
}

export function table(container, columns, rows) {
  const scroll = element("div", "table-scroll");
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", `数据表：${columns.slice(0, 3).map((column) => column.label || column.key).join("、") || "暂无记录"}；可横向滚动`);
  const grid = element("table", "research-table"), head = element("thead"), tr = element("tr");
  columns.forEach((column) => {
    const type = typeof column.type === "function" ? "number" : column.type || valueType(column.key);
    const th = element("th", cellClass(type), column.label || column.key);
    th.setAttribute("scope", "col"); tr.append(th);
  });
  head.append(tr);
  const body = element("tbody");
  rows.forEach((row) => {
    const item = element("tr");
    columns.forEach((column) => {
      const value = row[column.key];
      const type = typeof column.type === "function" ? column.type(row) : column.type || valueType(column.key);
      const text = formatCell(value, column, row);
      const td = element("td", cellClass(type, text == null || text === "—"), text ?? "—");
      td.title = rawTitle(value, type);
      if (text == null || text === "—") td.setAttribute("aria-label", "未记录或不适用");
      item.append(td);
    });
    body.append(item);
  });
  if (!rows.length) { const item = element("tr"), td = element("td", "empty-cell", "暂无记录"); td.colSpan = Math.max(1, columns.length); item.append(td); body.append(item); }
  grid.append(head, body); scroll.append(grid); container.replaceChildren(scroll);
}

export function stats(container, entries) {
  container.replaceChildren(...entries.map(([label, value, note, options = {}]) => {
    const text = options.type ? formatValue(value, options) : value ?? "—";
    const unavailable = text === "—";
    const item = element("div", `analysis-stat${unavailable ? " is-unavailable" : ""}`);
    if (options.key) item.dataset.metric = options.key;
    const strong = element("strong", "stat-value", text);
    strong.title = rawTitle(value, options.type);
    if (unavailable) strong.setAttribute("aria-label", "未记录或不适用");
    item.append(element("span", "stat-label", label), strong);
    if (options.unit && !unavailable) item.append(element("span", "stat-unit", options.unit));
    if (note) item.append(element("small", "stat-note", note));
    return item;
  }));
}
