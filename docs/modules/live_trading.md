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
- `_tick()` / `_tick_once()`（`tick_orchestrator.py`）：单次轮询。启用运行控制时先执行到期保护，再刷新行情、同步账户并执行到期的订单/账户对账；随后构建估值快照、检查组合风险、核对保护单与成交后入场风险，最后处理策略持仓管理，并对同一收盘时刻的新开仓候选批量分配。每根进入正常策略处理的已收盘 bar 使用 `StateStore` 认领，并在处理后完成或释放。
- `_recover_orders()` / `_run_reconciliation_if_due()`（`recovery.py`）：恢复非终态订单，并按需核对交易所订单与独立账户事实。
- `_maybe_export_state()` / `_export_state()`（`state_export.py`）：按间隔或重要状态变化导出 JSON，并用临时文件、`os.replace` 和 fsync 完成原子写入。

`_tick()` 返回 `True` 仅表示没有未处理异常逃出本轮，不表示允许下单。行情、账户或风险门控可以在 `_tick_once()` 内处理失败并提前返回；运行状态和入场门控需查看健康评估、`account_entry_gate` 与风险状态。

## 新增风险、已有持仓与失败处理

“禁止新增风险”与“停止全部处理”是不同状态。进入下表路径前仍需考虑账户事实、价格和在途订单是否可用：

| 当次情况 | 当前行为 |
| --- | --- |
| 行情刷新失败 | 拒绝新增风险；继续尝试账户同步、订单恢复及保护维护。失败标的旧缓存不能视为新行情。 |
| 余额事实可用，但独立账户核验未通过或已过期 | 保留基于已知持仓的保护/退出路径，拒绝新增风险；候选分配前再次核验账户证据。 |
| 账户同步异常或余额事实不可用 | 记录健康失败并提前结束本次 tick，不根据不明持仓继续正常策略处理。 |
| 存在未解决的 `UNKNOWN` 订单 | 强制尝试对账并阻止新增风险；风险动作和保护维护仍可进入，但受订单占用和撤单确认约束。 |
| 组合 `BLOCK_NEW` 且未触发日内损失熔断 | 拒绝新开仓，仍允许进入策略持仓管理路径。其他熔断动作或日内损失熔断可能在策略处理前返回。 |
| 组合减仓或回撤预算动作尚未完成 | 优先恢复该风险动作并核对保护；该轮不再分配新增风险。 |

入场门控至少在候选收集前和批量分配前各检查一次，避免耗时的行情读取、持仓管理或账户证据过期使早先的放行结果失效。账户来源和对账操作见 [`account_fact_operations.md`](../account_fact_operations.md)。

其他恢复约定：

- `StateStore` 的 `claim_bar`/`complete_bar`/`release_bar` 租约让崩溃后的 bar 可以重新认领，并配合订单身份与对账减少重复提交风险；租约本身不是交易所侧的“恰好一次”保证。
- 熔断状态通过事务性存储跨进程重启持久化（刻意不信任人类可读的 JSON 状态文件）。
- `_last_account_sync_at`/`_last_order_sync_at` 会喂给 `DataHealthMonitor`，健康检查不通过可强制进入 `RISK_HALTED`。

## 可选运行控制与状态补帧

`runtime_controls.py`、`protection_schedule.py`、`catchup.py` 提供同一引擎线程内的调度控制。`RuntimeSchedulePolicy.enabled` 默认是 `False`；CLI 用 `--runtime-controls` 显式启用。

| 配置 / CLI 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `market_timeout_seconds` / `--market-timeout` | 5 秒 | 本轮行情读取与逐标的计算启动的时间预算。 |
| `protection_interval_seconds` / `--protection-interval` | 5 秒 | 到期保护回调完成后，到下一次到期的间隔。 |
| `catchup_max_bars` / `--catchup-max-bars` | 100 | 每个标的每轮最多恢复的历史 bar 数。 |
| `--market-data-workers` | 4 | 并行公共行情读取数量，CLI 允许 1–8。 |

**行情读取。** 工作线程仅执行行情读取，主线程将成功结果合并进缓存并计算指标。读取等待占总预算的 80%；超时或失败标的保留原缓存并报告失败，迟到结果不能再回写。截止检查发生在每个标的计算开始之前，不能强制中断已经开始的网络请求或同步计算，因此该预算不是整个 tick 的严格耗时上限。

