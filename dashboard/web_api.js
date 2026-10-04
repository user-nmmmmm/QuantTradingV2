/** Same-origin JSON client. Requests are bounded and superseded reads are aborted. */
const pending = new Map();

export async function requestJSON(path, { key, timeout = 20000, ...options } = {}) {
  if (key) pending.get(key)?.abort();
  const controller = new AbortController();
  let timedOut = false;
  if (key) pending.set(key, controller);
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  try {
    const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new Error(payload?.error || payload?.message || `请求失败（HTTP ${response.status}）`);
    if (!payload) throw new Error("服务返回了无效的数据，请刷新页面后重试。");
    return payload;
  } catch (error) {
    if (timedOut) throw new Error("请求超时，请检查本地服务后重试");
    throw error;
  } finally {
    clearTimeout(timer);
    if (key && pending.get(key) === controller) pending.delete(key);
  }
}

export const el = (id) => document.getElementById(id);
export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
export const formatNumber = (value) => Number.isFinite(value) ? number.format(value) : "—";
export const formatPercent = (value) => Number.isFinite(value) ? `${value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%` : "—";
