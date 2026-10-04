"use strict";

import { requestJSON } from "/assets/api.js";

const REFRESH_SECONDS = 15;
const STALE_SECONDS = 300;
let secondsUntilRefresh = REFRESH_SECONDS;
let currentData = null;
let currentFilter = "all";
let fetching = false;
let receivedAt = 0;
const sectionVersions = new Map();

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const quantityFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });

function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function money(value) { return finite(value) ? numberFormat.format(value) : "—"; }
function quantity(value) { return finite(value) ? quantityFormat.format(value) : "—"; }
function text(value, fallback = "—") { return value === null || value === undefined || value === "" ? fallback : String(value); }
function set(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
function node(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined) item.textContent = value;
  return item;
}
function dateLabel(value, dateOnly = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value);
  return new Intl.DateTimeFormat("zh-CN", dateOnly ? {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  } : {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(date);
}
function ageLabel(seconds) {
  if (!finite(seconds)) return "更新时间未知";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}
function object(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
function isStale(data) { return !finite(data.snapshot_age_seconds) || data.snapshot_age_seconds > STALE_SECONDS; }

function renderHeader(data) {
  const valid = data.status_valid === true;
  const stale = valid && isStale(data);
  const ageKnown = finite(data.snapshot_age_seconds);
  const demo = data.mode === "demo";
  $("demoBanner").hidden = !demo;
  set("updatedAt", valid ? `最近更新 ${dateLabel(data.timestamp)}` : "最近更新 —");
  set("syncStatus", !valid ? "快照不可用" : !ageKnown ? "快照时间未确认" : stale ? `快照已过期 · ${ageLabel(data.snapshot_age_seconds)}` : `快照已同步 · ${ageLabel(data.snapshot_age_seconds)}`);
  let state = "账户快照不可用";
  let caption = "请检查状态快照";
  let stateClass = "danger";
  if (valid && stale) { state = ageKnown ? "快照过期" : "时间未确认"; caption = "等待可核验的状态更新"; stateClass = "warning"; }
  else if (valid && data.healthy === false) { state = "需要关注"; caption = text(data.operational_state, "系统报告异常"); stateClass = "danger"; }
  else if (valid && data.healthy === true) { state = "运行正常"; caption = demo ? "演示数据" : text(data.operational_state, "实时监控中"); stateClass = "healthy"; }
  set("systemState", state);
  set("systemCaption", caption);
  $("statusPill").className = `status-pill ${stateClass}`;
}

function renderMetrics(data) {
  const valid = data.status_valid === true;
  const stale = isStale(data);
  const positions = object(data.positions);
  const count = valid ? Object.keys(positions).length : null;
  const gate = object(object(data.details).account_entry_gate);
  $("equityValue").replaceChildren(document.createTextNode(valid ? money(data.equity) : "—"));
  $("cashValue").replaceChildren(document.createTextNode(valid ? money(data.cash) : "—"));
  set("positionValue", count === null ? "—" : String(count));
  set("positionHint", count === null ? "等待有效快照" : `${count} 个交易对`);
  let gateText = "待确认";
  let gateHint = "无账户核验记录";
  let gateTone = "unknown";
  if (!valid) gateHint = "快照不可用";
  else if (stale) gateHint = "快照过期或时间未知";
  else if (gate.allows_new_risk === true) { gateText = "允许"; gateHint = "账户风险门控已通过"; gateTone = "ok"; }
  else if (gate.allows_new_risk === false) { gateText = "已暂停"; gateHint = text(gate.reason, "门控阻断"); gateTone = "bad"; }
  set("gateValue", gateText);
  $("gateValue").className = `metric-value gate-value ${gateTone}`;
  set("gateHint", gateHint);
}

function renderChart(data) {
  const history = data.status_valid === true && Array.isArray(data.history)
    ? data.history.filter((point) => finite(point.equity) && point.timestamp).slice(-120) : [];
  const ready = history.length >= 2;
  $("chartEmpty").hidden = ready;
  $("chartPoint").setAttribute("visibility", ready ? "visible" : "hidden");
  if (!ready) {
    $("chartLine").setAttribute("d", "");
    $("chartArea").setAttribute("d", "");
    ["chartMax", "chartMid", "chartMin", "chartFrom", "chartTo"].forEach((id) => set(id, "—"));
    return;
  }
  const values = history.map((item) => item.equity);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const margin = Math.max((rawMax - rawMin) * 0.17, Math.abs(rawMax) * 0.002, 1);
  const low = rawMin - margin;
  const high = rawMax + margin;
  const points = history.map((item, index) => ({
    x: index * 700 / (history.length - 1),
    y: 20 + (high - item.equity) / (high - low) * 190
  }));
  const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
  const last = points[points.length - 1];
  $("chartLine").setAttribute("d", line);
  $("chartArea").setAttribute("d", `${line} L${last.x.toFixed(2)} 210 L0 210 Z`);
  $("chartPoint").setAttribute("cx", last.x.toFixed(2));
  $("chartPoint").setAttribute("cy", last.y.toFixed(2));
  set("chartMax", quantity(high));
  set("chartMid", quantity((high + low) / 2));
  set("chartMin", quantity(low));
  set("chartFrom", dateLabel(history[0].timestamp, true));
  set("chartTo", dateLabel(history[history.length - 1].timestamp, true));
}

function renderAllocation(data) {
  const equity = data.status_valid ? data.equity : null;
  const cash = data.status_valid ? data.cash : null;
  const comparable = !isStale(data) && finite(equity) && finite(cash) && equity > 0 && cash >= 0 && cash <= equity;
  set("legendCash", comparable ? money(cash) : "—");
  set("legendOther", comparable ? money(equity - cash) : "—");
  set("cashPercent", comparable ? `${Math.round(cash / equity * 100)}%` : "—");
  $("allocationDonut").style.background = comparable
    ? `conic-gradient(var(--accent) 0 ${cash / equity * 100}%, var(--surface-strong) ${cash / equity * 100}% 100%)`
    : "var(--surface-strong)";
  set("allocationNote", comparable
    ? "按权益减现金估算非现金部分；不代表逐仓实时市值。"
    : isStale(data) ? "快照过期或时间未知，未计算当前比例。" : "现金与权益口径暂不可比，未计算比例。");
}

function renderPositions(data) {
  const body = $("positionsBody");
  body.replaceChildren();
  const positions = data.status_valid ? object(data.positions) : {};
  const entries = Object.entries(positions);
  set("positionsCount", data.status_valid && isStale(data) ? `旧快照 · ${entries.length} 个标的` : `${entries.length} 个标的`);
  if (!entries.length) {
    const row = node("tr");
    const cell = node("td", "empty-cell", data.status_valid ? "当前没有持仓记录" : "状态快照不可用，暂不显示持仓");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  const protective = object(object(data.details).protective_orders);
  entries.forEach(([symbol, raw]) => {
    const position = object(raw);
    const row = node("tr");
    const symbolCell = node("td");
    symbolCell.append(node("span", "symbol-avatar", String(symbol).slice(0, 1).toUpperCase()), node("span", "symbol-name", symbol));
    row.append(symbolCell);
    const qty = position.qty;
    const direction = node("span", `direction ${finite(qty) ? qty < 0 ? "short" : qty === 0 ? "flat" : "" : "flat"}`, finite(qty) ? qty < 0 ? "空头" : qty === 0 ? "空仓" : "多头" : "未知");
    const directionCell = node("td"); directionCell.append(direction); row.append(directionCell);
    row.append(node("td", "", quantity(qty)), node("td", "", money(position.avg_price)));
    const protection = object(protective[symbol]);
    const protectText = protection.state ? text(protection.state).toLowerCase() === "active" ? "已保护" : text(protection.state) : "无记录";
    const protectClass = protection.state ? protectText === "已保护" ? "mini-status" : "mini-status warn" : "mini-status muted";
    const protectCell = node("td"); protectCell.append(node("span", protectClass, protectText)); row.append(protectCell);
    body.append(row);
  });
}

function check(label, note, level, state) { return { label, note, level, state }; }
function buildChecks(data) {
  if (!data.status_valid) return [
    check("状态快照", "缺失、损坏或结构无效", "bad", "不可用"),
    check("引擎健康", "无法核验", "unknown", "待确认"),
    check("账户风险门控", "无法核验", "unknown", "待确认"),
    check("订单对账", "无法核验", "unknown", "待确认"),
    check("未知订单", "无法核验", "unknown", "待确认"),
    check("组合熔断", "无法核验", "unknown", "待确认"),
    check("Phase 6 门槛", "无法核验", "unknown", "待确认")
  ];
  const details = object(data.details);
  const gate = object(details.account_entry_gate);
  const recon = object(details.reconciliation);
  const breaker = object(details.portfolio_breaker);
  const stale = isStale(data);
  const unknownOrder = details.unresolved_unknown_order;
  const action = String(breaker.action || "").toLowerCase();
  const phase6 = object(data.phase6_monitoring);
  const checks = [
    check("状态快照", `已更新于 ${ageLabel(data.snapshot_age_seconds)}`, stale ? "bad" : "ok", stale ? "待更新" : "有效"),
    check("引擎健康", text(data.operational_state, "未提供运行状态"), data.healthy === true ? "ok" : "bad", data.healthy === true ? "正常" : "异常"),
    check("账户风险门控", text(gate.reason, "等待独立账户核验"), gate.allows_new_risk === true ? "ok" : gate.allows_new_risk === false ? "bad" : "unknown", gate.allows_new_risk === true ? "通过" : gate.allows_new_risk === false ? "阻断" : "待确认"),
    check("订单对账", recon.last_run_at ? `上次 ${dateLabel(recon.last_run_at, true)} · 差异 ${text(recon.discrepancy_count, "—")}` : "暂无对账记录", recon.ok === true ? "ok" : recon.ok === false ? "bad" : "unknown", recon.ok === true ? "通过" : recon.ok === false ? "差异" : "待确认"),
    check("未知订单", unknownOrder === true ? "存在未解决的未知订单" : unknownOrder === false ? "没有未解决的未知订单" : "暂无订单状态记录", unknownOrder === true ? "bad" : unknownOrder === false ? "ok" : "unknown", unknownOrder === true ? "需处理" : unknownOrder === false ? "无" : "待确认"),
    check("组合熔断", action ? `当前动作 ${action}` : "暂无熔断记录", action === "normal" || action === "none" ? "ok" : action === "reduce" ? "warn" : action ? "bad" : "unknown", action === "normal" || action === "none" ? "正常" : action === "reduce" ? "降风险" : action ? "已触发" : "待确认"),
    check("Phase 6 门槛", phase6.task ? "来自最近一次 Phase 6 监控报告" : "暂无监控报告", phase6.passed === true ? "ok" : phase6.passed === false ? "bad" : "unknown", phase6.passed === true ? "通过" : phase6.passed === false ? "未通过" : "待确认")
  ];
  if (stale) return checks.map((item, index) => index === 0 ? item : check(item.label, "快照过期或时间未知，无法确认当前状态", "unknown", "待确认"));
  return checks;
}
function renderChecks(data) {
  const checks = buildChecks(data);
  const container = $("checksList"); container.replaceChildren();
  checks.forEach((item) => {
    const row = node("div", "check-row");
    row.append(node("span", `check-icon ${item.level}`, item.level === "ok" ? "✓" : item.level === "unknown" ? "·" : "!"));
    const copy = node("div"); copy.append(node("div", "check-label", item.label), node("div", "check-note", item.note)); row.append(copy);
    row.append(node("span", `check-state ${item.level}`, item.state));
    container.append(row);
  });
  const issues = checks.filter((item) => item.level === "bad" || item.level === "warn").length;
  set("checksSummary", issues ? `${issues} 项需关注` : data.status_valid ? "关键检查" : "状态不可用");
}

function renderStrategies(data) {
  const container = $("strategiesList"); container.replaceChildren();
  const entries = data.status_valid && !isStale(data) ? Object.entries(object(object(data.details).strategy_health)) : [];
  if (!entries.length) { container.append(node("div", "panel-empty", data.status_valid && isStale(data) ? "快照过期或时间未知，无法确认当前策略状态" : "当前快照未提供策略生命周期记录")); return; }
  entries.forEach(([name, raw]) => {
    const strategy = object(raw); const status = String(strategy.status || "unknown").toLowerCase();
    const label = { active: "运行中", probation: "观察期", cooldown: "冷却中", manual_lock: "人工锁定" }[status] || text(strategy.status, "待确认");
    const level = status === "active" ? "" : status === "probation" || status === "cooldown" ? "warn" : "bad";
    const row = node("div", "strategy-row"); const copy = node("div");
    copy.append(node("span", "strategy-name", name), node("span", "strategy-sub", finite(strategy.raw_setup_count) ? `原始信号 ${strategy.raw_setup_count} 次` : "生命周期状态"));
    row.append(copy, node("span", `strategy-pill ${level}`, label)); container.append(row);
  });
}

function renderReasons(data) {
  const container = $("reasonsList"); container.replaceChildren();
  const reasons = Array.isArray(data.health_reasons) ? data.health_reasons : [];
  set("reasonCount", `${reasons.length} 条`);
  if (!reasons.length) { container.append(node("div", "panel-empty", data.status_valid ? "当前没有报告健康异常" : "状态文件无效，无法确认系统健康")); return; }
  reasons.slice(0, 5).forEach((raw) => {
    const reason = object(raw); const row = node("div", "reason-row");
    row.append(node("div", "reason-code", `${text(reason.code, "UNKNOWN")} · ${text(reason.subject, "SYSTEM")}`), node("div", "reason-message", text(reason.message, "未提供详细说明")));
    container.append(row);
  });
}

const eventNames = {
  account_sync_failed: "账户同步失败", reconciliation_complete: "对账完成", reconcile_discrepancy: "对账发现差异",
  risk_halt: "风险停止", state_exported: "状态快照已更新", market_data_delay: "行情数据延迟",
  account_reconciliation_persistence_failed: "账户对账记录保存失败"
};
function contextLabel(value) {
  const ctx = object(value);
  if (typeof ctx.message === "string") return ctx.message;
  const entries = Object.entries(ctx).slice(0, 3).map(([key, item]) => `${key}: ${Array.isArray(item) ? item.join(", ") : typeof item === "object" ? JSON.stringify(item) : text(item)}`);
  return entries.join(" · ").slice(0, 180) || "无附加信息";
}
function renderAlerts() {
  const container = $("alertsList"); container.replaceChildren();
  const alerts = Array.isArray(currentData?.recent_alerts) ? [...currentData.recent_alerts].reverse() : [];
  const shown = alerts.filter((item) => {
    const level = String(item.level || "").toLowerCase();
    const high = ["critical", "error", "warning", "warn"].includes(level);
    return currentFilter === "all" || (currentFilter === "high" ? high : !high);
  }).slice(0, 12);
  if (!shown.length) { container.append(node("div", "panel-empty", alerts.length ? "当前筛选下没有告警记录" : "暂无告警记录")); return; }
  shown.forEach((raw) => {
    const alert = object(raw); const level = String(alert.level || "").toLowerCase();
    const high = level === "critical" || level === "error"; const warn = level === "warning" || level === "warn";
    const row = node("div", "alert-row");
    row.append(node("span", `alert-dot ${high ? "high" : warn ? "warning" : ""}`, high ? "!" : warn ? "!" : "i"));
    const copy = node("div"); copy.append(node("div", "alert-title", eventNames[alert.event] || text(alert.event, "未命名事件")), node("div", "alert-context", contextLabel(alert.context))); row.append(copy);
    row.append(node("span", "alert-time", dateLabel(alert.timestamp, true)));
    container.append(row);
  });
}

function changedSection(name, values, callback) {
  const version = JSON.stringify(values);
  if (sectionVersions.get(name) === version) return;
  sectionVersions.set(name, version);
  callback();
}
function render(data) {
  currentData = data;
  receivedAt = performance.now();
  const valid = data.status_valid === true;
  const stale = isStale(data);
  const details = object(data.details);
  renderHeader(data);
  changedSection("metrics", [valid, stale, data.equity, data.cash, data.positions, details.account_entry_gate], () => renderMetrics(data));
  changedSection("chart", [valid, data.history], () => renderChart(data));
  changedSection("allocation", [valid, stale, data.equity, data.cash], () => renderAllocation(data));
  changedSection("positions", [valid, stale, data.positions, details.protective_orders], () => renderPositions(data));
  // Checks include snapshot age, so their labels must follow each observation.
  renderChecks(data);
  changedSection("strategies", [valid, stale, details.strategy_health], () => renderStrategies(data));
  changedSection("reasons", [valid, data.health_reasons], () => renderReasons(data));
  changedSection("alerts", [currentFilter, data.recent_alerts], renderAlerts);
  document.dispatchEvent(new CustomEvent("dashboard:status", { detail: data }));
}
async function refresh() {
  if (fetching || document.hidden) return;
  fetching = true;
  $("refreshButton").disabled = true;
  $("refreshButton").classList.add("loading");
  try {
    render(await requestJSON("/api/status", { key: "dashboard-status", timeout: 10000 }));
  } catch (_) {
    render({ mode: "live", status_valid: false, healthy: false, recent_alerts: [],
      health_reasons: [{ code: "CONNECTION_UNAVAILABLE", subject: "dashboard", message: "监控页面暂时无法读取状态接口" }] });
    set("syncStatus", "连接中断");
  } finally {
    fetching = false;
    $("refreshButton").disabled = false;
    $("refreshButton").classList.remove("loading");
    secondsUntilRefresh = REFRESH_SECONDS;
    set("countdown", document.hidden ? "已暂停" : `${secondsUntilRefresh}s`);
  }
}

const alertFilterButtons = document.querySelectorAll(".alert-filters button");
function syncAlertFilterButtons() {
  alertFilterButtons.forEach((button) => {
    const selected = button.dataset.filter === currentFilter;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}
syncAlertFilterButtons();
alertFilterButtons.forEach((button) => button.addEventListener("click", () => {
  currentFilter = button.dataset.filter;
  syncAlertFilterButtons();
  renderAlerts();
}));
$("refreshButton").addEventListener("click", () => {
  document.dispatchEvent(new CustomEvent("dashboard:refresh"));
  refresh();
});
setInterval(() => {
  if (document.hidden || fetching) return;
  if (currentData && finite(currentData.snapshot_age_seconds)) {
    const observed = { ...currentData, snapshot_age_seconds: currentData.snapshot_age_seconds + Math.floor((performance.now() - receivedAt) / 1000) };
    renderHeader(observed);
    if (isStale(observed) !== isStale(currentData)) render(observed);
  }
  secondsUntilRefresh -= 1;
  if (secondsUntilRefresh <= 0) refresh();
  else set("countdown", `${secondsUntilRefresh}s`);
}, 1000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) set("countdown", "已暂停");
  else refresh();
});
refresh();
