import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";
import { requestJSON, formatNumber, formatPercent } from "../dashboard/web_api.js";

// Asset URLs differ from filesystem module names. Reuse the real pure formatters
// without requiring the HTTP asset router in the dependency-free VM harness.
const formatterContext = vm.createContext({});
const formatterSource = await readFile(new URL("../dashboard/web_charts.js", import.meta.url), "utf8");
vm.runInContext(formatterSource.replace(/^import .*;$/gm, "").replaceAll("export function", "function"), formatterContext);
const { formatCell, cellClass } = formatterContext;

// A dependency-free DOM seam for the dashboard's async lifecycle. Browser layout
// and assistive technology behavior still require the visual smoke checks.
class EventTarget {
  listeners = new Map();
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatchEvent(event) {
    event.currentTarget = this;
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }
}

class Element extends EventTarget {
  constructor(tag = "div") {
    super();
    Object.assign(this, {
      tagName: tag, children: [], textContent: "", dataset: {}, style: {}, attributes: {},
      hidden: false, checked: false, disabled: false, replaceCount: 0, _value: "",
      viewBox: { baseVal: { width: 1100 } },
      classList: { toggle() {}, add() {}, remove() {} },
    });
  }
  get options() { return this.children; }
  get textContent() { return this._textContent || ""; }
  set textContent(value) { this._textContent = String(value); this.textWrites = (this.textWrites || 0) + 1; this.children = []; }
  get value() { return this.tagName === "select" ? this._value || this.children[0]?.value || "" : this._value; }
  set value(value) { this._value = value; }
  append(...children) {
    const nodes = children.flatMap((child) => child.tagName === "#fragment" ? child.children : [child]);
    nodes.forEach((child) => { child.parentElement = this; });
    this.children.push(...nodes);
  }
  replaceChildren(...children) {
    if (this.ownerDocument?.activeElement !== this && this.contains(this.ownerDocument?.activeElement)) this.ownerDocument.activeElement = null;
    this.children = [];
    this.append(...children);
    this.replaceCount += 1;
    if (this.tagName === "select") this._value = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    return selector === "button[data-job-id]" ? descendants.filter((child) => child.tagName === "button" && child.dataset.jobId !== undefined) : descendants;
  }
  focus(options) {
    this.focused = true; this.focusOptions = options;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  getBoundingClientRect() {
    this.layoutReads = (this.layoutReads || 0) + 1;
    return this.bounds || { left: 0, width: 1100 };
  }
}

async function dashboard(source, view = "overview") {
  const elements = new Map();
  const document = new EventTarget();
  document.documentElement = new Element("html");
  document.documentElement.dataset.view = view;
  document.hidden = false;
  document.createElement = (tag) => Object.assign(new Element(tag), { ownerDocument: document });
  document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, document.createElement(["marketSymbol", "backtestRun"].includes(id) ? "select" : "div"));
    return elements.get(id);
  };
  const sections = ["overview", "backtest", "market", "operations"].map((name) => {
    const section = new Element("section");
    section.dataset.view = name;
    return section;
  });
  const routes = sections.map((section) => {
    const route = new Element("a");
    route.dataset.route = section.dataset.view;
    return route;
  });
  const candleRangeButtons = [60, 120, 240].map((limit) => {
    const button = new Element("button");
    button.dataset.limit = String(limit);
    return button;
  });
  const alertFilterButtons = ["all", "high", "other"].map((filter) => {
    const button = new Element("button");
    button.dataset.filter = filter;
    return button;
  });
  document.querySelectorAll = (selector) => selector === ".workspace-view[data-view]" ? sections
    : selector === "[data-view]" ? [document.documentElement, ...sections]
      : selector === "[data-route]" ? routes
        : selector === ".range-buttons button[data-limit]" ? candleRangeButtons
          : selector === ".alert-filters button" ? alertFilterButtons : [];
  document.createElementNS = (_, tag) => document.createElement(tag);
  document.createDocumentFragment = () => document.createElement("#fragment");
  document.createTextNode = (text) => Object.assign(new Element("#text"), { textContent: text });
  const requests = [];
  const frames = new Map();
  const intervals = [];
  let frameId = 0;
  let time = 0;
  const window = Object.assign(new EventTarget(), { innerWidth: 1440 });
  const location = { hash: `#${view}` };
  const context = {
    document, window, location, Intl, Date, Math, Number, String, Array, Object, Set, Map, formatCell, cellClass, formatNumber, formatPercent,
    el: (id) => document.getElementById(id),
    element: (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    },
    CustomEvent: class { constructor(type, { detail } = {}) { this.type = type; this.detail = detail; } },
    requestJSON: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
    getComputedStyle: () => ({ getPropertyValue: () => "#086e67" }),
    requestAnimationFrame: (callback) => { frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame: (id) => frames.delete(id),
    setInterval: (callback) => intervals.push(callback),
    performance: { now: () => time },
  };
  vm.createContext(context);
  const code = await readFile(new URL(`../dashboard/${source}`, import.meta.url), "utf8");
  vm.runInContext(code.replace(/^import .*;$/gm, "").replace(/^export /gm, ""), context, { filename: source });
  return {
    document, elements, requests, frames, window, location, sections, routes, candleRangeButtons, alertFilterButtons, context,
    el: (id) => document.getElementById(id),
    emit: (type, detail) => document.dispatchEvent({ type, detail }),
    tick: (elapsed = 1000) => { time += elapsed; intervals.forEach((callback) => callback()); },
    next(fragment) {
      const request = requests.shift();
      assert.ok(request, `expected request for ${fragment}`);
      assert.ok(request.path.includes(fragment), request.path);
      assert.ok(request.options.key, "request must have a cancellation key");
      assert.ok(request.options.timeout <= 20000, "request must be bounded");
      return request;
    },
  };
}

