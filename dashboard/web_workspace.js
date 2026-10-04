import { el } from "./api.js";

const views = {
  overview: ["工作台总览", "连接账户事实，掌握组合状态。", "WORKSPACE / OVERVIEW"],
  backtest: ["回测实验室", "从一个假设开始，用历史数据检验策略。", "RESEARCH / BACKTEST LAB"],
  market: ["市场行情", "观察本地历史日线，确认研究的数据边界。", "DATA / MARKET EXPLORER"],
  operations: ["运行监控", "追踪策略健康、控制条件与会话事件。", "OPERATIONS / SYSTEM HEALTH"],
  experiments: ["实验与对比", "积累研究结论，让每一次改动都可以比较。", "RESEARCH / EXPERIMENT LIBRARY"],
  data: ["本地数据管理", "先检查数据质量，再解释策略结果。", "DATA / QUALITY CONTROL"],
  robust: ["稳健性研究台", "在滚动训练、验证与样本外测试中检验稳定性。", "RESEARCH / WALK-FORWARD"],
};
const aliases = { top: "overview", positions: "overview", activity: "operations", alerts: "operations", "experiment-config": "backtest", "trade-diagnostics": "backtest", "execution-details": "backtest" };
let research;
let lab;
let market;
let robust;
let currentView = "overview";
function revealCurrentNav() {
  if (window.innerWidth > 760) return;
  document.querySelectorAll("[data-route]").forEach((link) => {
    if (link.dataset.route === currentView) link.scrollIntoView?.({ block: "nearest", inline: "center", behavior: "instant" });
  });
}

function activate() {
  const hash = location.hash.slice(1);
  const view = views[hash] ? hash : aliases[hash] || currentView;
  const changedView = view !== currentView;
  currentView = view;
  document.querySelectorAll(".workspace-view[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  document.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  el("workspaceTitle").textContent = views[view][0];
  el("workspaceDescription").textContent = views[view][1];
  el("workspaceEyebrow").textContent = views[view][2];
  document.title = `${views[view][0]} · Stillwater Quant`;
  document.documentElement.dataset.view = view;
  if (changedView) {
    window.scrollTo?.({ top: 0, behavior: "instant" });
    el("workspaceTitle").focus?.({ preventScroll: true });
  }
  revealCurrentNav();
  document.dispatchEvent(new CustomEvent("dashboard:view", { detail: { view } }));
  if (view === "backtest") {
    research ||= import("./research.js").then((module) => module.initResearch()).catch(() => {
      research = null;
      el("researchMessage").textContent = "回测工作区加载失败，请刷新页面重试。";
    });
  }
  if (["backtest", "experiments"].includes(view)) {
    lab ||= import("./lab.js").then((module) => module.initLab()).catch((error) => {
      lab = null; el("historyMessage").textContent = `实验模块加载失败：${error.message}`;
    });
  }
  if (["market", "data"].includes(view)) {
    market ||= import("./market.js").then((module) => module.initMarket()).catch((error) => {
      market = null; el("qualityMessage").textContent = `数据模块加载失败：${error.message}`;
    });
  }
  if (view === "robust") {
    robust ||= import("./robust.js").then((module) => module.initRobust()).catch((error) => {
      robust = null; el("robustContent").textContent = `研究模块加载失败：${error.message}`;
    });
  }
}

window.addEventListener("hashchange", activate);
let navigationFrame;
window.addEventListener("resize", () => {
  cancelAnimationFrame(navigationFrame);
  navigationFrame = requestAnimationFrame(revealCurrentNav);
});
document.addEventListener("dashboard:status", ({ detail }) => {
  el("sidebarMode").textContent = detail.mode === "demo" ? "演示快照" : "本地工作区";
  el("workspaceConnection").textContent = detail.status_valid ? "快照已连接" : "无有效账户快照";
});
activate();
