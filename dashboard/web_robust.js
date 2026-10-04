/** Bounded real-engine walk-forward experiments; loaded only on first visit. */
import { requestJSON, element, formatNumber, formatPercent } from "./api.js";
import { lineChart, table, stats } from "./charts.js";

let initialization, options, polling, selectedId, currentResult;
let jobsRequest = 0, resultRequest = 0, latestJobs = [], jobsSignature;
let form, message, taskList, resultArea, submitButton, budget;
const active = (job) => ["queued", "running"].includes(job.status);
const statuses = { queued: "等待启动", running: "研究中", succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时", interrupted: "服务重启中断" };
const researchFields = ["train_bars", "validation_bars", "test_bars", "purge_bars", "windows", "entry_windows", "exit_windows", "selection_metric"];
const parametersFields = ["source", "symbols", "start", "end", "capital", "slippage_bps", "seed", ...researchFields];
const fields = new Map();
const visible = () => !document.hidden && document.documentElement.dataset.view === "robust";
function stopPolling() { clearTimeout(polling); jobsRequest++; }
function clearResult(text) {
  currentResult = null;
  resultArea.replaceChildren(element("p", "panel-empty", text));
}
function drawCurve(container, result) {
  // Older archived workers emitted naive ISO values for their UTC bar clock.
  // Make that convention explicit before Date.parse sees the browser timezone.
  const points = result.curve.map((point) => {
    let timestamp = point.timestamp.replace(" ", "T");
    if (timestamp.includes("T") && !/(?:Z|[+-]\d{2}:?\d{2})$/iu.test(timestamp)) timestamp += "Z";
    return { ...point, timestamp };
  });
  lineChart(container, [{ name: "选参规则 OOS 净值 · 窗口边界", points }], { label: "滚动研究样本外净值，起点 100", baseline: 100 });
}

function field(key, label, type, value, attributes = {}) {
  const wrapper = element("label", "field");
  wrapper.append(element("span", "", label));
  const input = element(type === "select" ? "select" : "input");
  input.name = key; input.id = `robust-${key}`;
  if (type !== "select") input.type = type;
  input.required = true;
  Object.entries(attributes).forEach(([name, setting]) => input.setAttribute(name, setting));
  if (value !== undefined) input.value = value;
  fields.set(key, input); wrapper.append(input);
  return wrapper;
}
function pair(...children) { const row = element("div", "field-pair"); row.append(...children); return row; }
function formSection(step, title, note, ...children) {
  const section = element("fieldset", "form-section");
  const legend = element("legend");
  const number = element("span", "form-step", String(step).padStart(2, "0"));
  number.setAttribute("aria-hidden", "true");
  legend.append(number, element("span", "", title));
  const description = element("p", "form-section-note", note);
  description.id = `robust-section-${step}-note`;
  section.setAttribute("aria-describedby", description.id);
  section.append(legend, description, ...children);
  return section;
}
function panel(title, description) {
  const section = element("section", "panel research-stack");
  section.append(element("h3", "", title));
  if (description) section.append(element("p", "panel-description", description));
  return section;
}
function button(label, callback, primary = false) {
  const node = element("button", primary ? "primary-button" : "secondary-button", label);
  node.type = "button"; node.addEventListener("click", callback); return node;
}
function populate(select, values, selected) {
  select.replaceChildren(...values.map(({ id, label }) => {
    const option = element("option", "", label); option.value = id; return option;
  }));
  select.value = selected;
}
function integers(value) { return value.split(/[,，\s]+/u).filter(Boolean).map(Number); }
function readForm() {
  return Object.fromEntries(parametersFields.map((key) => {
    const value = fields.get(key).value;
    if (key === "symbols") return [key, value.split(/[,，\s]+/u).map((part) => part.trim()).filter(Boolean)];
    if (["entry_windows", "exit_windows"].includes(key)) return [key, integers(value)];
    return [key, ["source", "start", "end", "selection_metric"].includes(key) ? value : Number(value)];
  }));
}
function updateBudget() {
  const values = readForm();
  const count = values.entry_windows.length * values.exit_windows.length;
  const warmup = Math.max(options.geometry.warmup_bars, ...values.entry_windows);
  const bars = values.train_bars + values.validation_bars + values.test_bars + values.purge_bars + values.windows * Math.max(warmup, values.test_bars);
  budget.textContent = `${count} 个候选 × ${values.windows} 个窗口 · 最多 ${count * values.windows * 2} 次引擎运行 · 约需 ${bars} 根日线。实际采用所选日期内最后一段完整数据。`;
}
function fillForm(parameters) {
  parametersFields.forEach((key) => { if (parameters[key] !== undefined) fields.get(key).value = Array.isArray(parameters[key]) ? parameters[key].join(", ") : parameters[key]; });
  updateBudget();
}
function sourceChanged() {
  const local = fields.get("source").value === "local";
  fields.get("symbols").value = (local ? options.symbols : options.synthetic_symbols).slice(0, Math.max(3, options.required_symbols)).join(", ");
  if (local && options.cache_range?.start) {
    fields.get("end").value = options.cache_range.end;
    const last = new Date(`${options.cache_range.end}T00:00:00Z`);
    last.setUTCDate(last.getUTCDate() - 365);
    fields.get("start").value = [last.toISOString().slice(0, 10), options.cache_range.start].sort().at(-1);
  }
  updateBudget();
}
async function post(path, payload) {
  return requestJSON(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": options.csrf_token }, body: JSON.stringify(payload) });
}
async function submit(event) {
  event.preventDefault();
  if (!form.reportValidity()) return;
  submitButton.disabled = true;
  message.textContent = "正在检查数据与窗口预算…";
  try {
    const job = await post("/api/research-jobs", readForm());
    selectedId = (job.job || job).id;
    resultRequest++;
    clearResult("研究正在运行，完成后会显示结果。");
    message.textContent = "研究已提交。每个窗口只用验证期选参数，再独立运行测试期。";
    await refreshJobs();
  } catch (error) {
    if (/already running|busy|正在运行|执行槽/iu.test(error.message)) {
      await refreshJobs();
      const busy = latestJobs.find(active);
      message.textContent = busy ? `执行槽已占用：${statuses[busy.status]} · ${busy.id}。请等待完成或取消此任务后重试。`
        : "已有离线回测占用执行槽。请在回测实验室或研究任务中查看当前状态，等待完成或取消后重试。";
    } else message.textContent = error.message;
  }
  finally { submitButton.disabled = !options.enabled; }
}
function renderJobs(jobs) {
  const displayed = jobs.slice(0, 8);
  // Elapsed time is not displayed here; do not let its changing value replace
  // otherwise identical rows and disrupt keyboard or log inspection.
  const signature = JSON.stringify(displayed.map(({ id, status, parameters, error, logs }) =>
    ({ id, status, parameters, error, logs: logs?.slice(-12) })));
  if (signature === jobsSignature) return;
  const previousRows = Array.from(taskList.children);
  const opened = new Set(previousRows.filter((row) => row.querySelector("details")?.open).map((row) => row.dataset.jobId));
  const focused = document.activeElement;
  const focusedJob = previousRows.find((row) => row.contains(focused))?.dataset.jobId;
  const focusedAction = focused?.dataset?.action;
  const rows = displayed.map((job) => {
    const row = element("article", "job-row research-stack"); row.dataset.jobId = job.id;
    row.append(element("strong", "", `${statuses[job.status] || job.status} · ${job.parameters?.source === "synthetic" ? "合成情景" : "本地历史"}`),
      element("span", "subtle", `${job.id} · ${job.parameters?.symbols?.join(" / ") || ""}`));
    if (job.error) row.append(element("p", "form-message", job.error));
    const controls = element("div", "filter-row");
    const addAction = (action, label, callback) => {
      const control = button(label, callback); control.dataset.action = action; controls.append(control);
    };
    addAction("clone", "复制参数", () => { fillForm(job.parameters); message.textContent = "已复制研究参数，可修改后再次运行。"; form.scrollIntoView({ block: "start", behavior: "smooth" }); });
    if (job.status === "succeeded") addAction("open", "打开结果", () => openResult(job.id));
    if (active(job)) addAction("cancel", "取消任务", async () => {
      try { await post("/api/research-jobs/cancel", { id: job.id }); await refreshJobs(); }
      catch (error) { message.textContent = error.message; }
    });
    row.append(controls);
    if (job.logs?.length) {
      const details = element("details"), summary = element("summary", "", "执行日志");
      details.open = opened.has(job.id);
      details.append(summary, element("pre", "job-log", job.logs.slice(-12).join("\n"))); row.append(details);
    }
    return row;
  });
  taskList.replaceChildren(...(rows.length ? rows : [element("p", "panel-empty", "尚无稳健性研究。先设置一个候选网格，再启动研究。") ]));
  if (focusedJob && focusedAction) {
    const row = rows.find((item) => item.dataset.jobId === focusedJob);
    Array.from(row?.querySelectorAll("button") || []).find((control) => control.dataset.action === focusedAction)?.focus({ preventScroll: true });
  }
  jobsSignature = signature;
}
async function refreshJobs() {
  clearTimeout(polling);
  const request = ++jobsRequest;
  if (!visible()) return;
  let delay = 15000;
  try {
    const payload = await requestJSON("/api/research-jobs", { key: "robust-jobs" });
    if (request !== jobsRequest || !visible()) return;
    const jobs = payload.jobs || [];
    latestJobs = jobs;
    delay = jobs.some(active) ? 2500 : 15000;
    renderJobs(jobs);
    const completed = jobs.find((job) => job.id === selectedId && job.status === "succeeded");
    if (completed && currentResult?.id !== selectedId) await openResult(selectedId);
  } catch (error) {
    if (error.name !== "AbortError" && request === jobsRequest && visible()) {
      message.textContent = error.message;
    }
  } finally {
    // Only the newest visible refresh owns a timer, including after an
    // awaited result load. Late responses cannot resurrect hidden polling.
    if (request === jobsRequest && visible()) polling = setTimeout(refreshJobs, delay);
  }
}
function heatmap(result) {
  const section = panel("参数热力图 · 样本外累计收益", "按入场 / 退出窗口排列所有候选；测试期结果仅作诊断，不参与当期参数选择。");
  const scroll = element("div", "table-scroll"), grid = element("table"), header = element("thead"), head = element("tr");
  scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "参数热力图，左右滚动查看退出窗口");
  head.append(element("th", "", "入场 ↓ / 退出 →"));
  result.parameters.exit_windows.forEach((value) => head.append(element("th", "", String(value))));
  header.append(head);
  const body = element("tbody");
  result.parameters.entry_windows.forEach((entry) => {
    const row = element("tr"); row.append(element("th", "", String(entry)));
    result.parameters.exit_windows.forEach((exit) => {
      const candidate = result.heatmap.find((item) => item.entry_window === entry && item.exit_window === exit);
      const value = candidate?.total_return;
      const cell = element("td", Number.isFinite(value) ? (value > 0 ? "heat-positive" : value < 0 ? "heat-negative" : "") : "", formatPercent(value));
      cell.title = `${candidate?.test_windows || 0} 个测试窗口 / ${candidate?.sample_size || 0} 个收益样本`;
      row.append(cell);
    });
    body.append(row);
  });
  grid.append(header, body); scroll.append(grid); section.append(scroll); return section;
}
function renderResult(result) {
  resultArea.replaceChildren();
  const summary = panel("样本外研究结果", `${result.parameters.source === "synthetic" ? "合成情景 · 非真实行情" : "本地历史数据"} · ${result.data.effective_start.slice(0, 10)} — ${result.data.effective_end.slice(0, 10)} · ${result.data.bars} 根日线`);
  const metrics = element("div", "analysis-stats");
  const stability = result.procedure.selection_stability || {};
  stats(metrics, [
    ["选参规则 OOS 收益", formatPercent(result.procedure.total_return), "各测试窗口按所选参数独立执行"],
    ["完成测试窗口", `${result.windows.length} / ${result.parameters.windows}`, "参数不足或无有效验证分数时跳过"],
    ["正收益窗口", `${stability.positive_windows ?? 0} / ${result.windows.length}`, "按窗口统计，零收益不计为正"],
    ["训练 / 验证一致率", formatPercent(stability.train_validation_agreement), "两段选出的参数是否相同"],
    ["参数切换次数", formatNumber(stability.selection_switches), "相邻有效窗口之间"],
    ["OOS 收益样本", formatNumber(result.procedure.sample_size), "不填补测试窗口之间的间隔"],
  ]);
  summary.append(metrics);
  const observations = element("p", "panel-description", `选择标准：${result.selection_metric === "SharpeRatio" ? "验证期夏普比率" : "验证期总收益"}。各窗口以相同本金开始，结束时平仓；曲线仅连接测试窗口边界。`);
  summary.append(observations);
  if (!result.windows.length) summary.append(element("p", "form-message", "没有可评估的测试窗口。请查看跳过原因与执行日志；本次研究不提供有效收益结论。"));
  if (result.identity_check?.identity_changed) summary.append(element("p", "form-message", "运行期间代码或数据标识发生变化，统计证据标记为无效。请在环境稳定后重新运行。"));
  const plot = element("div"); plot.id = "robustCurve"; summary.append(plot);
  drawCurve(plot, result);
  resultArea.append(summary, heatmap(result));
  const windows = panel("滚动窗口", "训练段用于诊断，验证段选参；测试段在选参后独立执行。训练和验证阶段的策略状态连续，每个测试窗口重新初始化。");
  const windowTable = element("div");
  table(windowTable, [
    { key: "window", label: "窗口", format: (value) => value + 1 },
    { key: "train_start", label: "训练起点", format: (value) => value.slice(0, 10) },
    { key: "validation_start", label: "验证起点", format: (value) => value.slice(0, 10) },
    { key: "purged_bars", label: "隔离日数" },
    { key: "test_start", label: "测试区间", format: (value, row) => `${value.slice(0, 10)} → ${row.test_end.slice(0, 10)}` },
    { key: "selected", label: "验证期所选参数" },
    { key: "test_return", label: "OOS 收益", format: formatPercent },
    { key: "test_trades", label: "成交笔数" },
    { key: "selection_agrees", label: "训练 / 验证一致", format: (value) => value ? "是" : "否" },
  ], result.windows); windows.append(windowTable); resultArea.append(windows);
  const scores = panel("候选得分明细", "逐窗口对照训练、验证与测试表现。测试得分不参与参数排序；所有候选均保留。");
  const scoreTable = element("div"), scoreRows = result.windows.flatMap((window) => Object.entries(window.scores).map(([name, row]) => ({
    window: window.window + 1, name, train: row.train_score, validation: row.validation_score,
    test: window.test_scores[name], selected: window.selected === name,
  })));
  const scoreFormat = result.selection_metric === "TotalReturn" ? formatPercent : formatNumber;
  table(scoreTable, [{ key: "window", label: "窗口" }, { key: "name", label: "参数" },
    { key: "train", label: "训练得分", format: scoreFormat }, { key: "validation", label: "验证得分", format: scoreFormat },
    { key: "test", label: "测试得分", format: scoreFormat }, { key: "selected", label: "选中", format: (value) => value ? "✓" : "—" }], scoreRows);
  scores.append(scoreTable); resultArea.append(scores);
  const notes = panel("研究方法与归档", "这是一组历史滚动检验，不代表独立未见样本，也不自动改变策略准入。");
  const reasons = { insufficient_warmup_history: "起始窗口保留为指标预热历史", no_candidate_scored_on_validation: "验证期没有可计算的候选得分（例如零方差夏普）", candidate_failure_preserved_family_incomplete: "候选运行失败，候选族不完整" };
  result.skipped_windows.forEach((row) => notes.append(element("p", "subtle", `窗口 ${row.window + 1}：${reasons[row.reason] || row.reason}`)));
  notes.append(element("p", "subtle", `预热 ${result.data.warmup_bars} 根 · 滚动步长 ${result.data.step_bars} 根 · 每个测试窗口 ${result.parameters.test_bars} 根。测试窗口之间的空档不计收益。`),
    element("p", "subtle", `配置 SHA-256：${result.config_sha256}`),
    element("p", "subtle", "完整研究结果、候选注册、运行日志与实际使用的数据快照保存在本次研究目录。"));
  notes.append(button("导出研究 JSON", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    const anchor = element("a"); anchor.href = url; anchor.download = `${result.id}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  })); resultArea.append(notes);
}
async function openResult(id) {
  selectedId = id;
  const request = ++resultRequest;
  clearResult("正在加载所选研究结果…");
  message.textContent = "正在加载研究结果…";
  try {
    const result = await requestJSON(`/api/research-result?id=${encodeURIComponent(id)}`, { key: "robust-result" });
    if (selectedId !== id || request !== resultRequest) return;
    currentResult = result; renderResult(result); message.textContent = `已打开 ${id}`;
  } catch (error) {
    if (error.name !== "AbortError" && request === resultRequest && selectedId === id) {
      clearResult(`研究结果读取失败：${error.message}`);
      message.textContent = error.message;
    }
  }
}

async function initializeRobust() {
  const root = document.getElementById("robustContent");
  if (!root) return;
  options = await requestJSON("/api/research-options", { key: "robust-options" });
  root.replaceChildren(); root.classList.add("research-stack");
  const setup = panel("注册一次滚动研究", "选择 3–6 个趋势突破参数组合，在最多 4 个窗口中运行真实回测引擎。研究任务与普通回测共享一个执行槽。");
  form = element("form", "research-stack robust-form"); form.id = "robustForm";
  const defaults = options.defaults;
  const execution = element("details", "advanced-settings"); execution.id = "robustExecutionSettings";
  execution.append(element("summary", "", "调整本金、滑点与随机种子"),
    pair(field("capital", "每窗口初始资金", "number", defaults.capital, { min: 100, max: 1000000000 }), field("slippage_bps", "滑点（bps）", "number", defaults.slippage_bps, { min: 0, max: 100, step: .1 })),
    field("seed", "随机种子", "number", defaults.seed, { min: 0, max: 4294967295 }));
  form.append(
    formSection(1, "数据范围", "选择品种与历史区间，合成情景用于功能验证。",
      pair(field("source", "数据源", "select"), field("symbols", "品种（逗号分隔）", "text", defaults.symbols.join(", "), { maxlength: 100 })),
      pair(field("start", "数据起始日", "date", defaults.start), field("end", "数据截止日", "date", defaults.end))),
    formSection(2, "参数搜索", "组合 3–6 组候选参数，仅使用验证期得分进行选择。",
      pair(field("entry_windows", "入场窗口候选（最多 3 个）", "text", defaults.entry_windows.join(", "), { maxlength: 30 }), field("exit_windows", "退出窗口候选（最多 2 个）", "text", defaults.exit_windows.join(", "), { maxlength: 20 })),
      field("selection_metric", "验证期选择标准", "select")),
    formSection(3, "滚动窗口", "按训练、验证、隔离、测试的顺序推进，测试窗口互不重叠。",
      pair(field("train_bars", "训练日数", "number", defaults.train_bars, { min: 30, max: 180 }), field("validation_bars", "验证日数", "number", defaults.validation_bars, { min: 10, max: 90 })),
      pair(field("test_bars", "测试日数", "number", defaults.test_bars, { min: 10, max: 90 }), field("purge_bars", "验证 / 测试隔离日数", "number", defaults.purge_bars, { min: 1, max: 30 })),
      field("windows", "测试窗口数", "number", defaults.windows, { min: 1, max: 4 })),
    formSection(4, "执行设置", "每个测试窗口使用相同的初始资金；手续费与风控沿用配置快照。", execution));
  populate(fields.get("source"), options.sources, defaults.source);
  populate(fields.get("selection_metric"), options.selection_metrics, defaults.selection_metric);
  fields.get("source").addEventListener("change", sourceChanged);
  budget = element("p", "panel-description robust-form-wide"); budget.id = "robustBudget";
  submitButton = element("button", "primary-button", "启动稳健性研究"); submitButton.type = "submit"; submitButton.disabled = !options.enabled;
  message = element("p", "form-message robust-form-wide", options.enabled ? "每次运行保存独立配置和数据快照。合成情景用于功能验证。" : "本地研究任务已禁用。");
  message.id = "robustMessage"; message.setAttribute("role", "status");
  const actions = element("div", "filter-row robust-form-wide");
  actions.append(submitButton, button("刷新任务", async () => {
    try { options = await requestJSON("/api/research-options", { key: "robust-options" }); submitButton.disabled = !options.enabled; await refreshJobs(); }
    catch (error) { message.textContent = error.message; }
  }));
  form.append(budget, actions, message); form.addEventListener("input", updateBudget); form.addEventListener("submit", submit); setup.append(form);
  // Native validation must be able to reveal and focus invalid controls even
  // if the user closed the execution settings after editing them.
  form.addEventListener("invalid", ({ target }) => {
    if (["capital", "slippage_bps", "seed"].some((key) => fields.get(key) === target)) execution.open = true;
  }, true);
  const tasks = panel("研究任务", "最近 8 次研究；完整记录可在实验历史中检索。"); taskList = element("div", "research-stack"); taskList.id = "robustJobs"; tasks.append(taskList);
  resultArea = element("div", "research-stack"); resultArea.id = "robustResults";
  root.append(setup, tasks, resultArea); updateBudget();
  document.addEventListener("dashboard:view", ({ detail }) => { if (detail.view === "robust") refreshJobs(); else stopPolling(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPolling(); else if (document.documentElement.dataset.view === "robust") refreshJobs(); });
  document.addEventListener("dashboard:research-open", ({ detail }) => { if (detail?.id) openResult(detail.id); });
  let resizeFrame;
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(() => {
      const chart = document.getElementById("robustCurve");
      if (chart && currentResult && document.documentElement.dataset.view === "robust") drawCurve(chart, currentResult);
    });
  }).observe(root);
  await refreshJobs();
}

export function initRobust() {
  initialization ||= initializeRobust().catch((error) => { initialization = null; throw error; });
  return initialization;
}
