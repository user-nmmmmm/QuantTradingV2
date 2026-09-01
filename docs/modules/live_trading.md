# live_trading/ 模块说明

`live_trading/` 是实盘/沙盒轮询交易引擎的"外壳"。它不包含交易决策逻辑本身（决策逻辑在 `core/runtime.py` 的 `EventProcessor` 里，回测和实盘共用），而是负责实盘特有的**持续性**关切：轮询调度、崩溃恢复、健康监控、告警、状态导出。

## `live_trading/engine.py` — LiveTradingEngine

生产环境的轮询主循环。构造时会组装：
- `LiveMarketDataAdapter`（行情，来自 `core/market_data.py`）
- `RecordedExecutionAdapter`（执行，包装一个 `LiveBroker`）
- `RiskManager`
- 通过 `composition.factory.build_router` 构建的 `EventProcessor`

**关键方法**：
- `initialize()`：预热 OHLCV 历史数据，恢复/对齐状态。
- `run()`：无限循环 `_tick()` + `sleep(interval)`，捕获 `KeyboardInterrupt` 优雅退出。
- `_tick()`：单次轮询的核心顺序——恢复未完成订单 → 检查是否存在未解决的"未知订单状态" → 刷新行情 → `broker.sync()` 同步账户 → 计算已收盘 bar 的价格/估值快照 → 检查日内熔断 → 对每个标的执行"认领 bar → 处理 → 完成"（经 `StateStore`）→ 导出状态。
- `_export_state()`：原子化写入 JSON（临时文件 + `os.replace` + fsync）。

**需要注意的行为**：
- 使用 `StateStore` 的 `claim_bar`/`complete_bar`/`release_bar` 租约机制，保证处理过程中崩溃不会导致同一根 bar 被重复处理或丢失。
- 出现"未解决的未知订单状态"（交易所事实不确定）会**暂停全部交易**，直到问题解决。
- 熔断状态通过事务性存储跨进程重启持久化（刻意不信任人类可读的 JSON 状态文件）。
- `_last_account_sync_at`/`_last_order_sync_at` 会喂给 `DataHealthMonitor`，健康检查不通过可强制进入 `RISK_HALTED`。

## `live_trading/execution_adapter.py` — RecordedExecutionAdapter

实盘模式下 `RuntimeExecutionAdapter` 协议的实现。包装一个 `LiveBroker`（也可以不带 broker，用于离线重放/状态重建），把 `submit_intent`/`submit_order` 转发给 broker，并通过 `TradingEventPipeline` 记录/消费 `EventEnvelope`。`replay(events, apply_fills=False)` 可以重放成交事件而不触碰组合状态，除非显式传 `apply_fills=True`；`_apply_fill` 通过 `_applied_fill_ids` 保证幂等，避免重复计入同一笔交易所事实。`__getattr__` 会把未知属性代理到底层 broker。

## 与其他模块的关系

`live_trading/engine.py` 和 `backtest/engine.py` 是**同一套抽象上的两个平行调度器**，而不是各自独立的实现：两者都把全部交易决策逻辑委托给 `core.runtime.EventProcessor`，用同一套协作者（`Portfolio`、`RuntimeExecutionAdapter`、`RiskManager`、状态机、`Router`，经 `composition.factory` 构建）。实盘引擎额外承担了回测引擎完全没有的持久性关切：轮询/休眠循环、`StateStore` 租约、健康监控、告警、原子状态导出、订单恢复/对账。两种模式各自提供一对满足相同协议的适配器：`LiveMarketDataAdapter`/`RecordedExecutionAdapter`（实盘）对应 `HistoricalMarketDataAdapter`/`SimulatedExecutionAdapter`（回测）——正是这种对称性让 `Router` 和 `Strategy.on_bar` 可以做到与模式无关。

`run_live.py`（仓库根目录）是驱动这个引擎的 CLI 入口，最终产出的 `reports/live_status.json`/`reports/live_alerts.jsonl` 被 `dashboard/__main__.py` 消费。
