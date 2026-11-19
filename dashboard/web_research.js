import { requestJSON, el, element, formatNumber, formatPercent } from "./api.js";
import { formatCell, cellClass } from "./charts.js";
import { initStrategy, getStrategy, applyStrategy } from "./strategy.js";

let options;
let jobs = [];
let polling;
let submitting = false;
let latestReport;
let tradePage = 1;
let tradePages = 0;
let tradesRequest = 0;
let jobsRequest = 0;
const completed = new Set();
const active = (job) => ["queued", "running"].includes(job.status);
const statusNames = { queued: "等待运行", running: "运行中", succeeded: "已完成", failed: "运行失败", timed_out: "已超时", cancelled: "已取消", interrupted: "重启中断" };

function clearAnalysis(text) {
  latestReport = null;
  tradesRequest++;
  el("analysisContext").textContent = text;
  el("analysisStats").replaceChildren(element("p", "panel-empty", text));
  el("monthlyReturns").replaceChildren(element("p", "panel-empty", "等待有效报告"));
  el("exportAnalysis").disabled = true;
  el("tradeHead").replaceChildren();
  const row = element("tr"); row.append(element("td", "empty-cell", text));
  el("tradeBody").replaceChildren(row);
  el("tradeCount").textContent = "—"; el("tradePage").textContent = "—";
  el("previousTrades").disabled = true; el("nextTrades").disabled = true;
}

function message(text, error = false) {
  el("researchMessage").textContent = text;
  el("researchMessage").classList.toggle("error", error);
}
function updateSelectorNote() {
  el("researchUseSelector").setAttribute("aria-checked", String(el("researchUseSelector").checked === true));
}
function updateButton() {
  el("runBacktest").disabled = submitting || !options?.enabled || jobs.some(active);
  el("runOriginalBacktest").disabled = el("runBacktest").disabled;
  el("runBacktest").textContent = submitting ? "正在提交…" : jobs.some(active) ? "回测正在后台运行…" : "运行本地回测 ↗";
}
function symbols() {
  const source = el("researchSource").value;
  const available = source === "synthetic" ? options?.synthetic_symbols || ["BTC/USDT", "ETH/USDT", "BNB/USDT"] : options?.symbols || [];
  const selected = new Set(Array.from(el("researchSymbols").querySelectorAll("input:checked"), (node) => node.value));
  if (!selected.size) (options?.defaults?.symbols || ["BTC/USDT"]).forEach((value) => selected.add(value));
  const offered = new Set(available.map((value) => typeof value === "string" ? value : value.symbol));
  for (const value of selected) if (!offered.has(value)) selected.delete(value);
  for (const value of offered) {
    if (selected.size >= (options?.required_symbols || 1)) break;
    selected.add(value);
  }
  const fragment = document.createDocumentFragment();
  available.forEach((value) => {
    const symbol = typeof value === "string" ? value : value.symbol;
    if (!symbol) return;
    const label = element("label", "symbol-option");
    const input = document.createElement("input");
    input.type = "checkbox"; input.name = "symbol"; input.value = symbol;
    input.checked = selected.has(symbol);
    label.append(input, element("span", "", symbol)); fragment.append(label);
  });
  el("researchSymbols").replaceChildren(fragment);
  if (!available.length) el("researchSymbols").append(element("p", "", "没有本地行情，请使用合成数据验证功能。"));
  const range = options?.cache_range;
  el("symbolLimit").textContent = `${options?.required_symbols || 1}–${options?.limits?.max_symbols || 4} 个`;
  el("researchDataNote").textContent = source === "synthetic"
    ? "合成行情仅用于功能验证。结果不代表真实市场表现；相同种子便于复现。"
    : `本地日线缓存${range?.start && range?.end ? `：${range.start} 至 ${range.end}` : "，实际可用区间取决于所选标的"}。每次最多 ${options?.limits?.max_days || 1096} 天。${options?.required_symbols > 1 ? `当前策略配置至少需要 ${options.required_symbols} 个标的。` : ""}`;
}
async function loadOptions(preserve = false) {
  try {
    options = await requestJSON("/api/backtest-options", { key: "research-options" });
    const defaults = options.defaults || {};
    if (!preserve) {
      el("researchSource").value = defaults.source || "local";
      ["Start", "End", "Capital", "Seed"].forEach((suffix) => {
        if (defaults[suffix.toLowerCase()] !== undefined) el(`research${suffix}`).value = defaults[suffix.toLowerCase()];
      });
      el("researchSlippage").value = defaults.slippage_bps ?? 5;
      el("researchUseSelector").checked = defaults.use_selector === true;
    }
    updateSelectorNote();
    symbols();
    message(options.enabled ? "配置保存于每次任务，可在实验记录中追溯。" : "此服务禁用了网页回测。请使用默认启动命令启用。", !options.enabled);
  } catch (error) {
    message(`无法读取回测配置：${error.message}。点击“刷新记录”重试。`, true);
  }
  updateButton();
}