async function flush() { for (let count = 0; count < 8; count += 1) await Promise.resolve(); }
function report(id) {
  return {
    id, period_start: "2026-01-01", period_end: "2026-01-03",
    metrics: { total_return: .1, max_drawdown: -.1, end_equity: 110, fills_count: 1 },
    points: [
      { timestamp: "2026-01-01", equity: 100, drawdown: 0 },
      { timestamp: "2026-01-02", equity: 90, drawdown: -.1 },
      { timestamp: "2026-01-03", equity: 110, drawdown: 0 },
    ],
  };
}
async function resolveReport(harness, id = "new_run") {
  harness.next("/api/backtests").resolve({ runs: [{ id }, { id: "older_run" }] });
  await flush();
  harness.next(`id=${id}`).resolve(report(id));
  await flush();
}

test("charts load lazily; an initially empty directory receives completed runs without duplicate handlers", async () => {
  const h = await dashboard("web_visual.js");
  assert.equal(h.requests.length, 0, "overview must not fetch chart APIs");
  h.document.documentElement.dataset.view = "backtest";
  h.emit("dashboard:view", { view: "backtest" });
  h.next("/api/backtests").resolve({ runs: [] });
  await flush();
  assert.equal(h.requests.length, 0);
  assert.equal(h.el("backtestEmpty").hidden, false);
  h.emit("dashboard:backtest-complete", { run_id: "new_run" });
  await resolveReport(h);
  assert.equal(h.el("backtestRun").value, "new_run");
  assert.equal(h.el("backtestEmpty").hidden, true);
  for (let count = 0; count < 2; count += 1) {
    h.emit("dashboard:refresh");
    await resolveReport(h);
  }
  assert.equal(h.el("backtestRun").listeners.get("change").length, 1);
  assert.equal(h.el("backtestChart").listeners.get("pointermove").length, 1);
  let replay = 0;
  h.document.addEventListener("dashboard:backtest-loaded", () => { replay += 1; });
  h.emit("dashboard:backtest-current");
  assert.equal(replay, 1, "late analysis subscribers can recover the loaded result");
});

test("superseded result responses are ignored; failure clears chart and cache and navigation can retry", async () => {
  const h = await dashboard("web_visual.js", "backtest");
  await resolveReport(h);
  const select = h.el("backtestRun");
  select.value = "older_run";
  select.dispatchEvent({ type: "change" });
  const older = h.next("id=older_run");
  select.value = "new_run";
  select.dispatchEvent({ type: "change" });
  const newer = h.next("id=new_run");
  newer.resolve(report("new_run"));
  await flush();
  older.resolve(report("older_run"));
  await flush();
  assert.ok(h.el("backtestStatus").textContent.startsWith("new_run"));
  select.dispatchEvent({ type: "change" });
  h.next("/api/backtest?").reject(new Error("offline"));
  await flush();
  assert.equal(h.el("backtestChart").children.length, 0);
  assert.equal(h.el("backtestReturn").textContent, "—");
  h.emit("dashboard:theme");
  assert.equal(h.el("backtestChart").children.length, 0, "theme changes must not revive old results");
  h.emit("dashboard:view", { view: "backtest" });
  await resolveReport(h);
  assert.equal(h.el("backtestEmpty").hidden, true);
});

