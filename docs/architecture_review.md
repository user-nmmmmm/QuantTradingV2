# 架构边界与拆包说明

本页补齐源码中 `A3`、`A4` 注释所引用的架构说明，记录当前代码如何划分职责。它不维护项目阶段、任务状态或实盘准入；这些以[统一 Roadmap](unified_roadmap.md)和[开发计划](development_plan.md)为准。后续结构调整的顺序和验收条件见[工程结构优化路线图](engineering_structure_roadmap.md)。

## A3：交易账户事实与研究投影

交易路径中的持仓、现金与批次事实由 `core/trading/portfolio.py`、`core/trading/lots.py` 及对应状态存储维护。`research/audit/ledger.py` 的 `AuthoritativeLedger` 和 `PortfolioProjection` 是离线事件重放、对账研究的独立投影；其名称并不表示它已经接管实盘或回测下单路径。不能把离线投影的成功重放当成交易所账户已对账或真实资金可运行的证据。具体接口与限制见[研究账本说明](authoritative_ledger.md)。

`core/` 的通用事件编码不依赖 `research/` 的具体类型（2026-10-11 已完成）：`CashEvent`、`MarkPriceEvent` 及其小型辅助函数移到 `core/events/ledger_events.py`，`research.audit.ledger` 继续导出这些名字。持久化的类型名是线上格式的一部分，所以 `core/events/codec.py` 用显式的 `_PERSISTED_NAMES` 保持 `research.audit.ledger:CashEvent` 等历史名称不变，并继续接受 `core.ledger:*` 旧名称；`tests/test_event_wire_compat.py` 在移动前的代码上先通过，用来固定这一点。

## A4：按变更原因拆分大模块

原先集中在单个文件的实现已按职责拆入 `core/broker/`、`core/events/`、`core/exchange/`、`core/live_broker/`、`core/metrics/`、`core/risk/`，以及 `live_trading/tick_orchestrator.py`、`recovery.py`、`state_export.py`。各包的 `__init__.py` 保留原有常用导入入口，帮助调用方逐步迁移；拆包本身不代表交易算法、资金语义或验收状态发生变化。

`Broker`、`LiveBroker`、`RiskManager`、`LiveTradingEngine` 原先通过 mixin 继承共享同一个实例状态；B4（[ADR 0018](adr/0018-mixins-to-collaborators.md)）已把 13 个 mixin 改为持有 `host` 引用的协作类，宿主以惰性属性持有它们并保留同签名的转发方法。状态仍由宿主持有，迁移到协作类需要单独固定订单、成交、恢复与状态写入的行为，再进行有针对性的迁移和回归验证。

若需要追溯更早的架构判断，可读[2026-08 技术复盘归档](archive/2026-08-technical-reviews/README.md)和[历史项目文件分析](archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md)。这些材料保留当时快照，不维护当前完成状态。

## 导入方向（import-linter）

包之间的依赖方向由 `pyproject.toml` 的 `[tool.importlinter]` 声明并在 CI 里用 `lint-imports` 检查（包括函数内的延迟导入）。分层从高到低：`dashboard` → `research | analysis` → `backtest | live_trading` → `composition` → `router` → `strategies` → `config` → `ops | signal_meta` → `core`；高层可以导入低层，反之不行，同层的 `|` 两侧互相独立。另有一条规则禁止 `core` 依赖任何其他项目包。`scripts/` 与 `tests/` 在规则之外。

分层契约**没有已知例外**（`ignore_imports` 为空，由 `tests/test_architecture_boundaries.py::test_layer_exceptions_do_not_grow` 固定；最后 4 条在 [ADR 0017](adr/0017-clear-layer-exceptions.md) 中清零）。历史上的 4 条例外及其处理：

| 导入 | 原因 | 处理方向 |
|---|---|---|
| `backtest.engine` → `signal_meta.label_account_reconciliation` | 引擎结束时调用标签账户对账 | 已完成：对账模块及其两个依赖搬到 `signal_meta/`（ADR 0017） |
| `backtest.coin_selector` → `research.ml_selection.{dataset,models,selector}` | `--coin-selector on` 加载冻结的 ML 选币器 | 已完成：`create_selector` / `restore_selector` 搬到 `research/ml_selection/serving.py`（ADR 0017）；选币器统一仍属 [V4 设计](v4_architecture_design.md) 第 4 节 |