**保护节奏。** 到期保护在 tick 开始时以及主循环较长等待期间执行，依次同步账户、按需恢复订单、处理外部持仓并核对保护单。`ProtectionSchedule` 不创建线程，也不抢占正在执行的任务；其重入标记只用于单线程递归保护。回调结束或抛错后都会推进下一次到期时间，`run_if_due()` 返回 `True` 只表示执行过回调，不能作为保护成功证据。

**状态补帧。** `catchup_cursor:{symbol}:{timeframe}` 保存已处理 bar 的开盘时间。`plan_catchup()` 接收已收盘且索引有序、唯一的历史数据，并按以下规则恢复：

1. 首次启动没有游标时，直接将最新已收盘 bar 交给正常处理，不补发启动前的历史信号。
2. 有游标时，按时间顺序恢复其后的历史 bar；`replay_state_only()` 只推进市场状态、路由和策略冷却，不运行历史下单、撤单、退出或候选分配。
3. 每轮最多恢复 `catchup_max_bars` 根。仍有历史积压或时间缺口时，不放行最新 bar；存在缺口时游标最多推进到缺口前的最后一根连续 bar。
4. 每次历史状态恢复都将游标与运行状态原子写入。最新 bar 正常处理完成时，`complete_bar(..., state_values=...)` 将完成事实、游标与运行状态一并提交。

补帧状态中的 `pending` 统计剩余已观察帧，不估计缺失帧数量；无缺口时不包含预留给正常处理的最新 bar。内置 `DataFetcher` 发现游标早于当前窗口时，每轮可补取一个有界历史窗口；若仍有缺口，继续阻止该标的恢复正常策略处理并发出 `catchup_gap` 告警。

## 保护单与持仓身份

`_reconcile_protective_orders()` 以真实净持仓及其 `position_ids` 为依据：保护数量扣除在途非止损退出单已经占用的数量，包括状态未确定的退出单；保护价沿收紧方向调整。持仓身份用于防止旧仓位的保护单或退出动作误作用于后来重新建立的仓位。

替换保护单前，撤单必须确认进入 `CANCELED`、`FILLED` 或 `EXPIRED` 终态，并重新同步余额。订单记录无法读取、持仓身份不可验证或取消结果不确定时，系统会降级并告警；不能确认保护时可请求退出，但退出仍受账户事实、撤单结果及持仓归属约束。请求退出不代表已经平仓。详细约定见 [`protective_stop_contract.md`](../protective_stop_contract.md) 和 [`g2_order_lifecycle.md`](../live_safety.md)。

## `live_trading/execution_adapter.py` — RecordedExecutionAdapter

实盘模式下 `RuntimeExecutionAdapter` 协议的实现。包装一个 `LiveBroker`（也可以不带 broker，用于离线重放/状态重建），把 `submit_intent`/`submit_order` 转发给 broker，并通过 `TradingEventPipeline` 记录/消费 `EventEnvelope`。`replay(events, apply_fills=False)` 可以重放成交事件而不触碰组合状态，除非显式传 `apply_fills=True`；`_apply_fill` 通过 `_applied_fill_ids` 保证幂等，避免重复计入同一笔交易所事实。`__getattr__` 会把未知属性代理到底层 broker。

## 维护时的核对入口

- [`tests/test_runtime_roadmap_followup.py`](../../tests/test_runtime_roadmap_followup.py)：限时读取、迟到结果隔离、保护重入、状态补帧、原子游标和重启恢复。
- [`tests/test_account_runtime_gate.py`](../../tests/test_account_runtime_gate.py)：独立账户证据与新增风险门控。
- [`tests/test_live_risk_action_lifecycle.py`](../../tests/test_live_risk_action_lifecycle.py)：组合风险动作和恢复生命周期。
- [`tests/test_protective_position_epoch.py`](../../tests/test_protective_position_epoch.py)：保护单与持仓身份隔离。

这些测试用于核对当前行为，不替代生产账户证据或历史验收文档中的环境、日期及范围说明。

## 与其他模块的关系

`live_trading/engine.py` 和 `backtest/engine.py` 共用 `EventProcessor`、路由与策略接口，但各自编排执行顺序和风险动作。实盘引擎还负责轮询、`StateStore` 租约、健康检查、账户与保护单对账、状态导出及订单恢复。实盘使用 `LiveMarketDataAdapter`/`RecordedExecutionAdapter`，回测使用 `HistoricalMarketDataAdapter`/`SimulatedExecutionAdapter`；两种模式均需提供共享决策链路所要求的数据与执行接口。

`run_live.py`（仓库根目录）是驱动这个引擎的 CLI 入口，最终产出的 `reports/live_status.json`/`reports/live_alerts.jsonl` 被 `dashboard/__main__.py` 消费。