let jobListSignature;
const cancellingJobs = new Set();
function renderJobs() {
  const visible = jobs.slice(0, 5).map((job) => {
    const params = job.parameters || {};
    return {
      job,
      title: params.preset === "original_100k" ? "原 10 万本金回测 · 60 币 · 智能资金分配" : `${(params.symbols || []).join(" + ") || job.id} · ${params.source === "synthetic" ? "合成情景" : "历史行情"}`,
      detail: `${params.start || "—"} → ${params.end || "—"} · ${formatNumber(job.elapsed_seconds || 0)} 秒${!job.kind || job.kind === "backtest" ? ` · 选币器${params.use_selector === true ? "开启" : "关闭"}` : ""}`,
      action: active(job) ? "cancel" : job.status === "succeeded" && job.run_id && (!job.kind || job.kind === "backtest") ? "report" : "",
    };
  });
  const signature = JSON.stringify(visible.map(({ job, title, detail, action }) => [job.id, title, detail, job.error || "", job.status, action, action === "report" ? job.run_id : null]));
  const list = el("jobList");
  if (signature !== jobListSignature) {
    const focusedId = list.contains(document.activeElement) ? document.activeElement?.dataset.jobId : undefined;
    let focusTarget;
    const fragment = document.createDocumentFragment();
    visible.forEach(({ job, title, detail, action }) => {
      const row = element("div", "job-row");
      const copy = element("div");
      copy.append(element("strong", "", title));
      copy.append(element("small", "", detail));
      if (job.error) copy.append(element("small", "", job.error));
      row.append(copy, element("span", `job-status ${job.status}`, statusNames[job.status] || job.status));
      if (action === "cancel") {
        const button = element("button", "text-button", "取消任务");
        button.type = "button";
        button.dataset.jobId = job.id;
        button.disabled = cancellingJobs.has(job.id);
        if (job.id === focusedId) focusTarget = button;
        button.addEventListener("click", async () => {
          if (cancellingJobs.has(job.id)) return;
          cancellingJobs.add(job.id);
          button.disabled = true;
          try {
            await requestJSON("/api/backtest-jobs/cancel", {
              method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": options.csrf_token }, body: JSON.stringify({ id: job.id }),
            });
            await refreshJobs();
          } catch (error) { message(error.message, true); }
          finally {
            cancellingJobs.delete(job.id);
            list.querySelectorAll("button[data-job-id]").forEach((item) => { if (item.dataset.jobId === job.id) item.disabled = false; });
          }
        });
        row.append(button);
      } else if (action === "report") {
        const button = element("button", "text-button", "查看报告 ↗");
        button.type = "button";
        button.dataset.jobId = job.id;
        if (job.id === focusedId) focusTarget = button;
        button.addEventListener("click", () => selectReport(job.run_id));
        row.append(button);
      }
      fragment.append(row);
    });
    if (!jobs.length) fragment.append(element("p", "panel-empty", "本次服务暂无任务。可在下方查看历史报告，或配置参数开始新实验。"));
    list.replaceChildren(fragment);
    jobListSignature = signature;
    if (focusedId !== undefined) {
      if (!focusTarget || focusTarget.disabled) { list.tabIndex = -1; focusTarget = list; }
      focusTarget.focus({ preventScroll: true });
    }
  }
  const latest = jobs[0];
  const log = latest ? ((latest.logs || []).join("\n") || latest.error || "等待引擎输出日志…") : "提交实验后，日志会显示在这里。";
  if (el("jobLog").textContent !== log) el("jobLog").textContent = log;
  updateButton();
}
function selectReport(runId) {
  document.dispatchEvent(new CustomEvent("dashboard:backtest-complete", { detail: { run_id: runId } }));
}
function schedulePoll() {
  clearTimeout(polling);
  if (document.hidden || document.documentElement.dataset.view !== "backtest") return;
  polling = setTimeout(refreshJobs, jobs.some(active) ? 2000 : 15000);
}
async function refreshJobs() {
  const request = ++jobsRequest;
  try {
    const payload = await requestJSON("/api/backtest-jobs", { key: "research-jobs" });
    if (request !== jobsRequest) return;
    jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    const newlyCompleted = jobs.find((job) => job.status === "succeeded" && (!job.kind || job.kind === "backtest") && !completed.has(job.id));
    jobs.filter((job) => job.status === "succeeded").forEach((job) => completed.add(job.id));
    renderJobs();
    if (newlyCompleted?.run_id) selectReport(newlyCompleted.run_id);
  } catch (error) {
    if (error.name !== "AbortError") message(`任务状态暂不可用：${error.message}。可点击刷新记录重试。`, true);
  } finally { if (request === jobsRequest) schedulePoll(); }
}

