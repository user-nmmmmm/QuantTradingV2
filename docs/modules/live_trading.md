# live_trading/ 模块说明

`live_trading/` 负责实盘/沙盒的轮询调度、订单恢复、账户与保护单对账、健康监控、告警和状态导出。状态识别、仓位管理与新开仓候选收集复用 `core/runtime.py` 的 `EventProcessor`；实盘还要在下单前核对独立账户事实与最新风险限制。

## `live_trading/engine.py` — LiveTradingEngine

`LiveTradingEngine` 组装依赖并运行轮询主循环。具体实现分布在 `engine.py` 及三个 mixin：

- `LiveMarketDataAdapter`（行情，来自 `core/market_data.py`）
- `RecordedExecutionAdapter`（执行，包装一个 `LiveBroker`）
- `RiskManager`、路由器、候选分配器及共享的 `EventProcessor`
- `TickOrchestratorMixin`（`tick_orchestrator.py`）、`RecoveryMixin`（`recovery.py`）、`StateExportMixin`（`state_export.py`）

**关键方法**：

- `initialize()`（`engine.py`）：建立状态存储、恢复未完成订单、同步账户并预热历史行情。
- `run()`（`engine.py`）：循环调用 `_tick()` 并按正常间隔或失败退避休眠，捕获 `KeyboardInterrupt` 退出。
- `_tick()` / `_tick_once()`（`tick_orchestrator.py`）：单次轮询。先刷新行情，随后同步账户并执行到期的订单/账户对账；再构建估值快照、检查风控、处理已有仓位与保护单，最后对同一收盘时刻的新开仓候选批量分配。每根已收盘 bar 使用 `StateStore` 认领并在处理后完成或释放。
- `_recover_orders()` / `_run_reconciliation_if_due()`（`recovery.py`）：恢复非终态订单，并按需核对交易所订单与独立账户事实。
- `_maybe_export_state()` / `_export_state()`（`state_export.py`）：按间隔或重要状态变化导出 JSON，并用临时文件、`os.replace` 和 fsync 完成原子写入。

**需要注意的行为**：

- `StateStore` 的 `claim_bar`/`complete_bar`/`release_bar` 租约让崩溃后的 bar 可以重新认领，并配合订单身份与对账减少重复提交风险；租约本身不是交易所侧的“恰好一次”保证。
- 行情刷新失败会使健康检查拒绝新增风险，但 `_tick_once()` 仍尝试同步账户、恢复订单并维护已有仓位的保护措施。账户事实无法确认等更严重的失败可能提前结束本次 tick。
- 未解决的 `UNKNOWN` 订单会阻止**新增风险**，并触发对账；已有仓位的风险动作、退出及保护单对账仍可继续，具体执行取决于当次账户、价格和风控事实是否可用。
- 熔断状态通过事务性存储跨进程重启持久化（刻意不信任人类可读的 JSON 状态文件）。
- `_last_account_sync_at`/`_last_order_sync_at` 会喂给 `DataHealthMonitor`，健康检查不通过可强制进入 `RISK_HALTED`。

## `live_trading/execution_adapter.py` — RecordedExecutionAdapter

实盘模式下 `RuntimeExecutionAdapter` 协议的实现。包装一个 `LiveBroker`（也可以不带 broker，用于离线重放/状态重建），把 `submit_intent`/`submit_order` 转发给 broker，并通过 `TradingEventPipeline` 记录/消费 `EventEnvelope`。`replay(events, apply_fills=False)` 可以重放成交事件而不触碰组合状态，除非显式传 `apply_fills=True`；`_apply_fill` 通过 `_applied_fill_ids` 保证幂等，避免重复计入同一笔交易所事实。`__getattr__` 会把未知属性代理到底层 broker。

## 与其他模块的关系

`live_trading/engine.py` 和 `backtest/engine.py` 共用 `EventProcessor`、路由与策略接口，但各自编排执行顺序和风险动作。实盘引擎还负责轮询、`StateStore` 租约、健康检查、账户与保护单对账、状态导出及订单恢复。实盘使用 `LiveMarketDataAdapter`/`RecordedExecutionAdapter`，回测使用 `HistoricalMarketDataAdapter`/`SimulatedExecutionAdapter`；两种模式均需提供共享决策链路所要求的数据与执行接口。

`run_live.py`（仓库根目录）是驱动这个引擎的 CLI 入口，最终产出的 `reports/live_status.json`/`reports/live_alerts.jsonl` 被 `dashboard/__main__.py` 消费。
