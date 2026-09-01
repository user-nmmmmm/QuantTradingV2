# backtest/ 模块说明

`backtest/` 是回测模式的调度外壳，与 `live_trading/` 共用 `core/runtime.py` 的 `EventProcessor` 决策核心，区别只在于行情来源和执行落地方式。

## `backtest/engine.py` — BacktestEngine

`BacktestEngine.run(data_map, strategies=None, routing_log_path=None)`：
1. 构建全新的 `Portfolio`、`Broker`（从 `config.config` 读取滑点/手续费/冲击成本配置）、`RiskManager`、状态机、`Router`。
2. 包装历史行情适配器 `HistoricalMarketDataAdapter` 和执行适配器 `SimulatedExecutionAdapter`。
3. 构造 `EventProcessor`——**和实盘用的是完全同一个类**。
4. 遍历 `market_data.stream()`，逐 bar 调用 `processor.process(event)`。
5. 若运行中途触发熔断，通过 `execution.submit_order` 直接强制平掉所有持仓。
6. 返回 `{trades, equity_curve, benchmark}`；`_benchmark` 计算等权买入并持有基准，在 `warmup_period` 处折算回 `initial_capital`。

`warmup_period`（默认 30）直接传给 `EventProcessor`，用于推迟路由直到指标数据填充完毕。

## `backtest/execution_adapter.py` — SimulatedExecutionAdapter

回测版的 `RecordedExecutionAdapter` 对应实现。`on_market_data(event)` 调用 `broker.process_orders(event.bars)`——这正是"下一 bar 执行"模型的落地位置：bar N 提交的挂单会在 bar N+1 的 OHLC 上撮合，`strategies/base.py` 文档字符串反复提到的这个模型就是靠这里实现的。`__getattr__` 代理到 `Broker`（`submit_order`/`cancel_symbol_orders`/`trades` 等），这也是为什么 `Router`/`Strategy` 代码可以把实盘和回测的执行端当作同一种接口对待。

## `backtest/reporting/` — ReportGenerator

报告层分两级：`metrics.py`（指标计算）和 `trades.py`（FIFO 往返交易重建）先算出事实，
`render/` 下的 `text.py`（report.txt）、`charts.py`（PNG）、`workbook.py`（xlsx）、
`pdf.py`（PDF）只负责把同一份事实渲染成不同产物，本身不再计算任何指标。

回测跑完之后的分析产出，不参与交易循环本身。`ReportGenerator(output_dir).generate(trades, equity_curve, metadata, benchmark_curve)` 写出 `equity.csv`、`benchmark.csv`、`trades.csv`、`report.txt`，以及四联图 `equity.png`（权益+基准、回撤、日收益、现金/持仓堆叠面积）。

`_analyze_trades` 用**按标的、按方向的 FIFO 栈**（`long_stack`/`short_stack`）从原始成交记录重建完整的往返交易，计算胜率、盈亏比（经 `core/metrics.py` 的 `calculate_profit_factor`，含置信区间）、期望值，以及按策略拆分的指标（`Strat_{策略名}_*`）。用 `core/metrics.py` 的 `calculate_equity_metrics` 算 CAGR/夏普/最大回撤，年化因子根据权益曲线时间戳的中位数间隔自动推断。

## 与其他模块的关系

参见 [`live_trading.md`](live_trading.md) 中"与其他模块的关系"一节——两个包是对同一套 `EventProcessor` 抽象的两种平行调度实现，`backtest/` 只是同步地循环 `market_data.stream()` 并收集权益曲线，没有实盘引擎那些持久性/健康监控相关的复杂度。