async function submit(event) {
  event.preventDefault();
  if (submitting || jobs.some(active) || !options?.enabled) return;
  const selected = Array.from(el("researchSymbols").querySelectorAll("input:checked"), (node) => node.value);
  const maxSymbols = options.limits?.max_symbols || 4;
  const minSymbols = options.required_symbols || 1;
  if (selected.length < minSymbols || selected.length > maxSymbols) { message(`当前策略配置需要 ${minSymbols} 至 ${maxSymbols} 个交易标的。`, true); return; }
  const start = el("researchStart").value;
  const end = el("researchEnd").value;
  const days = (Date.parse(end) - Date.parse(start)) / 86400000;
  if (!Number.isFinite(days) || days < (options.limits?.min_days || 30) || days > (options.limits?.max_days || 1096)) {
    message(`请选择 ${options.limits?.min_days || 30} 至 ${options.limits?.max_days || 1096} 天的有效时间区间。`, true); return;
  }
  const parameters = {
    source: el("researchSource").value, symbols: selected, start, end,
    capital: Number(el("researchCapital").value), slippage_bps: Number(el("researchSlippage").value), seed: Number(el("researchSeed").value),
    use_selector: el("researchUseSelector").checked === true,
    strategy: getStrategy(),
  };
  await createBacktest(parameters);
}

async function submitOriginal(event) {
  event.preventDefault();
  await createBacktest({ preset: "original_100k", use_selector: el("researchUseSelector").checked === true });
}

async function createBacktest(parameters) {
  if (submitting || jobs.some(active) || !options?.enabled) return;
  submitting = true; updateButton(); message("正在创建本地实验…");
  try {
    if (parameters.source === "local" && !parameters.preset) {
      const query = new URLSearchParams({ symbols: parameters.symbols.join(","), start: parameters.start, end: parameters.end });
      const quality = await requestJSON(`/api/data-quality?${query}`, { key: "backtest-preflight" });
      if (!quality.selection?.valid) throw new Error((quality.selection?.errors || ["所选数据质量检查未通过"]).join("；"));
    }
    const payload = await requestJSON("/api/backtest-jobs", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": options.csrf_token }, body: JSON.stringify(parameters),
    });
    jobs = [payload.job, ...jobs.filter((job) => job.id !== payload.job.id)]; renderJobs();
    message("已提交。任务在本机后台运行，切换工作区不会中断回测。");
    await refreshJobs();
  } catch (error) {
    message(`提交状态未确认：${error.message}。请先刷新实验记录，再决定是否重试。`, true);
    await refreshJobs();
  } finally { submitting = false; updateButton(); }
}