test("an archived report outside the recent catalog can be opened directly", async () => {
  const h = await dashboard("web_visual.js", "backtest");
  await resolveReport(h);
  h.emit("dashboard:backtest-complete", { run_id: "archived_run" });
  h.next("/api/backtests").resolve({ runs: [{ id: "new_run" }] });
  await flush();
  h.next("id=archived_run").resolve(report("archived_run"));
  await flush();
  assert.equal(h.el("backtestRun").value, "archived_run");
  assert.equal(h.el("backtestEmpty").hidden, true);
  assert.equal(h.requests.length, 0, "validated archive payload is reused without a duplicate fetch");
});

test("chart pointer updates are batched; keyboard navigation exposes individual observations", async () => {
  const h = await dashboard("web_visual.js", "backtest");
  await resolveReport(h);
  const chart = h.el("backtestChart");
  for (let index = 0; index < 20; index += 1) chart.dispatchEvent({ type: "pointermove", clientX: 70 + index });
  assert.equal(h.frames.size, 1);
  for (const callback of h.frames.values()) callback();
  h.frames.clear();
  chart.dispatchEvent({ type: "keydown", key: "End", preventDefault() {} });
  assert.ok(h.el("backtestHover").textContent.includes("2026-01-03"));
  chart.dispatchEvent({ type: "keydown", key: "ArrowLeft", preventDefault() {} });
  assert.ok(h.el("backtestHover").textContent.includes("2026-01-02"));
  assert.equal(chart.attributes.tabindex, "0");
});

test("flat 20,000-point reports have bounded SVG paths while retaining full result data", async () => {
  const h = await dashboard("web_visual.js", "backtest");
  h.next("/api/backtests").resolve({ runs: [{ id: "flat" }] });
  await flush();
  const payload = report("flat");
  payload.metrics.max_drawdown = 0;
  payload.points = Array.from({ length: 20000 }, (_, index) => ({ timestamp: String(index), equity: 100, drawdown: 0 }));
  let received;
  h.document.addEventListener("dashboard:backtest-loaded", (event) => { received = event.detail; });
  h.next("id=flat").resolve(payload);
  await flush();
  const paths = h.el("backtestChart").children.filter((child) => child.tagName === "path");
  assert.ok(paths.length > 0);
  for (const path of paths) assert.ok((path.attributes.d.match(/[ML]/g) || []).length < 860);
  assert.equal(received.points.length, 20000, "sampling must not truncate analysis data");
});

test("market fetch failures clear candle observations and do not restore cached charts on theme changes", async () => {
  const h = await dashboard("web_visual.js", "market");
  h.next("/api/markets").resolve({ markets: ["BTCUSDT"] });
  await flush();
  h.next("/api/candles").resolve({
    symbol: "BTCUSDT", last_candle: "2026-01-02", age_days: 1,
    candles: [
      { timestamp: "2026-01-01", open: 100, high: 110, low: 95, close: 105, volume: 100 },
      { timestamp: "2026-01-02", open: 105, high: 112, low: 102, close: 109, volume: 80 },
    ],
  });
  await flush();
  assert.equal(h.el("candleEmpty").hidden, true);
  h.el("marketSymbol").dispatchEvent({ type: "change" });
  h.next("/api/candles").reject(new Error("offline"));
  await flush();
  assert.equal(h.el("candleChart").children.length, 0);
  assert.equal(h.el("candleClose").textContent, "—");
  h.emit("dashboard:theme");
  assert.equal(h.el("candleChart").children.length, 0);
});

test("candle ranges and alert filters expose exactly one pressed option on initialization and selection", async () => {
  const charts = await dashboard("web_visual.js");
  assert.deepEqual(charts.candleRangeButtons.map((button) => button.attributes["aria-pressed"]), ["false", "true", "false"]);
  charts.candleRangeButtons[0].dispatchEvent({ type: "click" });
  assert.deepEqual(charts.candleRangeButtons.map((button) => button.attributes["aria-pressed"]), ["true", "false", "false"]);
  charts.candleRangeButtons[2].dispatchEvent({ type: "click" });
  assert.deepEqual(charts.candleRangeButtons.map((button) => button.attributes["aria-pressed"]), ["false", "false", "true"]);

  const alerts = await dashboard("web_app.js");
  assert.deepEqual(alerts.alertFilterButtons.map((button) => button.attributes["aria-pressed"]), ["true", "false", "false"]);
  alerts.alertFilterButtons[1].dispatchEvent({ type: "click" });
  assert.deepEqual(alerts.alertFilterButtons.map((button) => button.attributes["aria-pressed"]), ["false", "true", "false"]);
  alerts.alertFilterButtons[2].dispatchEvent({ type: "click" });
  assert.deepEqual(alerts.alertFilterButtons.map((button) => button.attributes["aria-pressed"]), ["false", "false", "true"]);
});

