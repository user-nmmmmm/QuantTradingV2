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

**敞口列（BM3）**：每写一行权益曲线，引擎同时用 `_sample_exposure` 采样当时的非空持仓与对应标记价（空仓标的不记，`calculate_exposure` 本来也会跳过），run 结束时用 `core/metrics.py` 的 `calculate_exposure` 算出 `gross_exposure` / `net_exposure` / `priced_symbols` / `gross_exposure_pct_equity` / `net_exposure_pct_equity`，**join 到返回的 `equity_curve` 上**而不是另开一个结果键——这样 `equity.csv`、xlsx 的 Equity 表、`equity.png` 的杠杆曲线都自动拿到，且不可能把某一行的权益配上另一行的持仓。熔断终止后的平坦尾段与 `EndOfBacktest` 合成行也会采样，因此这些列没有空洞（空洞会被读成"空仓"，而那是另一个断言）。

## `backtest/execution_adapter.py` — SimulatedExecutionAdapter

回测版的 `RecordedExecutionAdapter` 对应实现。`on_market_data(event)` 调用 `broker.process_orders(event.bars)`——这正是"下一 bar 执行"模型的落地位置：bar N 提交的挂单会在 bar N+1 的 OHLC 上撮合，`strategies/base.py` 文档字符串反复提到的这个模型就是靠这里实现的。`__getattr__` 代理到 `Broker`（`submit_order`/`cancel_symbol_orders`/`trades` 等），这也是为什么 `Router`/`Strategy` 代码可以把实盘和回测的执行端当作同一种接口对待。

## `backtest/reporting/` — ReportGenerator

报告层分两级：`metrics.py`（指标计算）和 `trades.py`（FIFO 往返交易重建）先算出事实，
`render/` 下的 `text.py`（report.txt）、`charts.py`（PNG）、`workbook.py`（xlsx）、
`pdf.py`（PDF）只负责把同一份事实渲染成不同产物，本身不再计算任何指标。

回测跑完之后的分析产出，不参与交易循环本身。`ReportGenerator(output_dir).generate(trades, equity_curve, metadata, benchmark_curve)` 写出 `equity.csv`、`benchmark.csv`、`trades.csv`、`report.txt`，以及四联图 `equity.png`（权益+基准、回撤、日收益、现金/持仓堆叠面积）。

交易口径分两级，不能混：`_reconstruct_closed_trades` 用**按标的、按方向的 FIFO 栈**（`long_stack`/`short_stack`）把原始成交记录配成**成交腿**（一次开仓 fill 与一次平仓 fill 的匹配）；`_aggregate_round_trips` 再按 `position_id` 把同一次持仓（建仓到清零）的腿折叠成一条**往返交易**。参与率限速会把一个订单拆到多根 bar，因而一次往返可能有多条腿。

`TotalTrades`、`WinRate`、`ProfitFactor`、`Expectancy` 以及 `ExtendedAnalytics` / `Diagnostics` 全部按**往返**统计；腿数另以 `ClosedTradeLegs` 报出，两个口径在 report.txt、xlsx 与 PDF 摘要里都可见。唯一仍按腿统计的是 `lifecycle_coverage`——它的对照量 `Strategy.observed_close_events` 是按 lot close 计数的，所以 `build_diagnostics` 为它单独接收 `closed_legs`。

折叠时有两个反直觉的取舍：`initial_risk` 在 `core/lots.py` 里是**整个 lot** 的值、每次部分平仓都会重复带出，所以按不同 `lot_id` 各取一次求和而不是按腿累加（否则 1R 的交易会被算成 0.33R）；`mae`/`mfe` 是单位价格幅度，取各 lot 的最大值而不是求和。用 `core/metrics.py` 的 `calculate_equity_metrics` 算 CAGR/夏普/最大回撤，年化因子根据权益曲线时间戳的中位数间隔自动推断。

**信号漏斗（BM3）**：`ReportGenerator.generate(..., event_log=...)` 传入引擎返回的 `event_log` 后，`ExtendedAnalytics["signal_funnel"]` 按 `correlation_id` 统计"风控评估 → 风控放行 → 订单生成 → 订单受理 → 成交"每一级的留存数。这是唯一能指出**信号在哪个环节被吞掉**的产物——成交明细和权益曲线都只能看到活下来的那些。注意回测只为**开仓**意图发布 `risk_decision`（`ensure_opening_reservation` 只为 buy/short 预留额度），所以漏斗描述的是入场链路，平仓单从 `order_created` 那一级才出现，`order_created` 大于 `risk_evaluated` 是正常的。

## 与其他模块的关系

参见 [`live_trading.md`](live_trading.md) 中"与其他模块的关系"一节——两个包是对同一套 `EventProcessor` 抽象的两种平行调度实现，`backtest/` 只是同步地循环 `market_data.stream()` 并收集权益曲线，没有实盘引擎那些持久性/健康监控相关的复杂度。