function renderAnalysis(report) {
  latestReport = report;
  tradePage = 1;
  el("exportAnalysis").disabled = false;
  const source = report.parameters?.source === "synthetic" ? "合成数据 · 非真实市场" : report.parameters?.source === "local" ? "本地历史行情" : "历史报告 · 来源以原报告为准";
  el("analysisContext").textContent = `${source}${report.parameters?.preset === "original_100k" ? " · 原 10 万本金 · 智能资金分配" : ""} · ${report.id} · 选币器${report.parameters?.use_selector === true ? "开启" : "关闭"} · ${report.metrics.observations ?? report.points?.length ?? 0} 个权益点`;
  const metrics = report.metrics || {};
  const stats = [
    ["年化收益 · CAGR", formatPercent(metrics.annualized_return)],
    ["基准区间收益", formatPercent(metrics.benchmark_total_return)],
    ["超额收益 · 百分点", Number.isFinite(metrics.excess_return) ? `${(metrics.excess_return * 100).toFixed(2)} pp` : "—"],
    ["正收益周期占比", formatPercent(metrics.positive_period_ratio)],
    ["最佳单周期收益", formatPercent(metrics.best_period_return)],
    ["最差单周期收益", formatPercent(metrics.worst_period_return)],
  ];
  el("analysisStats").replaceChildren(...stats.map(([label, value]) => {
    const cell = element("div", "analysis-stat"); cell.append(element("span", "", label), element("strong", "", value)); return cell;
  }));
  const months = report.monthly_returns || [];
  el("monthlyReturns").replaceChildren(...months.map((item) => {
    const cell = element("div", `month-cell ${item.return > 0 ? "positive" : item.return < 0 ? "negative" : ""}`);
    cell.title = `${item.period_start} → ${item.period_end} · ${item.periods} 个周期`;
    cell.append(element("span", "", item.month), element("strong", "", formatPercent(item.return))); return cell;
  }));
  if (!months.length) el("monthlyReturns").append(element("p", "panel-empty", "有效数据不足，暂无月度收益。"));
  el("analysisMethodology").replaceChildren(
    element("p", "", "总收益 = 期末权益 / 首个有效权益 − 1；回撤 = 当期权益 / 历史最高权益 − 1。年化收益按实际经过天数、每年 365.25 天换算，短区间年化值可能被放大。"),
    element("p", "", "单周期收益取相邻权益点之比；正收益周期占比不是交易胜率。月度收益连接上月末权益，首月可能是不完整月份。"),
    element("p", "", report.benchmark_aligned ? "基准覆盖报告的首尾日期，超额为策略与基准区间收益之差。" : "基准缺失或未对齐首尾日期，基准收益与超额不作估算。"),
  );
  loadTrades();
}

const columnNames = {
  timestamp: "时间", fill_time: "成交时间", signal_time: "信号时间", symbol: "标的", side: "方向",
  qty: "数量", quantity: "数量", price: "价格", fill_price: "成交价", theoretical_price: "理论价",
  commission: "手续费", fee: "费用", slippage: "滑点", slip: "滑点", slip_dir: "滑点方向",
  spread_bps: "价差 · bps", spread_slippage_rate: "价差滑点率", strategy: "策略", strategy_id: "策略",
  pnl: "盈亏", reason: "原因", order_id: "订单 ID",
};
const preferredColumns = ["fill_time", "timestamp", "symbol", "side", "qty", "quantity", "fill_price", "price", "commission", "pnl", "strategy"];
async function loadTrades() {
  if (!latestReport) return;
  const id = latestReport.id;
  const request = ++tradesRequest;
  el("previousTrades").disabled = true; el("nextTrades").disabled = true;
  el("tradeCount").textContent = "读取中";
  el("tradeHead").replaceChildren();
  const loadingRow = element("tr"); loadingRow.append(element("td", "empty-cell", "正在读取成交记录…")); el("tradeBody").replaceChildren(loadingRow);
  try {
    const payload = await requestJSON(`/api/backtest-trades?id=${encodeURIComponent(id)}&page=${tradePage}&page_size=25`, { key: "research-trades" });
    if (request !== tradesRequest || id !== latestReport.id) return;
    tradePages = payload.pages || 0; tradePage = payload.page || 1;
    el("tradeCount").textContent = `${formatNumber(payload.total)} 条记录`;
    const supplied = payload.columns || [];
    const columns = [...preferredColumns.filter((name) => supplied.includes(name)), ...supplied.filter((name) => !preferredColumns.includes(name))];
    const head = element("tr");
    columns.forEach((name) => { const th = element("th", cellClass({ key: name }), columnNames[name] || name); th.setAttribute("scope", "col"); head.append(th); });
    el("tradeHead").replaceChildren(head);
    el("tradeBody").replaceChildren(...(payload.rows || []).map((row) => {
      const tr = element("tr"); columns.forEach((name) => { const column = { key: name, csv: true }; const td = element("td", cellClass(column, row[name] == null), formatCell(row[name], column, row)); td.title = row[name] == null ? "未记录" : `原始值：${row[name]}`; tr.append(td); }); return tr;
    }));
    if (!payload.rows?.length) {
      const tr = element("tr"); const td = element("td", "empty-cell", payload.available ? "这个区间没有成交记录。" : "此历史报告未保存 trades.csv。"); td.colSpan = Math.max(1, columns.length); tr.append(td); el("tradeBody").append(tr);
    }
    el("tradePage").textContent = tradePages ? `${tradePage} / ${tradePages} 页` : "无成交";
    el("previousTrades").disabled = tradePage <= 1;
    el("nextTrades").disabled = tradePage >= tradePages;
  } catch (error) {
    if (error.name === "AbortError" || request !== tradesRequest) return;
    el("tradeCount").textContent = "读取失败";
    el("tradePage").textContent = "刷新页面可重试";
    const tr = element("tr"); tr.append(element("td", "empty-cell", error.message)); el("tradeBody").replaceChildren(tr);
  }
}