test("job polling preserves unchanged rows and logs, restores button focus after updates, and keeps report actions current", async () => {
  const h = await dashboard("web_research.js");
  const list = h.el("jobList"), log = h.el("jobLog");
  const job = { id: "job-1", status: "running", elapsed_seconds: 10.001, logs: ["started"], parameters: { symbols: ["BTC/USDT"], source: "local", start: "2026-01-01", end: "2026-06-01" } };
  const render = (values) => {
    h.context.testJobs = values;
    vm.runInContext("options = { enabled: true, csrf_token: 'test-token' }; jobs = testJobs; renderJobs();", h.context);
  };
  render([job]);
  const firstButton = list.querySelectorAll("button[data-job-id]")[0];
  firstButton.focus();
  const renders = list.replaceCount, writes = log.textWrites;
  render([{ ...job, elapsed_seconds: 10.004, updated_at: "new metadata" }]);
  assert.equal(list.replaceCount, renders, "unchanged visible fields must preserve the list nodes");
  assert.equal(log.textWrites, writes, "identical logs must preserve text nodes and selection");
  assert.equal(h.document.activeElement, firstButton);
  render([{ ...job, logs: ["started", "progress"] }]);
  assert.equal(list.replaceCount, renders, "log updates must not rebuild task controls");
  assert.equal(log.textContent, "started\nprogress");
  assert.equal(log.textWrites, writes + 1);

  render([{ ...job, elapsed_seconds: 12, logs: ["started", "progress"] }]);
  const replacement = list.querySelectorAll("button[data-job-id]")[0];
  assert.notEqual(replacement, firstButton);
  assert.equal(h.document.activeElement, replacement);
  assert.equal(replacement.focusOptions.preventScroll, true);
  assert.equal(h.el("runBacktest").disabled, true);
  let opened;
  h.document.addEventListener("dashboard:backtest-complete", ({ detail }) => { opened = detail.run_id; });
  render([{ ...job, status: "succeeded", run_id: "report-one" }]);
  assert.equal(h.el("runBacktest").disabled, false);
  let reportButton = list.querySelectorAll("button[data-job-id]")[0];
  assert.equal(h.document.activeElement, reportButton);
  reportButton.dispatchEvent({ type: "click" });
  assert.equal(opened, "report-one");
  render([{ ...job, status: "succeeded", run_id: "report-two" }]);
  reportButton = list.querySelectorAll("button[data-job-id]")[0];
  reportButton.dispatchEvent({ type: "click" });
  assert.equal(opened, "report-two", "the signature must account for changed action targets");
  render([{ ...job, status: "cancelled" }]);
  assert.equal(h.document.activeElement, list, "a removed action leaves focus on the task list");
});

test("a pending cancellation stays disabled across job refreshes and recovers after failure", async () => {
  const h = await dashboard("web_research.js");
  const job = { id: "job-cancel", status: "running", elapsed_seconds: 1 };
  h.context.testJobs = [job];
  vm.runInContext("options = { enabled: true, csrf_token: 'test-token' }; jobs = testJobs; renderJobs();", h.context);
  h.el("jobList").querySelectorAll("button[data-job-id]")[0].dispatchEvent({ type: "click" });
  const cancellation = h.requests.shift();
  assert.equal(cancellation.path, "/api/backtest-jobs/cancel");
  h.context.testJobs = [{ ...job, elapsed_seconds: 2 }];
  vm.runInContext("jobs = testJobs; renderJobs();", h.context);
  const replacement = h.el("jobList").querySelectorAll("button[data-job-id]")[0];
  assert.equal(replacement.disabled, true);
  replacement.dispatchEvent({ type: "click" });
  assert.equal(h.requests.length, 0, "a pending cancellation must not be submitted twice");
  cancellation.reject(new Error("temporarily unavailable"));
  await flush();
  assert.equal(replacement.disabled, false);
  assert.equal(h.el("researchMessage").textContent, "temporarily unavailable");
});

