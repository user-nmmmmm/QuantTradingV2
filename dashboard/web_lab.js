import { requestJSON, el, element, formatNumber } from "./api.js";
import { lineChart, table, stats, formatValue, valueType } from "./charts.js";
import { mutate } from "./strategy.js";

let reportId;
let closedPage = 1;
let diagnosticSequence = 0;
let historyOffset = 0;
let historyTotal = 0;
let comparison;
let quickCompare;
const selections = new Map();
const expandedDiagnostics = new Set();
const statusLabels = { succeeded: "已完成", running: "运行中", queued: "等待运行", failed: "失败", cancelled: "已取消", timed_out: "已超时", interrupted: "重启中断" };
const valueText = (value) => value == null ? "未记录" : typeof value === "object" ? JSON.stringify(value) : String(value);
const canClone = (parameters) => ["source", "symbols", "start", "end", "capital", "slippage_bps", "seed"].every((key) => parameters[key] !== undefined && parameters[key] !== null);
const statLabels = { closed_trades: "完整交易数", win_rate: "交易胜率", profit_factor: "盈利因子", expectancy: "每笔期望盈亏", avg_win: "平均盈利", avg_loss: "平均亏损", net_pnl: "已平仓净盈亏", commission: "手续费", slippage: "滑点成本", mean_holding_hours: "平均持仓时间", mean_mae: "平均 MAE", mean_mfe: "平均 MFE", max_drawdown_days: "最大深度回撤时长", underwater_ratio: "回撤期间占比", sharpe_ratio: "Sharpe", sortino_ratio: "Sortino", calmar_ratio: "Calmar", annualized_volatility: "年化波动率", fee_return_ratio: "手续费 / 毛盈利", time_in_market_ratio: "在场时间占比", mean_gross_leverage: "平均总杠杆", max_gross_leverage: "最大总杠杆", turnover_ratio: "换手率" };
const primaryMetrics = ["closed_trades", "win_rate", "profit_factor", "expectancy", "net_pnl", "sharpe_ratio"];
const metricGroups = [
  ["交易表现", ["avg_win", "avg_loss", "mean_holding_hours", "mean_mae", "mean_mfe"]],
  ["成本与回撤", ["commission", "slippage", "fee_return_ratio", "max_drawdown_days", "underwater_ratio"]],
  ["风险与敞口", ["sortino_ratio", "calmar_ratio", "annualized_volatility", "time_in_market_ratio", "mean_gross_leverage", "max_gross_leverage", "turnover_ratio"]],
];
const metricStates = { insufficient: "样本不足", insufficient_data: "样本不足", partial: "部分有效样本", unknown: "未记录", not_recorded: "未记录", undefined: "不适用", not_modeled: "未建模", excluded: "已排除", invalid_input: "数据无效" };
function disclosure(title, key, render, cls = "diagnostic-disclosure") {
  const details = element("details", cls);
  details.append(element("summary", "", title));
  let built = false;
  const reveal = () => { if (details.open && !built) { details.append(...render()); built = true; } };
  details.open = expandedDiagnostics.has(key);
  details.addEventListener("toggle", () => {
    if (details.open) expandedDiagnostics.add(key); else expandedDiagnostics.delete(key);
    reveal();
  });
  reveal(); return details;
}
function metricEntries(data, keys) {
  return keys.filter((key) => key in (data.stats || {})).map((key) => {
    const state = data.metric_status?.[key], type = valueType(key);
    const status = metricStates[state?.status] || (data.stats[key] == null ? "未记录或不适用" : "");
    const sample = state?.sample_size == null ? "" : `${formatValue(state.sample_size, { type: "count" })} 个样本`;
    return [statLabels[key], data.stats[key], [status, sample].filter(Boolean).join(" · "), { key, type, unit: type === "money" ? "USDT" : ["mean_mae", "mean_mfe"].includes(key) ? "原报告单位" : "" }];
  });
}
function diagnosticMetrics(data) {
  const primary = element("div", "diagnostic-primary analysis-grid");
  stats(primary, metricEntries(data, primaryMetrics));
  const extraCount = metricGroups.reduce((count, [, keys]) => count + keys.filter((key) => key in (data.stats || {})).length, 0);
  const advanced = disclosure(`深度指标与统计依据 · ${extraCount} 项`, "metrics", () => {
    const groups = element("div", "diagnostic-groups");
    metricGroups.forEach(([label, keys]) => {
      const entries = metricEntries(data, keys); if (!entries.length) return;
      const section = element("section", "diagnostic-group"), grid = element("div", "analysis-grid");
      section.append(element("h4", "", label), grid); stats(grid, entries); groups.append(section);
    });
    const evidence = disclosure("查看每项指标的来源与状态", "metric-evidence", () => {
      const body = element("div");
      table(body, [{ key: "label", label: "指标" }, { key: "status", label: "状态" }, { key: "sample_size", label: "样本数" }, { key: "source", label: "记录来源" }, { key: "reason", label: "说明" }], Object.keys(statLabels).filter((key) => key in (data.stats || {})).map((key) => ({ ...data.metric_status?.[key], label: statLabels[key], status: metricStates[data.metric_status?.[key]?.status] || data.metric_status?.[key]?.status || "未记录" })));
      return [body];
    });
    return [groups, evidence];
  }, "diagnostic-advanced");
  el("diagnosticStats").replaceChildren(primary, advanced);
}
function button(label, handler, cls = "text-button") { const item = element("button", cls, label); item.type = "button"; item.addEventListener("click", handler); return item; }
function clearDiagnostics(text) {
  reportId = null; diagnosticSequence++; expandedDiagnostics.clear();
  el("diagnosticContext").textContent = text;
  ["diagnosticStats", "diagnosticFunnel", "diagnosticDetails", "closedTrades"].forEach((id) => el(id).replaceChildren());
  el("noTradeReason").textContent = ""; el("closedCount").textContent = "—"; el("closedPage").textContent = "—";
  el("closedPrevious").disabled = true; el("closedNext").disabled = true;
}
async function diagnostics() {
  if (!reportId) return;
  const id = reportId, sequence = ++diagnosticSequence;
  el("diagnosticContext").textContent = `正在读取 ${id} 的诊断…`;
  el("closedPrevious").disabled = true; el("closedNext").disabled = true;
  try {
    const data = await requestJSON(`/api/backtest-diagnostics?id=${encodeURIComponent(id)}&page=${closedPage}`, { key: "diagnostics" });
    if (sequence !== diagnosticSequence) return;
    el("diagnosticContext").textContent = `${id} · 完整交易与成交记录分开统计`;
    diagnosticMetrics(data);
    const executed = data.funnel?.stages?.find((stage) => stage.id === "execution");
    const zeroStage = data.funnel?.stages?.find((stage) => stage.id === data.no_trade?.stage);
    el("noTradeReason").textContent = data.no_trade?.status === "entries_observed" ? `报告记录了 ${executed?.count} 条已成交入场链。入场链与完整交易属于不同统计口径。` : data.no_trade?.status === "no_entry_fills_recorded" ? `未记录已成交入场链；最早明确为零的已记录环节是“${zeroStage?.label || "执行"}”。未记录环节仍为未知，仅凭零计数不能确定根因。` : "缺少入场成交证据。没有已平仓交易，不等于没有入场，也不能据此确定阻断原因。";
    for (const blocker of data.no_trade?.blockers || []) {
      const fact = element("span", "diagnostic-blocker", `已记录控制：${blocker.reason || blocker.label || blocker.id || JSON.stringify(blocker)}`);
      fact.title = valueText(blocker); el("noTradeReason").append(fact);
    }
    el("diagnosticFunnel").replaceChildren(...(data.funnel?.stages || []).map((stage) => {
      const card = element("div", `funnel-stage ${stage.status}`);
      card.append(element("strong", "", stage.label), element("b", "", stage.count == null ? "未知" : formatNumber(stage.count)), element("p", "", stage.count == null ? "此报告未记录该环节" : "已记录计数；零值不等同于因果结论"));
      if (stage.source) card.append(disclosure("记录依据", `funnel-${stage.id}`, () => [element("p", "", stage.source)], "funnel-evidence")); return card;
    }));
    const details = el("diagnosticDetails"); details.replaceChildren();
    details.append(disclosure(`退出归因 · ${(data.exit_reasons || []).length} 类`, "exits", () => {
      const exits = element("div");
      table(exits, [{ key: "reason", label: "退出原因" }, { key: "count", label: "交易数" }, { key: "net_pnl", label: "净盈亏 · USDT" }], data.exit_reasons || []);
      return [exits];
    }));
    const events = data.drawdowns || [];
    details.append(disclosure(`回撤事件 · ${events.length} 次`, "drawdowns", () => {
      const drawdowns = element("div");
      table(drawdowns, [
        { key: "peak", label: "峰值时间" }, { key: "trough", label: "谷值时间" }, { key: "recovery", label: "恢复时间" },
        { key: "depth_pct", label: "回撤幅度" }, { key: "depth_amount", label: "回撤金额 · USDT" },
        { key: "duration_days", label: "持续时间" }, { key: "duration_periods", label: "观测周期" },
        { key: "recovery_days", label: "恢复时长" }, { key: "is_open", label: "恢复状态", type: "text", format: (value) => value === true ? "尚未恢复" : value === false ? "已恢复" : "—" },
      ], events); return [drawdowns];
    }));
    const concentration = element("p", "", `收益集中度 HHI：${formatValue(data.concentration?.profit_hhi, { type: "ratio" })}。数值越高表示盈利更集中；不足样本不估算。`);
    concentration.title = `原始值：${valueText(data.concentration?.profit_hhi)}`; details.append(concentration);
    const methods = data.methodology;
    const methodRows = Array.isArray(methods) ? methods : typeof methods === "object" && methods ? Object.values(methods) : [methods];
    (data.warnings || []).forEach((text) => details.append(element("p", "diagnostic-warning", valueText(text))));
    details.append(disclosure("计算口径与原始说明", "methodology", () => [
      element("p", "", "胜率按盈利完整交易数 / 有效完整交易数计算；盈利因子为盈利之和 / 亏损绝对值之和，没有亏损样本时不估算。持仓时间按开平仓 UTC 时差计算。滑点可能已包含在成交价盈亏中，不重复扣减。MAE / MFE 保留原报告单位。"),
      ...methodRows.filter(Boolean).map((text) => element("p", "", valueText(text))),
    ]));
    const trades = data.closed_trades || {};
    const preferred = ["symbol", "strategy", "entry_time", "exit_time", "side", "net_pnl", "pnl", "holding_hours", "exit_reason", "commission", "slippage", "mae", "mfe"];
    const columns = [...preferred.filter((key) => trades.columns?.includes(key)), ...(trades.columns || []).filter((key) => !preferred.includes(key))];
    table(el("closedTrades"), columns.map((key) => ({ key, label: ({ symbol: "标的", strategy: "策略", strategy_id: "策略标识", position_id: "持仓标识", entry_time: "开仓时间 · UTC", exit_time: "平仓时间 · UTC", side: "方向", net_pnl: "净盈亏 · USDT", gross_pnl: "毛盈亏 · USDT", holding_hours: "持仓时间", exit_reason: "退出原因", commission: "手续费 · USDT", slippage: "滑点 · USDT", entry_price: "入场价格", exit_price: "退出价格", qty: "数量", mae: "MAE · 原单位", mfe: "MFE · 原单位", legs: "成交腿数", cost_semantics: "成本口径" })[key] || key })), trades.rows || []);
    el("closedCount").textContent = trades.available ? `${trades.total} 笔完整交易` : "报告未保存完整交易";
    el("closedPage").textContent = trades.pages ? `${trades.page} / ${trades.pages} 页` : "无完整交易";
    el("closedPrevious").disabled = closedPage <= 1; el("closedNext").disabled = closedPage >= (trades.pages || 0);
  } catch (error) { if (error.name !== "AbortError" && sequence === diagnosticSequence) clearDiagnostics(`诊断读取失败：${error.message}`); }
}