async function initializeResearch() {
  await initStrategy();
  el("backtestForm").addEventListener("submit", submit);
  el("runOriginalBacktest").addEventListener("click", submitOriginal);
  el("researchSource").addEventListener("change", symbols);
  el("researchUseSelector").addEventListener("change", updateSelectorNote);
  el("refreshJobs").addEventListener("click", async () => { await loadOptions(Boolean(options)); await refreshJobs(); });
  el("previousTrades").addEventListener("click", () => { if (tradePage > 1) { tradePage--; loadTrades(); } });
  el("nextTrades").addEventListener("click", () => { if (tradePage < tradePages) { tradePage++; loadTrades(); } });
  el("exportAnalysis").addEventListener("click", () => {
    if (!latestReport) return;
    const { points, ...summary } = latestReport;
    const blob = new Blob([JSON.stringify(summary, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob); const link = element("a");
    link.href = url; link.download = `${latestReport.id}_analysis.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  document.addEventListener("dashboard:backtest-loaded", ({ detail }) => renderAnalysis(detail));
  document.addEventListener("dashboard:backtest-loading", () => clearAnalysis("正在读取所选报告…"));
  document.addEventListener("dashboard:backtest-error", ({ detail }) => clearAnalysis(detail.message || "报告读取失败，请重试。"));
  document.dispatchEvent(new CustomEvent("dashboard:backtest-current"));
  document.addEventListener("dashboard:view", ({ detail }) => { if (detail.view === "backtest") refreshJobs(); else clearTimeout(polling); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(polling); else if (document.documentElement.dataset.view === "backtest") refreshJobs(); });
  document.addEventListener("dashboard:refresh", () => { if (document.documentElement.dataset.view === "backtest") { refreshJobs(); loadTrades(); } });
  document.addEventListener("dashboard:clone-experiment", async ({ detail }) => {
    await loadOptions(true);
    if (detail.preset === "original_100k") {
      el("researchUseSelector").checked = detail.use_selector === true;
      updateSelectorNote();
      message("已复制原 10 万本金预设及选币器状态。点击“复现原 10 万本金回测”使用相同注册输入运行。");
      el("backtestForm").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (!["local", "synthetic"].includes(detail.source) || ["symbols", "start", "end", "capital", "seed", "slippage_bps"].some((key) => detail[key] === undefined || detail[key] === null)) { message("此历史报告未保存完整运行参数，无法直接复制。", true); return; }
    el("researchSource").value = detail.source; symbols();
    el("researchSymbols").querySelectorAll("input").forEach((input) => { input.checked = detail.symbols?.includes(input.value) || false; });
    for (const suffix of ["Start", "End", "Capital", "Seed"]) if (detail[suffix.toLowerCase()] !== undefined) el(`research${suffix}`).value = detail[suffix.toLowerCase()];
    if (detail.slippage_bps !== undefined) el("researchSlippage").value = detail.slippage_bps;
    el("researchUseSelector").checked = detail.use_selector === true;
    updateSelectorNote();
    await applyStrategy(detail.strategy);
    message("已复制实验参数。新实验使用当前基础配置及所选参数，独立保存快照；这不是旧配置的严格回放。请检查后运行。");
    el("backtestForm").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  await loadOptions();
  await refreshJobs();
}

let initialization;
export function initResearch() {
  initialization ||= initializeResearch().catch((error) => { initialization = null; throw error; });
  return initialization;
}
