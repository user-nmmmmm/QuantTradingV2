import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

// Async lifecycle checks deliberately allow superseded requests to complete.
// Correctness must not rely on a fetch abort arriving before the response.
class EventTarget {
  listeners = new Map();
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }
}
class Element extends EventTarget {
  constructor(tag) {
    super();
    Object.assign(this, { tagName: tag, children: [], textContent: "", dataset: {}, attributes: {},
      classList: { add() {} }, disabled: false, _value: "" });
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelectorAll(tag) { return nodes(this).slice(1).filter((node) => node.tagName === tag); }
  querySelector(tag) { return this.querySelectorAll(tag)[0] || null; }
  contains(node) { return nodes(this).includes(node); }
  focus() { this.ownerDocument.activeElement = this; }
  reportValidity() { return true; }
  scrollIntoView() {}
}
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function contents(root) { return nodes(root).map((node) => node.textContent).join(" "); }
async function flush() { for (let index = 0; index < 14; index++) await Promise.resolve(); }
function options() {
  return { enabled: true, csrf_token: "test-token", required_symbols: 3, geometry: { warmup_bars: 60 },
    sources: [{ id: "synthetic", label: "合成情景" }], symbols: [], synthetic_symbols: ["BTC/USDT", "ETH/USDT", "BNB/USDT"],
    selection_metrics: [{ id: "TotalReturn", label: "总收益" }], defaults: { source: "synthetic", symbols: ["BTC/USDT", "ETH/USDT", "BNB/USDT"],
      start: "2025-01-01", end: "2025-12-31", capital: 10000, slippage_bps: 5, seed: 42,
      train_bars: 90, validation_bars: 30, test_bars: 30, purge_bars: 5, windows: 3,
      entry_windows: [20, 30, 50], exit_windows: [5, 10], selection_metric: "TotalReturn" } };
}
function report(id) {
  return { id, parameters: options().defaults, data: { effective_start: "2025-02-01", effective_end: "2025-12-31", bars: 335, warmup_bars: 60, step_bars: 60 },
    procedure: { selection_stability: {}, sample_size: 0, total_return: null }, windows: [], curve: [], heatmap: [],
    skipped_windows: [], selection_metric: "TotalReturn", config_sha256: "test-hash" };
}
async function harness() {
  const root = new Element("div"); root.id = "robustContent";
  const document = new EventTarget();
  root.ownerDocument = document;
  document.hidden = false; document.documentElement = { dataset: { view: "robust" } };
  document.getElementById = (id) => nodes(root).find((node) => node.id === id) || null;
  const requests = [], timers = new Map(), plots = [];
  let timerId = 0;
  const context = { document, Intl, Date, Math, Number, String, Array, Object, Set, Map,
    element(tag, className = "", text = "") { const node = new Element(tag); node.className = className; node.textContent = text; node.ownerDocument = document; return node; },
    requestJSON: (path, settings) => new Promise((resolve, reject) => requests.push({ path, settings, resolve, reject })),
    formatNumber: (value) => Number.isFinite(value) ? String(value) : "—",
    formatPercent: (value) => Number.isFinite(value) ? `${value * 100}%` : "—",
    lineChart: (container, series) => { plots.push(series); container.replaceChildren(new Element("svg")); },
    table() {}, stats() {},
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  vm.createContext(context);
  const source = await readFile(new URL("../dashboard/web_robust.js", import.meta.url), "utf8");
  vm.runInContext(source.replace(/^import .*;$/gm, "").replace("export function initRobust", "function initRobust"), context);
  const h = { root, document, requests, timers, plots, init: context.initRobust,
    el: (id) => document.getElementById(id),
    emit: (type, detail) => document.dispatchEvent({ type, detail }),
    next(fragment) { const request = requests.shift(); assert.ok(request, `Missing ${fragment}`); assert.ok(request.path.includes(fragment), request.path); return request; },
  };
  h.start = async () => { const promise = h.init(); h.next("research-options").resolve(options()); await flush(); h.next("research-jobs").resolve({ jobs: [] }); await promise; };
  return h;
}

test("concurrent lazy init and history init share one promise and one set of handlers", async () => {
  const h = await harness();
  const first = h.init(), second = h.init();
  assert.strictEqual(first, second);
  assert.equal(h.requests.length, 1);
  h.next("research-options").resolve(options()); await flush();
  h.next("research-jobs").resolve({ jobs: [] }); await Promise.all([first, second]);
  assert.equal(h.el("robustForm").listeners.get("submit").length, 1);
  assert.equal(h.document.listeners.get("dashboard:research-open").length, 1);
  await h.init(); assert.equal(h.requests.length, 0);
});

test("failed initial options request can retry without duplicate event handlers", async () => {
  const h = await harness();
  const first = h.init(); const rejected = assert.rejects(first, /offline/);
  h.next("research-options").reject(new Error("offline")); await rejected;
  await h.start();
  assert.equal(h.document.listeners.get("dashboard:view").length, 1);
  assert.equal(h.el("robustForm").listeners.get("submit").length, 1);
});

test("out-of-order job refreshes retain the newest list and one polling timer", async () => {
  const h = await harness(); await h.start();
  h.emit("dashboard:view", { view: "robust" }); const older = h.next("research-jobs");
  h.emit("dashboard:view", { view: "robust" }); const newer = h.next("research-jobs");
  newer.resolve({ jobs: [{ id: "newest-job", status: "running", parameters: options().defaults }] }); await flush();
  older.resolve({ jobs: [{ id: "stale-job", status: "succeeded", parameters: options().defaults }] }); await flush();
  assert.match(contents(h.root), /newest-job/); assert.doesNotMatch(contents(h.root), /stale-job/);
  assert.equal(h.timers.size, 1); assert.equal([...h.timers.values()][0].delay, 2500);
});

test("leaving or hiding the research view invalidates pending refreshes and prevents polling revival", async () => {
  const h = await harness(); await h.start();
  h.emit("dashboard:view", { view: "robust" }); const leaving = h.next("research-jobs");
  h.document.documentElement.dataset.view = "overview"; h.emit("dashboard:view", { view: "overview" });
  leaving.resolve({ jobs: [{ id: "late-hidden-job", status: "running", parameters: options().defaults }] }); await flush();
  assert.equal(h.timers.size, 0); assert.doesNotMatch(contents(h.root), /late-hidden-job/);
  h.document.documentElement.dataset.view = "robust"; h.emit("dashboard:view", { view: "robust" });
  const hiding = h.next("research-jobs");
  h.document.hidden = true; h.emit("visibilitychange"); const oldMessage = h.el("robustMessage").textContent;
  hiding.reject(new Error("late network error")); await flush();
  assert.equal(h.timers.size, 0); assert.equal(h.el("robustMessage").textContent, oldMessage);
});

test("changing reports clears old output immediately and failures cannot restore it", async () => {
  const h = await harness(); await h.start();
  h.emit("dashboard:research-open", { id: "old-report" }); h.next("id=old-report").resolve(report("old-report")); await flush();
  assert.ok(h.el("robustCurve"));
  h.emit("dashboard:research-open", { id: "failed-report" });
  assert.equal(h.el("robustCurve"), null); assert.match(contents(h.el("robustResults")), /正在加载/);
  h.next("id=failed-report").reject(new Error("missing archive")); await flush();
  assert.equal(h.el("robustCurve"), null); assert.match(contents(h.el("robustResults")), /missing archive/);
});

test("even same-id result races ignore superseded responses and errors", async () => {
  const h = await harness(); await h.start();
  h.emit("dashboard:research-open", { id: "same" }); const older = h.next("id=same");
  h.emit("dashboard:research-open", { id: "same" }); const newer = h.next("id=same");
  newer.resolve(report("same")); await flush();
  const rendered = h.plots.length;
  older.reject(new Error("stale result failure")); await flush();
  assert.ok(h.el("robustCurve")); assert.equal(h.plots.length, rendered);
  assert.equal(h.el("robustMessage").textContent, "已打开 same");
});

test("task conflicts show the current busy job status and restore submit controls", async () => {
  const h = await harness(); await h.start();
  h.el("robustForm").dispatchEvent({ type: "submit", preventDefault() {} });
  h.next("research-jobs").reject(new Error("A backtest is already running; wait or cancel it first")); await flush();
  h.next("research-jobs").resolve({ jobs: [{ id: "busy-job", status: "running", parameters: options().defaults }] }); await flush();
  assert.match(h.el("robustMessage").textContent, /研究中.*busy-job/);
  const submit = nodes(h.el("robustForm")).find((node) => node.type === "submit");
  assert.equal(submit.disabled, false);
});

test("legacy naive research curve timestamps retain UTC dates in every browser timezone", async () => {
  const h = await harness(); await h.start();
  const payload = report("legacy"); payload.curve = [
    { timestamp: "2026-03-30T00:00:00", value: 100 },
    { timestamp: "2026-08-27 00:00:00", value: 102 },
    { timestamp: "2026-08-28T00:00:00Z", value: 102 },
  ];
  h.emit("dashboard:research-open", { id: "legacy" }); h.next("id=legacy").resolve(payload); await flush();
  assert.equal(h.plots.at(-1)[0].points[0].timestamp, "2026-03-30T00:00:00Z");
  assert.equal(h.plots.at(-1)[0].points[1].timestamp, "2026-08-27T00:00:00Z");
  assert.equal(h.plots.at(-1)[0].points[2].timestamp, "2026-08-28T00:00:00Z");
  assert.equal(payload.curve[0].timestamp, "2026-03-30T00:00:00", "archived source stays unchanged");
});

test("copied parameters still submit completely while execution settings remain collapsed", async () => {
  const h = await harness(); await h.start();
  const parameters = { ...options().defaults, capital: 24000, slippage_bps: 7.5, seed: 17, windows: 2, entry_windows: [15, 30, 60] };
  h.emit("dashboard:view", { view: "robust" });
  h.next("research-jobs").resolve({ jobs: [{ id: "clone-source", status: "succeeded", parameters }] }); await flush();
  const clone = nodes(h.el("robustJobs")).find((node) => node.dataset.action === "clone");
  clone.dispatchEvent({ type: "click" });
  assert.ok(!h.el("robustExecutionSettings").open);
  h.el("robustForm").dispatchEvent({ type: "submit", preventDefault() {} });
  const submit = h.next("research-jobs");
  assert.deepEqual(JSON.parse(submit.settings.body), parameters);
  submit.resolve({ id: "new-research" }); await flush();
  h.next("research-jobs").resolve({ jobs: [] }); await flush();
});

test("native invalid events reveal execution fields before browser focus", async () => {
  const h = await harness(); await h.start();
  const execution = h.el("robustExecutionSettings");
  for (const name of ["capital", "slippage_bps", "seed"]) {
    execution.open = false;
    h.el("robustForm").dispatchEvent({ type: "invalid", target: h.el(`robust-${name}`) });
    assert.equal(execution.open, true);
  }
  execution.open = false;
  h.el("robustForm").dispatchEvent({ type: "invalid", target: h.el("robust-train_bars") });
  assert.equal(execution.open, false);
  assert.equal(h.requests.length, 0);
});

test("unchanged polling preserves task nodes; changed logs preserve disclosure and action focus", async () => {
  const h = await harness(); await h.start();
  const job = { id: "running-study", status: "running", parameters: options().defaults, logs: ["first log"], elapsed_seconds: 1 };
  h.emit("dashboard:view", { view: "robust" }); h.next("research-jobs").resolve({ jobs: [job] }); await flush();
  const row = h.el("robustJobs").children[0];
  row.querySelector("details").open = true;
  row.querySelectorAll("button").find((button) => button.dataset.action === "cancel").focus();
  h.emit("dashboard:view", { view: "robust" }); h.next("research-jobs").resolve({ jobs: [{ ...job, elapsed_seconds: 3 }] }); await flush();
  assert.strictEqual(h.el("robustJobs").children[0], row, "undisplayed elapsed time does not force a render");
  assert.equal(row.querySelector("details").open, true);
  h.emit("dashboard:view", { view: "robust" }); h.next("research-jobs").resolve({ jobs: [{ ...job, logs: ["first log", "second log"] }] }); await flush();
  const changed = h.el("robustJobs").children[0];
  assert.notStrictEqual(changed, row);
  assert.equal(changed.querySelector("details").open, true);
  assert.match(contents(changed), /second log/);
  assert.equal(h.document.activeElement.dataset.action, "cancel");
  assert.ok(changed.contains(h.document.activeElement));
});
