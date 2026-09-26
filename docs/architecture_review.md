# 架构边界与拆包说明

本页补齐源码中 `A3`、`A4` 注释所引用的架构说明，记录公开代码当前如何划分职责。它不维护项目阶段、任务状态或实盘准入；这些以[统一 Roadmap](unified_roadmap.md)和[开发计划](development_plan.md)为准。后续结构调整的顺序与验收条件见[工程结构优化路线图](engineering_structure_roadmap.md)。

## A3：交易账户事实与研究投影

交易路径中的持仓、现金与批次事实由 `core/portfolio.py`、`core/lots.py` 及对应状态存储维护。`research/audit/ledger.py` 的 `AuthoritativeLedger` 和 `PortfolioProjection` 是离线事件重放、对账研究的独立投影；其名称不表示它已经接管实盘或回测下单路径。离线投影的成功重放不能代替交易所账户对账或真实资金准入。具体接口见[离线研究账本](authoritative_ledger.md)。

设计上，`core/` 的通用事件编码不应依赖 `research/` 的具体类型。当前 `core/events/codec.py` 的 `_resolve_type` 仍延迟导入 `research.audit.ledger` 中的 `CashEvent`、`MarkPriceEvent`，形成待处理的依赖倒置。迁移前需固定旧事件的编码、解码与 `core.ledger:*` 历史名称兼容样本，再把共享事件类型放到稳定的核心契约位置。该迁移尚未实施。

## A4：按变更原因拆分大模块

已按职责拆出的模块包括 `core/broker/`、`core/events/`、`core/exchange/`、`core/live_broker/`、`core/metrics/`、`core/risk/`，以及 `live_trading/tick_orchestrator.py`、`recovery.py`、`state_export.py`。各包的 `__init__.py` 保留常用旧导入入口，便于调用方逐步迁移；拆包本身不代表交易算法、资金语义或验收状态发生变化。

部分拆分使用 mixin 复用同一个实例状态，例如 `core/broker/` 与 `live_trading/engine.py`。将来若改为独立协作对象，应先固定订单、成交、恢复与状态写入行为，再分别迁移和验证。早期架构审查资料属于历史快照，不用于维护当前完成状态。
