import { requestJSON, el, element } from "./api.js";

let catalog;
let presets = [];
let initialized;
let previewSequence = 0;
export async function mutate(path, payload) {
  const options = await requestJSON("/api/backtest-options");
  return requestJSON(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": options.csrf_token }, body: JSON.stringify(payload) });
}
function field(label, input) { const item = element("label", "field", label); item.append(input); return item; }
function select(id, values) {
  const input = element("select"); input.id = id;
  values.forEach(([value, label]) => { const option = element("option", "", label); option.value = value; input.append(option); });
  return input;
}
export function getStrategy() {
  const family = el("strategyFamily")?.value || "configured";
  const parameters = {};
  for (const param of catalog?.families.find((item) => item.id === family)?.parameters || []) {
    const input = el(`strategy-${param.key}`);
    parameters[param.key] = param.type === "boolean" ? input.checked : Number(input.value);
  }
  return { family, parameters };
}
async function preview() {
  const sequence = ++previewSequence;
  try {
    const result = await mutate("/api/strategy-preview", { strategy: getStrategy() });
    if (sequence !== previewSequence) return;
    el("strategyDiff").textContent = result.config_diff?.length ? result.config_diff.map((row) => `${row.path}：${JSON.stringify(row.before)} → ${JSON.stringify(row.after)}`).join("\n") : "使用当前项目配置，无参数覆盖。";
  } catch (error) { if (sequence === previewSequence) el("strategyDiff").textContent = `参数检查：${error.message}`; }
}
function renderParameters(values = {}) {
  const family = catalog.families.find((item) => item.id === el("strategyFamily").value);
  el("strategyDescription").textContent = family?.description || "";
  el("strategyFields").replaceChildren(...(family?.parameters || []).map((param) => {
    const input = element("input"); input.id = `strategy-${param.key}`;
    input.type = param.type === "boolean" ? "checkbox" : "number";
    if (param.type === "boolean") input.checked = values[param.key] ?? param.default;
    else { input.value = values[param.key] ?? param.default; input.min = param.min; input.max = param.max; input.step = param.step ?? (param.type === "integer" ? 1 : "any"); input.required = true; }
    input.addEventListener("change", preview);
    const item = field(param.label, input);
    if (param.type === "boolean") item.classList.add("field-toggle");
    item.append(element("small", "", param.description)); return item;
  }));
  preview();
}
export async function applyStrategy(strategy) {
  await initStrategy();
  const accepted = catalog.families.some((item) => item.id === strategy?.family);
  el("strategyFamily").value = accepted ? strategy.family : "configured";
  renderParameters(accepted ? strategy.parameters || {} : {});
}
async function reloadPresets() {
  const data = await requestJSON("/api/strategy-presets", { key: "strategy-presets" });
  presets = data.presets || [];
  const input = el("strategyPreset");
  input.replaceChildren(...[["", "选择已保存的预设"], ...presets.map((item) => [item.id, item.name])].map(([value, name]) => { const opt = element("option", "", name); opt.value = value; return opt; }));
}
export function initStrategy() {
  initialized ||= (async () => {
    catalog = await requestJSON("/api/strategy-catalog", { key: "strategy-catalog" });
    const root = el("strategyEditor");
    const family = select("strategyFamily", catalog.families.map((item) => [item.id, item.name]));
    family.addEventListener("change", () => renderParameters());
    const description = element("p", "data-note"); description.id = "strategyDescription";
    const fields = element("div", "field-pair"); fields.id = "strategyFields";
    const preset = select("strategyPreset", [["", "选择已保存的预设"]]);
    const presetName = element("input"); presetName.id = "strategyPresetName"; presetName.maxLength = 80; presetName.placeholder = "例如：趋势窗口 30 / 10";
    preset.addEventListener("change", () => { const item = presets.find((p) => p.id === preset.value); if (item) { presetName.value = item.name; applyStrategy(item.strategy); } });
    const save = element("button", "secondary-button", "保存为新预设"); save.type = "button";
    const remove = element("button", "text-button", "删除所选预设"); remove.type = "button";
    const status = element("p", "form-message"); status.id = "strategyStatus"; status.setAttribute("role", "status");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try { await mutate("/api/strategy-presets", { name: presetName.value.trim(), strategy: getStrategy() }); await reloadPresets(); status.textContent = "预设已保存，可在后续实验中重复使用。"; }
      catch (error) { status.textContent = error.message; }
      finally { save.disabled = false; }
    });
    remove.addEventListener("click", async () => {
      if (!preset.value) { status.textContent = "请先选择一个预设。"; return; }
      try { await mutate("/api/strategy-presets/delete", { id: preset.value }); await reloadPresets(); status.textContent = "预设已删除，已完成实验的配置仍保留。"; }
      catch (error) { status.textContent = error.message; }
    });
    const details = element("details", "methodology"), diff = element("pre", "strategy-diff"); diff.id = "strategyDiff";
    details.append(element("summary", "", "本次配置差异"), diff);
    const presetsPanel = element("details", "preset-manager");
    const actions = element("div", "history-actions"); actions.append(save, remove);
    presetsPanel.append(element("summary", "", "保存与管理个人预设"), field("新预设名称", presetName), actions, status);
    root.replaceChildren(field("策略配置", family), description, fields, field("个人预设", preset), presetsPanel, details);
    renderParameters(); await reloadPresets();
  })().catch((error) => { initialized = null; if (el("strategyEditor")) el("strategyEditor").textContent = `策略配置加载失败：${error.message}`; throw error; });
  return initialized;
}
