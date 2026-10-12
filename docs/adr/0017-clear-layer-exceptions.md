# 0017 清零分层规则的 4 条已知例外（B3）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B3，[ADR 0004](0004-layered-import-rules.md)，[ADR 0011](0011-signal-meta-package.md)

## 背景
ADR 0004 建立分层规则时，有 4 条"向上"的函数内延迟导入登记为已知例外（`ignore_imports`），只许减少：
- `backtest.engine → analysis.label_account_reconciliation`（引擎结束时对账观察标签与实际账户）；
- `backtest.coin_selector → research.ml_selection.{dataset,models,selector}`（`--coin-selector on` 时构建冻结的 ML 选币器）。

依赖链显示：对账模块依赖 `analysis.paper_label_execution` → `analysis.paper_labels` → 只依赖 `core`，三者自成一体；选币器里只有 `create_selector` / `restore_selector` 两个函数需要 `research`，包校验、账户兼容性检查、快照、报告都不需要。

## 决定
- 把 `paper_labels`、`paper_label_execution`、`label_account_reconciliation` 三个模块（约 720 行，只依赖 `core`）从 `analysis/` 搬到 `signal_meta/`（它位于 `backtest` 之下）。`analysis` 与 `scripts` 里原有的使用方改为从 `signal_meta` 导入（高层导入低层，合规）。模块名不变，无垫片。
- 把 `create_selector`、`restore_selector` **原样**搬到 `research/ml_selection/serving.py`；`backtest/coin_selector.py` 保留不依赖 `research` 的部分（`read_bundle`、`_account_compatibility`、`snapshot_bundle`、`write_selector_report`、`disabled_identity`）。`main.py`、`scripts/run_selector_backtest.py` 和测试改为从新位置导入（它们位于所有分层之上，或属于 `research` 一侧）。没有引入全局注册表或隐式注入，函数体未改动。
- 删除 `pyproject.toml` 里的 `ignore_imports`，`tests/test_architecture_boundaries.py` 收紧为"不得存在 `ignore_imports`"，并新增测试：`import backtest.coin_selector` 不会加载 `research` 或 `analysis`。以后再加例外需要明确的设计决定。
- 冻结的历史证据、历史 ADR（含 ADR 0004 的原文）不改写。

## 备选方案
- 依赖注入（把加载函数从 `main.py` 传入 `backtest`）：需要改动 `BacktestEngine` 与 CLI 的签名，并在所有调用路径传递，行为风险更大；搬迁后不需要。
- 为选币器定义端口并在 `backtest` 内保留适配：属于 V4 选币器统一的范围（V4 设计第 4 节），留给功能阶段。

## 后果
- 分层契约不再有任何例外；`lint-imports` 的 "N ignored imports" 为 0。
- 行为身份报告显示为"移动 3 个文件、若干文件仅导入变化"，行为变化只有 `create_selector` / `restore_selector` 所在文件的变化（函数体不变）。
- 构建选币器的入口路径变为 `research.ml_selection.serving`；运行手册里的 `backtest.coin_selector.create_selector` 引用已更新。
