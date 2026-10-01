# 架构边界与拆包说明

本页补齐源码中 `A3`、`A4` 注释所引用的架构说明，记录当前代码如何划分职责。它不维护项目阶段、任务状态或实盘准入；这些以[统一 Roadmap](unified_roadmap.md)和[开发计划](development_plan.md)为准。后续结构调整的顺序和验收条件见[工程结构优化路线图](engineering_structure_roadmap.md)。

## A3：交易账户事实与研究投影

交易路径中的持仓、现金与批次事实由 `core/portfolio.py`、`core/lots.py` 及对应状态存储维护。`research/audit/ledger.py` 的 `AuthoritativeLedger` 和 `PortfolioProjection` 是离线事件重放、对账研究的独立投影；其名称并不表示它已经接管实盘或回测下单路径。不能把离线投影的成功重放当成交易所账户已对账或真实资金可运行的证据。具体接口与限制见[研究账本说明](authoritative_ledger.md)。

设计上，`core/` 的通用事件编码不应依赖 `research/` 的具体类型。当前 `core/events/codec.py` 的 `_resolve_type` 仍延迟导入 `research.audit.ledger` 中的 `CashEvent`、`MarkPriceEvent`，这是尚待消除的依赖倒置。迁移时应先固定旧事件的编码、解码及历史名称兼容样本，再将共享事件类型移动到稳定的核心契约位置；该迁移尚未完成。

## A4：按变更原因拆分大模块

原先集中在单个文件的实现已按职责拆入 `core/broker/`、`core/events/`、`core/exchange/`、`core/live_broker/`、`core/metrics/`、`core/risk/`，以及 `live_trading/tick_orchestrator.py`、`recovery.py`、`state_export.py`。各包的 `__init__.py` 保留原有常用导入入口，帮助调用方逐步迁移；拆包本身不代表交易算法、资金语义或验收状态发生变化。

部分拆分通过 mixin 复用同一个实例状态，例如 `core/broker/` 和 `live_trading/engine.py`。未来若改为独立协作对象，需要单独固定订单、成交、恢复与状态写入的行为，再进行有针对性的迁移和回归验证。

若需要追溯更早的架构判断，可读[2026-08 技术复盘归档](archive/2026-08-technical-reviews/README.md)和[历史项目文件分析](archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md)。这些材料保留当时快照，不维护当前完成状态。