function selectionState() {
  el("compareSelection").textContent = selections.size ? `已选 ${selections.size} / 4：${[...selections.values()].join("、")}` : "在档案中选择 2–4 份已完成的回测报告";
  el("compareRun").disabled = selections.size < 2 || selections.size > 4;
  if (quickCompare) { quickCompare.disabled = el("compareRun").disabled; quickCompare.textContent = `比较所选实验（${selections.size} / 4）→`; }
}
async function cloneExperiment(parameters) {
  location.hash = "backtest";
  const module = await import("./research.js"); await module.initResearch();
  document.dispatchEvent(new CustomEvent("dashboard:clone-experiment", { detail: parameters }));
}
function metadataEditor(item, root) {
  const details = element("details"); details.append(element("summary", "", "编辑名称、标签与研究笔记"));
  const fields = {};
  for (const [key, label, value, max] of [["name", "实验名称", item.name || "", 80], ["tags", "标签 · 逗号分隔", (item.tags || []).join(", "), 400], ["notes", "研究笔记", item.notes || "", 4000]]) {
    const input = element(key === "notes" ? "textarea" : "input"); input.value = value; input.maxLength = max;
    const field = element("label", "field", label); field.append(input); fields[key] = input; details.append(field);
  }
  const feedback = element("p", "form-message"); feedback.setAttribute("role", "status");
  const save = button("保存记录", async () => {
    save.disabled = true;
    try { await mutate("/api/experiments/metadata", { id: item.id, name: fields.name.value.trim(), tags: fields.tags.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean), notes: fields.notes.value, favorite: Boolean(item.favorite) }); feedback.textContent = "研究记录已保存"; await history(); }
    catch (error) { feedback.textContent = error.message; } finally { save.disabled = false; }
  }, "secondary-button");
  details.append(save, feedback); root.append(details);
}
function historyCard(item) {
  const root = element("article", "history-item");
  const heading = element("div", "history-item-heading"), copy = element("div");
  copy.append(element("h3", "", item.name || item.run_id || item.id));
  const p = item.parameters || {};
  copy.append(element("p", "", `${p.source === "synthetic" ? "合成数据" : p.source === "local" ? "本地历史行情" : "历史报告"} · ${(p.symbols || []).join(" / ") || "标的未记录"} · ${p.start || "—"} → ${p.end || "—"}`));
  heading.append(copy, element("span", `job-status ${item.status}`, statusLabels[item.status] || item.status)); root.append(heading);
  if (item.error) root.append(element("p", "error", item.error));
  if (item.notes) root.append(element("p", "", item.notes));
  const tags = element("div", "tag-list"); (item.tags || []).forEach((tag) => tags.append(element("span", "tag", tag))); root.append(tags);
  if (item.config_sha256 || item.config_diff?.length) {
    const identity = element("details", "methodology");
    identity.append(element("summary", "", "查看本次策略与配置身份"), element("p", "", `策略：${valueText(item.strategy || p.strategy)}。配置 SHA256：${item.config_sha256 || "未记录"}。基础配置 SHA256：${item.base_config_sha256 || "未记录"}。`));
    identity.append(element("pre", "strategy-diff", item.config_diff?.length ? item.config_diff.map((row) => `${row.path}：${valueText(row.before)} → ${valueText(row.after)}`).join("\n") : "本次运行未覆盖基础配置。"));
    root.append(identity);
  }
  const actions = element("div", "history-actions");
  const runId = item.run_id || item.id;
  if (item.status === "succeeded" && (!item.kind || item.kind === "backtest")) {
    const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.checked = selections.has(runId);
    const label = element("label", "checkbox-field"); label.append(checkbox, element("span", "", "加入对比"));
    checkbox.addEventListener("change", () => { if (checkbox.checked && selections.size >= 4) { checkbox.checked = false; el("historyMessage").textContent = "一次最多比较 4 份报告。"; return; } if (checkbox.checked) selections.set(runId, item.name || runId); else selections.delete(runId); selectionState(); });
    actions.append(label, button("打开报告 ↗", async () => { location.hash = "backtest"; const module = await import("./research.js"); await module.initResearch(); document.dispatchEvent(new CustomEvent("dashboard:backtest-complete", { detail: { run_id: runId } })); }));
  }
  if (canClone(p) && (!item.kind || item.kind === "backtest")) actions.append(button("复制参数再运行", () => cloneExperiment(p)));
  if (item.kind === "robust" && item.status === "succeeded") actions.append(button("打开稳健性报告 ↗", async () => { location.hash = "robust"; const module = await import("./robust.js"); await module.initRobust(); document.dispatchEvent(new CustomEvent("dashboard:research-open", { detail: { id: runId } })); }));
  actions.append(button(item.favorite ? "★ 已收藏" : "☆ 收藏", async () => {
    try { await mutate("/api/experiments/metadata", { id: item.id, favorite: !item.favorite }); await history(); }
    catch (error) { el("historyMessage").textContent = error.message; }
  }));
  root.append(actions); metadataEditor(item, root); return root;
}
async function history() {
  const params = new URLSearchParams({ q: el("historySearch").value, status: el("historyStatus").value, tag: el("historyTag").value, limit: "15", offset: String(historyOffset) });
  if (el("historyFavorite").checked) params.set("favorite", "true");
  el("historyMessage").textContent = "正在读取实验档案…";
  try {
    const data = await requestJSON(`/api/experiments?${params}`, { key: "experiment-history" });
    historyTotal = data.total;
    el("historyList").replaceChildren(...data.items.map(historyCard));
    el("historyMessage").textContent = `${data.total} 个实验 · 档案与研究笔记保存在本机`;
    if (!data.items.length) el("historyList").append(element("p", "panel-empty", "当前筛选条件没有实验记录。"));
    el("historyPrevious").disabled = historyOffset === 0; el("historyNext").disabled = historyOffset + 15 >= historyTotal;
    el("historyPage").textContent = `${historyTotal ? Math.floor(historyOffset / 15) + 1 : 0} / ${Math.ceil(historyTotal / 15)} 页`;
  } catch (error) { if (error.name !== "AbortError") el("historyMessage").textContent = `档案读取失败：${error.message}`; }
}
function drawComparison() {
  if (!comparison) return;
  for (const [id, key, percent] of [["compareNav", "nav", false], ["compareDrawdown", "drawdown", true]]) {
    lineChart(el(id), comparison.runs.map((run, index) => ({ name: `${index + 1}. ${selections.get(run.id) || run.id}`, points: run.points.map((point) => ({ timestamp: point.timestamp, value: point[key] })) })), { percent, label: key === "nav" ? "多实验净值对比" : "多实验回撤对比" });
  }
}
async function compare() {
  const ids = [...selections.keys()]; if (ids.length < 2) return;
  el("compareRun").disabled = true; el("compareMessage").textContent = "正在对齐并比较报告…";
  try {
    comparison = await requestJSON(`/api/compare?${new URLSearchParams({ ids: ids.join(",") })}`, { key: "compare" });
    const names = { source: "数据来源", symbols: "交易标的", start: "开始日期", end: "结束日期", capital: "初始资金", slippage_bps: "滑点 bps", seed: "随机种子", strategy: "策略配置", base_config_sha256: "基础配置 SHA256", config_sha256: "实验配置 SHA256" };
    const fields = [["total_return", "总收益"], ["annualized_return", "年化收益"], ["max_drawdown", "最大回撤"], ["end_equity", "期末权益 · USDT"], ["fills_count", "成交记录"]];
    const metricRows = fields.map(([key, label]) => Object.fromEntries([
      ["metric", label], ["valueType", valueType(key)],
      ...comparison.runs.map((run, i) => [String(i), run.metrics[key]]),
    ]));
    table(el("compareMetrics"), [{ key: "metric", label: "指标" }, ...comparison.runs.map((run, i) => ({ key: String(i), label: `${i + 1}. ${run.id}`, type: (row) => row.valueType }))], metricRows);
    const details = el("compareDifferences"); details.replaceChildren(); const diff = element("div"); details.append(diff);
    table(diff, [{ key: "field", label: "参数" }, ...comparison.runs.map((run, i) => ({ key: String(i), label: `${i + 1}. ${run.id}` }))], comparison.differences.map((item) => Object.fromEntries([["field", names[item.field] || item.field], ...item.values.map((value, i) => [String(i), valueText(value)])])));
    details.append(element("p", "", "净值以各自首个有效权益点归一化为 100；按日历时间绘制，不填补缺失日期。统计使用全部数据，图表采样保留最深回撤。旧报告可能缺少费用或完整配置记录。"));
    el("compareMessage").textContent = comparison.warnings.length ? `存在 ${comparison.warnings.length} 项口径差异或缺失，请展开参数差异核对。${comparison.runs.map((run, i) => `${i + 1}：${run.period_start.slice(0, 10)} → ${run.period_end.slice(0, 10)}`).join("；")}` : "数据来源、标的、滑点与观察区间一致；请结合参数差异解读结果。";
    drawComparison();
  } catch (error) { if (error.name !== "AbortError") { comparison = null; ["compareNav", "compareDrawdown", "compareMetrics", "compareDifferences"].forEach((id) => el(id).replaceChildren()); el("compareMessage").textContent = error.message; } }
  finally { selectionState(); }
}
export function initLab() {
  document.addEventListener("dashboard:backtest-loading", () => clearDiagnostics("正在切换报告…"));
  document.addEventListener("dashboard:backtest-error", () => clearDiagnostics("报告不可用"));
  document.addEventListener("dashboard:backtest-loaded", ({ detail }) => { if (reportId !== detail.id) expandedDiagnostics.clear(); reportId = detail.id; closedPage = 1; diagnostics(); });
  document.dispatchEvent(new CustomEvent("dashboard:backtest-current"));
  el("closedPrevious").addEventListener("click", () => { closedPage--; diagnostics(); });
  el("closedNext").addEventListener("click", () => { closedPage++; diagnostics(); });
  el("historyFilters").addEventListener("submit", (event) => { event.preventDefault(); historyOffset = 0; history(); });
  el("historyRefresh").addEventListener("click", history);
  el("historyPrevious").addEventListener("click", () => { historyOffset = Math.max(0, historyOffset - 15); history(); });
  el("historyNext").addEventListener("click", () => { if (historyOffset + 15 < historyTotal) { historyOffset += 15; history(); } });
  el("compareRun").addEventListener("click", compare);
  quickCompare = button("比较所选实验（0 / 4）→", async () => { await compare(); if (comparison) el("compareNav").closest("section").scrollIntoView({ behavior: "smooth", block: "start" }); }, "secondary-button");
  quickCompare.disabled = true; el("historyFilters").append(quickCompare);
  document.addEventListener("dashboard:view", ({ detail }) => { if (detail.view === "experiments") { history(); drawComparison(); } });
  let frame;
  window.addEventListener("resize", () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { if (document.documentElement.dataset.view === "experiments") drawComparison(); }); });
  if (document.documentElement.dataset.view === "experiments") history();
}
