import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

// This seam exercises real table/stat rendering and diagnostic lifecycle without
// a browser dependency. Native details layout is covered by the browser smoke.
class Element {
  constructor(tag = "div", className = "", text) {
    this.tagName = tag; this.className = className; this.children = [];
    this.attributes = {}; this.dataset = {}; this.listeners = new Map();
    this.open = false; this.disabled = false; this.value = "";
    if (text !== undefined) this.textContent = text;
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  emit(type, detail) { for (const listener of this.listeners.get(type) || []) listener({ type, detail }); }
  dispatchEvent(event) { this.emit(event.type, event.detail); }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const byClass = (node, cls) => descendants(node).filter((child) => child.className.split(" ").includes(cls));
const byTag = (node, tag) => descendants(node).filter((child) => child.tagName === tag);
const flush = async () => { for (let index = 0; index < 6; index++) await Promise.resolve(); };
function open(details) { details.open = true; details.emit("toggle"); }
async function harness() {
  const elements = new Map(), requests = [];
  const el = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const document = new Element("document");
  document.documentElement = new Element("html");
  document.documentElement.dataset.view = "backtest";
  const context = vm.createContext({
    document, window: new Element("window"), location: { hash: "#backtest" },
    element: (tag, cls = "", text) => new Element(tag, cls, text), el,
    requestJSON: (path) => new Promise((resolve, reject) => requests.push({ path, resolve, reject })),
    mutate: async () => ({}), formatNumber: (value) => Number.isFinite(value) ? String(value) : "—",
    CustomEvent: class { constructor(type, { detail } = {}) { this.type = type; this.detail = detail; } },
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
  });
  for (const file of ["web_charts.js", "web_lab.js"]) {
    const source = (await readFile(new URL(`../dashboard/${file}`, import.meta.url), "utf8"))
      .replace(/^import .*;\r?$/gm, "").replaceAll("export function", "function");
    vm.runInContext(source, context, { filename: file });
  }
  context.initLab();
  return { context, el, document, requests, load: (id) => document.emit("dashboard:backtest-loaded", { id }) };
}
function diagnostic() {
  return {
    stats: {
      closed_trades: 3, win_rate: 1 / 3, profit_factor: 1.345678, expectancy: 5.123456,
      avg_win: 20, avg_loss: -7, net_pnl: 15.370368, commission: 2.789012, slippage: 1.11111,
      mean_holding_hours: 36.5, mean_mae: 4.12345, mean_mfe: 11.76543,
      max_drawdown_days: 5.5, underwater_ratio: .5, sharpe_ratio: null,
      sortino_ratio: null, calmar_ratio: null, annualized_volatility: null,
      fee_return_ratio: .1, time_in_market_ratio: .6, mean_gross_leverage: .5,
      max_gross_leverage: 1.23456, turnover_ratio: 2.12345,
    },
    metric_status: { sharpe_ratio: { status: "insufficient", sample_size: 2, source: "metrics.json:SharpeRatio", reason: "insufficient_observations" } },
    no_trade: { status: "unknown", blockers: [{ reason: "策略健康明确禁止新开仓", source: "StrategyHealth" }] },
    funnel: { stages: [{ id: "data", label: "数据", status: "unknown", count: null }, { id: "execution", label: "执行", status: "recorded", count: 0, source: "metrics.json:Diagnostics.entry_causal_chain_audit.filled_entry_chains" }] },
    exit_reasons: [{ reason: "stop_loss", count: 1, net_pnl: -7.123456 }],
    drawdowns: [{ peak: "2026-01-01T00:00:00", trough: "2026-01-03T00:00:00", recovery: null, depth_pct: -.123456, depth_amount: 123.456, duration_days: 5.5, duration_periods: 6, recovery_days: null, is_open: true }],
    concentration: { profit_hhi: .123456 },
    warnings: ["不足样本仅作描述"], methodology: { pnl: "saved net_pnl" },
    closed_trades: { available: true, total: 26, pages: 2, page: 1,
      columns: ["position_id", "net_pnl", "entry_time", "holding_hours"],
      rows: [{ position_id: "000001", net_pnl: 1.23456789, entry_time: "2026-01-01T08:00:00+08:00", holding_hours: 36.5 }],
    },
  };
}

test("shared tables are keyboard-scrollable with semantic numeric cells and exact original values", async () => {
  const h = await harness(), container = new Element();
  h.context.table(container, [{ key: "position_id", label: "标识" }, { key: "net_pnl", label: "净盈亏" }, { key: "holding_hours", label: "持有时间" }], [{ position_id: "000001", net_pnl: 12.3456789, holding_hours: null }]);
  const scroll = container.children[0], headers = byTag(container, "th"), cells = byTag(container, "td");
  assert.equal(scroll.tabIndex, 0); assert.equal(scroll.attributes.role, "region");
  assert.match(scroll.attributes["aria-label"], /标识.*可横向滚动/);
  assert.equal(headers[1].attributes.scope, "col");
  assert.equal(cells[0].textContent, "000001"); assert.equal(cells[0].className, "cell-text");
  assert.equal(cells[1].textContent, "12.35"); assert.match(cells[1].className, /cell-number/);
  assert.equal(cells[1].title, "原始值：12.3456789");
  assert.equal(cells[2].textContent, "—"); assert.match(cells[2].className, /cell-empty/);
  assert.equal(cells[2].attributes["aria-label"], "未记录或不适用");
  h.context.table(container, [{ key: "fill_price", csv: true }], [{ fill_price: "000012.3456789" }]);
  const rawCsv = byTag(container, "td")[0];
  assert.equal(rawCsv.textContent, "12.3457");
  assert.equal(rawCsv.title, "原始值：000012.3456789", "formatting preserves the complete original CSV value in title");
});

test("diagnostics prioritize six metrics and lazily retain all seventeen detailed metrics and provenance", async () => {
  const h = await harness(); h.load("report_a"); h.requests.shift().resolve(diagnostic()); await flush();
  const root = h.el("diagnosticStats");
  assert.equal(byClass(root, "analysis-stat").length, 6);
  const advanced = byClass(root, "diagnostic-advanced")[0];
  assert.equal(advanced.children.length, 1, "closed details must not build every metric and evidence table");
  assert.match(advanced.textContent, /17 项/);
  const sharpe = byClass(root, "analysis-stat").find((node) => node.dataset.metric === "sharpe_ratio");
  assert.match(sharpe.className, /is-unavailable/); assert.match(sharpe.textContent, /样本不足.*2 个样本/);
  assert.match(h.el("noTradeReason").textContent, /禁止新开仓/);
  const source = byClass(h.el("diagnosticFunnel"), "funnel-evidence")[0];
  assert.equal(source.children.length, 1); open(source); assert.match(source.textContent, /filled_entry_chains/);
  open(advanced);
  assert.equal(byClass(root, "analysis-stat").length, 23);
  assert.equal(byClass(root, "diagnostic-group").length, 3);
  assert.match(root.textContent, /1 天 12 小时 30 分钟/);
  const evidence = byClass(root, "diagnostic-disclosure")[0]; open(evidence);
  assert.match(evidence.textContent, /metrics.json:SharpeRatio/); assert.match(evidence.textContent, /insufficient_observations/);
  const net = byClass(root, "analysis-stat").find((node) => node.dataset.metric === "net_pnl");
  assert.equal(byClass(net, "stat-value")[0].title, "原始值：15.370368");
  const cells = byTag(h.el("closedTrades"), "td");
  assert.ok(cells.some((cell) => cell.textContent === "000001"));
  assert.ok(cells.some((cell) => cell.textContent === "2026-01-01 00:00 UTC"));
});

test("pagination preserves disclosures, while a different report resets them and stale responses stay ignored", async () => {
  const h = await harness(); h.load("report_a"); h.requests.shift().resolve(diagnostic()); await flush();
  open(byClass(h.el("diagnosticStats"), "diagnostic-advanced")[0]);
  h.el("closedNext").emit("click");
  const next = h.requests.shift(); assert.match(next.path, /page=2/);
  const page = diagnostic(); page.closed_trades.page = 2; next.resolve(page); await flush();
  assert.equal(byClass(h.el("diagnosticStats"), "diagnostic-advanced")[0].open, true);
  h.load("old_request"); const old = h.requests.shift();
  h.load("new_request"); const current = h.requests.shift(); current.resolve(diagnostic()); await flush();
  assert.equal(byClass(h.el("diagnosticStats"), "diagnostic-advanced")[0].open, false);
  const stale = diagnostic(); stale.stats.closed_trades = 999; old.resolve(stale); await flush();
  assert.match(h.el("diagnosticContext").textContent, /^new_request/);
  const count = byClass(h.el("diagnosticStats"), "analysis-stat").find((node) => node.dataset.metric === "closed_trades");
  assert.equal(byClass(count, "stat-value")[0].textContent, "3");
});

test("drawdown disclosure exposes named timestamps, percentage, duration and recovery state", async () => {
  const h = await harness(); h.load("report_a"); h.requests.shift().resolve(diagnostic()); await flush();
  const details = byClass(h.el("diagnosticDetails"), "diagnostic-disclosure").find((node) => node.textContent.includes("回撤事件"));
  assert.equal(byTag(details, "table").length, 0); open(details);
  const cells = byTag(details, "td");
  assert.ok(cells.some((cell) => cell.textContent === "-12.35%"));
  assert.ok(cells.some((cell) => cell.textContent === "5 天 12 小时"));
  assert.ok(cells.some((cell) => cell.textContent === "尚未恢复"));
  assert.equal(cells[2].textContent, "—");
});

test("legacy stat tuples and custom column formatters remain compatible", async () => {
  const h = await harness(), container = new Element();
  h.context.stats(container, [["已格式化", "+10.00%", "原有说明"], ["缺失", null]]);
  assert.equal(byClass(container, "stat-value")[0].textContent, "+10.00%");
  assert.equal(byClass(container, "stat-value")[1].textContent, "—");
  h.context.table(container, [{ key: "error", format: (value, row) => value || (row.available ? "可读取" : "不可用") }], [{ error: null, available: true }]);
  assert.equal(byTag(container, "td")[0].textContent, "可读取");
});