test("mobile chart labels preserve screen glyph size and resize batches only the visible chart", async () => {
  const h = await dashboard("web_visual.js", "backtest");
  const chart = h.el("backtestChart");
  chart.viewBox.baseVal.height = 390;
  chart.bounds = { left: 0, width: 308, height: 250 };
  await resolveReport(h);
  assert.equal(chart.layoutReads, 1, "read SVG layout once for all labels");
  const labels = chart.children.filter((child) => child.tagName === "text");
  const dates = labels.filter((label) => label.textContent.startsWith("2026-"));
  assert.equal(dates.length, 3, "mobile date ticks must not overlap");
  for (const label of labels) {
    const match = label.attributes.transform.match(/^translate\(([^ ]+) ([^)]+)\) scale\(([^ ]+) ([^)]+)\)$/);
    assert.ok(match);
    const [, x, y, scaleX, scaleY] = match.map(Number);
    assert.ok(x > 0 && x < 1100 && y > 0 && y < 390);
    assert.ok(Math.abs(10 * scaleX * 308 / 1100 - 10) < .00001);
    assert.ok(Math.abs(10 * scaleY * 250 / 390 - 10) < .00001);
  }
  const before = chart.replaceCount;
  chart.bounds = { left: 0, width: 700, height: 390 };
  for (let count = 0; count < 15; count += 1) h.window.dispatchEvent({ type: "resize" });
  assert.equal(h.frames.size, 1);
  for (const callback of h.frames.values()) callback();
  h.frames.clear();
  assert.equal(chart.replaceCount, before + 1);
  assert.equal(chart.layoutReads, 2);
  h.document.documentElement.dataset.view = "overview";
  h.window.dispatchEvent({ type: "resize" });
  for (const callback of h.frames.values()) callback();
  h.frames.clear();
  assert.equal(chart.replaceCount, before + 1, "hidden charts are not redrawn");
  h.document.documentElement.dataset.view = "backtest";
  h.emit("dashboard:view", { view: "backtest" });
  assert.equal(chart.replaceCount, before + 2, "deferred sizing refreshes when visible");
  chart.bounds = { left: 0, width: 0, height: 0 };
  h.emit("dashboard:theme");
  const hiddenLabels = chart.children.filter((child) => child.tagName === "text");
  assert.ok(hiddenLabels.every((label) => !/NaN|Infinity/.test(label.attributes.transform)));
});

test("status polling pauses when hidden, preserves unchanged DOM sections, and tracks snapshot expiry", async () => {
  const h = await dashboard("web_app.js");
  const data = {
    mode: "live", status_valid: true, healthy: true, snapshot_age_seconds: 295,
    timestamp: "2026-01-01T00:00:00Z", equity: 100, cash: 100, positions: {},
    details: { account_entry_gate: { allows_new_risk: true } }, recent_alerts: [], history: [],
  };
  h.next("/api/status").resolve(data);
  await flush();
  const renders = h.el("positionsBody").replaceCount;
  h.el("refreshButton").dispatchEvent({ type: "click" });
  h.next("/api/status").resolve({ ...data, server_time: "2026-01-01T00:00:01Z" });
  await flush();
  assert.equal(h.el("positionsBody").replaceCount, renders);
  h.document.hidden = true;
  h.emit("visibilitychange");
  h.tick(20000);
  assert.equal(h.requests.length, 0);
  assert.equal(h.el("countdown").textContent, "已暂停");
  h.document.hidden = false;
  h.emit("visibilitychange");
  h.next("/api/status").resolve(data);
  await flush();
  h.tick(6000);
  assert.equal(h.el("systemState").textContent, "快照过期");
  assert.equal(h.el("gateValue").textContent, "待确认");
});

test("workspace navigation never hides the document and skip links preserve the active view", async () => {
  const h = await dashboard("web_workspace.js", "market");
  assert.equal(h.document.documentElement.dataset.view, "market");
  h.location.hash = "#operations";
  h.window.dispatchEvent({ type: "hashchange" });
  assert.equal(h.document.documentElement.hidden, false);
  assert.equal(h.sections.find((section) => section.dataset.view === "operations").hidden, false);
  assert.equal(h.sections.find((section) => section.dataset.view === "market").hidden, true);
  h.location.hash = "#workspaceTitle";
  h.window.dispatchEvent({ type: "hashchange" });
  assert.equal(h.document.documentElement.dataset.view, "operations");
  assert.equal(h.document.documentElement.hidden, false);
  assert.equal(h.routes.find((route) => route.dataset.route === "operations").attributes["aria-current"], "page");
  assert.equal(h.routes.find((route) => route.dataset.route === "market").attributes["aria-current"], undefined);
});

test("shared API client aborts superseded reads and enforces timeouts", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  });
  try {
    const first = requestJSON("/first", { key: "test-supersession", timeout: 1000 });
    const firstFailure = assert.rejects(first, { name: "AbortError" });
    const second = requestJSON("/second", { key: "test-supersession", timeout: 10 });
    await Promise.all([firstFailure, assert.rejects(second, /请求超时/)]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
