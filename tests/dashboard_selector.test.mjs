import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const html = await readFile(new URL("../dashboard/web_index.html", import.meta.url), "utf8");
const source = (await readFile(new URL("../dashboard/web_research.js", import.meta.url), "utf8"))
  .replace(/^import .*;\r?$/gm, "").replaceAll("export function", "function");

// Execute the production event handlers with a small DOM seam. The checkbox's
// initial state comes from the actual HTML, rather than a test-only default.
class Element {
  constructor(tag = "div", className = "", text) {
    this.tagName = tag; this.className = className; this.children = [];
    this.attributes = {}; this.dataset = {}; this.listeners = new Map();
    this.value = ""; this.checked = false; this.disabled = false;
    this.classList = { toggle: (name, enabled) => {
      const names = new Set(this.className.split(" ").filter(Boolean));
      if (enabled) names.add(name); else names.delete(name);
      this.className = [...names].join(" ");
    } };
    if (text !== undefined) this.textContent = text;
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map((node) => node.textContent).join(""); }
  append(...nodes) {
    for (const node of nodes) this.children.push(...(node.tagName === "fragment" ? node.children : [node]));
  }
  replaceChildren(...nodes) { this.text = ""; this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  querySelectorAll(selector) {
    const nodes = this.children.flatMap(descendants);
    if (selector === "input") return nodes.filter((node) => node.tagName === "input");
    if (selector === "input:checked") return nodes.filter((node) => node.tagName === "input" && node.checked);
    if (selector === "button[data-job-id]") return nodes.filter((node) => node.tagName === "button" && node.dataset.jobId !== undefined);
    throw new Error(`Unsupported test selector: ${selector}`);
  }
  contains(node) { return descendants(this).includes(node); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async emit(type, detail) {
    const event = { type, detail, preventDefault() { this.defaultPrevented = true; } };
    await Promise.all((this.listeners.get(type) || []).map((listener) => listener(event)));
    return event;
  }
  dispatchEvent(event) { return this.emit(event.type, event.detail); }
  scrollIntoView() { this.scrolled = true; }
  focus() {}
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const strategy = { name: "TrendBreakout", parameters: { lookback: 20 } };
function defaults() {
  return {
    enabled: true, csrf_token: "test-csrf", required_symbols: 1,
    limits: { min_days: 30, max_days: 1096, max_symbols: 4 },
    symbols: ["BTC/USDT", "ETH/USDT"], synthetic_symbols: ["BTC/USDT", "ETH/USDT"],
    defaults: { source: "synthetic", symbols: ["BTC/USDT"], start: "2024-01-01", end: "2024-03-01", capital: 10000, seed: 7, slippage_bps: 5, use_selector: false },
  };
}
function cloneParameters(overrides = {}) {
  return {
    source: "synthetic", symbols: ["ETH/USDT"], start: "2024-04-01", end: "2024-06-01",
    capital: 20000, seed: 13, slippage_bps: 8, strategy, ...overrides,
  };
}
async function harness() {
  const elements = new Map(), requests = [], posts = [], appliedStrategies = [];
  let pendingPost;
  const el = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const selectorTag = html.match(/<input\b[^>]*\bid="researchUseSelector"[^>]*>/)?.[0];
  assert.ok(selectorTag, "The real form must contain the selector checkbox");
  const checkbox = el("researchUseSelector");
  for (const [, key, value] of selectorTag.matchAll(/([\w-]+)="([^"]*)"/g)) checkbox.attributes[key] = value;
  checkbox.type = checkbox.attributes.type; checkbox.name = checkbox.attributes.name;
  checkbox.checked = /\schecked(?:\s|=|\/?>)/.test(selectorTag);
  const document = new Element("document");
  document.documentElement = new Element("html"); document.documentElement.dataset.view = "backtest";
  const fixture = defaults();
  const context = vm.createContext({
    document, el, element: (tag, cls = "", text) => new Element(tag, cls, text),
    requestJSON: async (path, config = {}) => {
      requests.push({ path, config });
      if (path === "/api/backtest-options") return structuredClone(fixture);
      if (path === "/api/backtest-jobs" && config.method === "POST") {
        const parameters = JSON.parse(config.body); posts.push(parameters);
        if (pendingPost) await pendingPost;
        return { job: { id: `job_${posts.length}`, kind: "backtest", status: "succeeded", parameters } };
      }
      if (path === "/api/backtest-jobs") return { jobs: [] };
      if (path.startsWith("/api/data-quality?")) return { selection: { valid: true, errors: [] } };
      if (path.startsWith("/api/backtest-trades?")) return { available: true, rows: [], columns: [], total: 0, page: 1, pages: 0 };
      throw new Error(`Unexpected request: ${path}`);
    },
    initStrategy: async () => {}, getStrategy: () => strategy,
    applyStrategy: async (value) => { appliedStrategies.push(value); },
    formatNumber: (value) => String(value), formatPercent: (value) => value == null ? "—" : `${value * 100}%`,
    formatCell: (value) => String(value), cellClass: () => "",
    URLSearchParams, setTimeout: () => 1, clearTimeout() {},
    CustomEvent: class { constructor(type, { detail } = {}) { this.type = type; this.detail = detail; } },
  });
  document.createDocumentFragment = () => new Element("fragment");
  document.createElement = (tag) => new Element(tag);
  vm.runInContext(source, context, { filename: "web_research.js" });
  await context.initResearch();
  return { context, document, el, fixture, requests, posts, appliedStrategies, selectorTag,
           setPostWait: (promise) => { pendingPost = promise; } };
}

test("the real form starts with the selector disabled and exposes a named switch", async () => {
  const h = await harness(), checkbox = h.el("researchUseSelector");
  assert.equal(checkbox.type, "checkbox"); assert.equal(checkbox.name, "use_selector");
  assert.equal(checkbox.attributes.role, "switch");
  assert.doesNotMatch(h.selectorTag, /\schecked(?:\s|=|\/?>)/);
  assert.equal(checkbox.checked, false); assert.equal(checkbox.attributes["aria-checked"], "false");
  assert.doesNotMatch(html, /<input\b[^>]*\bname="(?:model_path|selector_model_path)"/);
});

test("loadOptions only enables the selector for an explicit boolean default", async () => {
  const h = await harness();
  for (const value of [undefined, false, "true", 1, true]) {
    if (value === undefined) delete h.fixture.defaults.use_selector;
    else h.fixture.defaults.use_selector = value;
    await h.context.loadOptions(false);
    assert.equal(h.el("researchUseSelector").checked, value === true, `Default ${String(value)}`);
    assert.equal(h.el("researchUseSelector").attributes["aria-checked"], String(value === true));
  }
});

test("the refresh listener preserves both enabled and disabled user choices", async () => {
  const h = await harness(), checkbox = h.el("researchUseSelector");
  const saved = ["Source", "Start", "End", "Capital", "Seed", "Slippage"].map((suffix) => h.el(`research${suffix}`).value);
  for (const enabled of [true, false]) {
    checkbox.checked = enabled; await checkbox.emit("change");
    h.fixture.defaults.use_selector = !enabled;
    await h.el("refreshJobs").emit("click");
    assert.equal(checkbox.checked, enabled);
    assert.equal(checkbox.attributes["aria-checked"], String(enabled));
    assert.deepEqual(["Source", "Start", "End", "Capital", "Seed", "Slippage"].map((suffix) => h.el(`research${suffix}`).value), saved);
  }
});

test("the real submit listener sends strict selector booleans for both data sources", async () => {
  const h = await harness();
  for (const dataSource of ["synthetic", "local"]) {
    h.el("researchSource").value = dataSource; await h.el("researchSource").emit("change");
    for (const enabled of [false, true]) {
      h.el("researchUseSelector").checked = enabled;
      h.el("researchUseSelector").value = enabled ? "false" : "true";
      await h.el("researchUseSelector").emit("change");
      const event = await h.el("backtestForm").emit("submit");
      assert.equal(event.defaultPrevented, true);
      const parameters = h.posts.at(-1);
      assert.equal(parameters.use_selector, enabled); assert.equal(typeof parameters.use_selector, "boolean");
      assert.equal(parameters.source, dataSource);
      assert.deepEqual(Object.keys(parameters).sort(), ["source", "symbols", "start", "end", "capital", "slippage_bps", "seed", "strategy", "use_selector"].sort());
      assert.deepEqual(parameters.strategy, strategy);
      assert.equal(parameters.capital, 10000); assert.equal(parameters.slippage_bps, 5);
    }
  }
  assert.equal(h.posts.length, 4);
  assert.equal(h.requests.filter(({ path }) => path.startsWith("/api/data-quality?")).length, 2);
  for (const { config } of h.requests.filter(({ config }) => config.method === "POST")) {
    assert.equal(config.headers["X-CSRF-Token"], "test-csrf");
  }
});

test("cloning restores enabled experiments and resets legacy or non-boolean selector values", async () => {
  const h = await harness();
  for (const detail of [cloneParameters({ use_selector: true }), cloneParameters(), cloneParameters({ use_selector: "true" }), cloneParameters({ use_selector: false })]) {
    h.el("researchUseSelector").checked = true;
    await h.document.emit("dashboard:clone-experiment", detail);
    assert.equal(h.el("researchUseSelector").checked, detail.use_selector === true);
    assert.equal(h.el("researchUseSelector").attributes["aria-checked"], String(detail.use_selector === true));
    assert.equal(h.el("researchCapital").value, detail.capital);
    assert.equal(h.el("researchStart").value, detail.start);
    assert.equal(h.el("researchEnd").value, detail.end);
    assert.deepEqual(h.el("researchSymbols").querySelectorAll("input:checked").map((input) => input.value), ["ETH/USDT"]);
    assert.equal(h.el("backtestForm").scrolled, true);
  }
  assert.equal(h.appliedStrategies.length, 4);
});

test("clone then submit carries only the boolean switch and ignores arbitrary model paths", async () => {
  const h = await harness();
  const detail = cloneParameters({ use_selector: true, model_path: "D:/external/model.json", selector_model_path: "../arbitrary.json" });
  await h.document.emit("dashboard:clone-experiment", detail);
  await h.el("backtestForm").emit("submit");
  assert.equal(h.posts.length, 1); assert.equal(h.posts[0].use_selector, true);
  assert.equal(Object.hasOwn(h.posts[0], "model_path"), false);
  assert.equal(Object.hasOwn(h.posts[0], "selector_model_path"), false);
  assert.deepEqual(h.posts[0].symbols, ["ETH/USDT"]);
  assert.equal(h.posts[0].capital, 20000); assert.equal(h.posts[0].start, "2024-04-01");
});

test("the original account button uses the switch and ignores invalid ordinary form fields", async () => {
  const h = await harness();
  const tag = html.match(/<button\b[^>]*\bid="runOriginalBacktest"[^>]*>/)?.[0];
  assert.ok(tag); assert.match(tag, /type="button"/);
  assert.match(html, /60 币 · 2020-01-01 至 2026-09-18 · 智能资金分配；沿用上方选币器开关/);
  h.el("researchStart").value = "bad date"; h.el("researchEnd").value = "";
  h.el("researchCapital").value = "1"; h.el("researchSource").value = "local";
  h.el("researchSymbols").querySelectorAll("input").forEach((node) => { node.checked = false; });
  for (const enabled of [false, true]) {
    h.el("researchUseSelector").checked = enabled;
    await h.el("runOriginalBacktest").emit("click");
    assert.deepEqual(h.posts.at(-1), { preset: "original_100k", use_selector: enabled });
  }
  assert.equal(h.posts.length, 2);
  assert.equal(h.requests.filter(({ path }) => path.startsWith("/api/data-quality?")).length, 0);
  for (const { config } of h.requests.filter(({ config }) => config.method === "POST")) {
    assert.equal(config.headers["X-CSRF-Token"], "test-csrf");
  }
});

test("both run buttons remain disabled while submitting or while a job is active", async () => {
  const h = await harness();
  let release;
  h.setPostWait(new Promise((resolve) => { release = resolve; }));
  const pending = h.el("runOriginalBacktest").emit("click");
  assert.equal(h.posts.length, 1);
  assert.equal(h.el("runBacktest").disabled, true);
  assert.equal(h.el("runOriginalBacktest").disabled, true);
  await h.el("runOriginalBacktest").emit("click");
  await h.el("backtestForm").emit("submit");
  assert.equal(h.posts.length, 1);
  release(); await pending;
  assert.equal(h.el("runBacktest").disabled, false);
  assert.equal(h.el("runOriginalBacktest").disabled, false);
  vm.runInContext("jobs = [{status: 'running'}]; updateButton();", h.context);
  await h.el("runOriginalBacktest").emit("click");
  assert.equal(h.posts.length, 1);
  assert.equal(h.el("runBacktest").disabled, true);
  assert.equal(h.el("runOriginalBacktest").disabled, true);
  vm.runInContext("jobs = [];", h.context);
  h.fixture.enabled = false; await h.context.loadOptions(true);
  await h.el("runOriginalBacktest").emit("click");
  assert.equal(h.posts.length, 1);
  assert.equal(h.el("runBacktest").disabled, true);
  assert.equal(h.el("runOriginalBacktest").disabled, true);
});

test("cloning the original preset restores the switch without replacing ordinary form limits", async () => {
  const h = await harness();
  const saved = ["Source", "Start", "End", "Capital", "Seed", "Slippage"].map((suffix) => h.el(`research${suffix}`).value);
  for (const enabled of [true, false]) {
    await h.document.emit("dashboard:clone-experiment", {
      preset: "original_100k", use_selector: enabled, source: "local", capital: 100000,
      start: "2020-01-01", end: "2026-09-18", seed: 42,
      symbols: Array.from({ length: 60 }, (_, index) => `S${index}/USDT`),
    });
    assert.equal(h.el("researchUseSelector").checked, enabled);
    assert.match(h.el("researchMessage").textContent, /点击“复现原 10 万本金回测”/);
    assert.deepEqual(["Source", "Start", "End", "Capital", "Seed", "Slippage"].map((suffix) => h.el(`research${suffix}`).value), saved);
    await h.el("runOriginalBacktest").emit("click");
    assert.deepEqual(h.posts.at(-1), { preset: "original_100k", use_selector: enabled });
  }
});
