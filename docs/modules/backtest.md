# backtest/ 模块说明

`backtest/` 负责历史行情调度、模拟撮合、保护止损、适用的资金费用/借贷成本、逐 bar 估值与报告。它与 `live_trading/` 共用 `core/runtime.py` 的 `EventProcessor`、路由和策略决策，但执行顺序和模式特有的风控、持久化流程分别实现。

## `backtest/engine.py` — BacktestEngine

`BacktestEngine.run(data_map, strategies=None, routing_log_path=None)`：

1. 为每次运行构建新的 `Portfolio`、`Broker`（使用 `config.config` 的滑点/手续费/冲击成本等配置）、`RiskManager`、状态机、`Router` 和候选分配器。
2. 包装历史行情适配器 `HistoricalMarketDataAdapter` 和执行适配器 `SimulatedExecutionAdapter`。
3. 构造与实盘共用的 `EventProcessor`，注入 `router` 和 `router.allocator`，另建 `ResidentStopSimulator` 负责已驻场保护止损。
4. 遍历 `market_data.stream()`：启用点时点组合控制器时，先过滤已知退市后不可执行的 bar；撮合前一 bar 的普通挂单并计提适用的资金费用/借贷成本；再处理驻场保护止损和已公告退市的强平；最后调用 `processor.process(event, execute_market_event=False)` 在当前 bar 收盘时运行策略。
5. 按风险决策执行减仓或清算，并逐 bar 检查保证金、成交后的入场风险和账户核算，再把权益与敞口写入同一行。
6. 返回包含 `trades`、`equity_curve`、基准、事件日志、执行与风险审计等字段的结果。基准口径由运行配置选择，结果还区分固定与动态基准。

`warmup_period`（默认 30）传给 `EventProcessor`，限制新开仓在指标预热完成后开始；已有持仓的管理路径仍可运行。

**敞口列（BM3）**：每写一行权益曲线，`BacktestEngine._sample_exposure` 就从当时的非空持仓与标记价直接计算 `gross_exposure`、`net_exposure`、`priced_symbols`、`gross_exposure_pct_equity`、`net_exposure_pct_equity`，并更新该行。最后 `_equity_frame` 只组装这些已配对的行，不会在运行结束后把另一份持仓历史 join 回来。熔断后的尾段与 `EndOfBacktest` 合成行也按各自状态采样。该实现位于 `backtest/engine.py`；`core/metrics/` 另提供报告分析用的 `calculate_exposure`。

## `backtest/execution_adapter.py` — SimulatedExecutionAdapter

它实现共享的执行端接口。`on_market_data(event)` 调用 `broker.process_orders(event.bars, order_filter=...)`，排除驻场保护止损单，再调用 `broker.accrue_carry(event.bars)` 计提相应成本。保护单由 `ResidentStopSimulator` 在普通单撮合之后单独处理，避免新开仓与止损顺序颠倒。bar N 提交的普通订单最早在 bar N+1 撮合。`__getattr__` 将其余执行能力代理给 `Broker`。

## `backtest/reporting/` — ReportGenerator

报告层分两级：`metrics.py`（指标计算）和 `trades.py`（FIFO 往返交易重建）先算出事实，
`render/` 下的 `text.py`（report.txt）、`charts.py`（PNG）、`workbook.py`（xlsx）、
`pdf.py`（PDF）只负责把同一份事实渲染成不同产物，本身不再计算任何指标。

回测跑完之后的分析产出，不参与交易循环本身。`ReportGenerator(output_dir).generate(...)` 按 `report_profile` 输出不同文件：`full` 包含详细 CSV、文本、图表等；`compact` 输出精简版本；`workbook` 以工作簿为主。不可假定每种 profile 都会生成 `equity.csv`、`trades.csv`、`report.txt` 与 PNG。

交易口径分两级，不能混：`_reconstruct_closed_trades` 用**按标的、按方向的 FIFO 栈**（`long_stack`/`short_stack`）把原始成交记录配成**成交腿**（一次开仓 fill 与一次平仓 fill 的匹配）；`_aggregate_round_trips` 再按 `position_id` 把同一次持仓（建仓到清零）的腿折叠成一条**往返交易**。参与率限速会把一个订单拆到多根 bar，因而一次往返可能有多条腿。

`TotalTrades`、`WinRate`、`ProfitFactor`、`Expectancy` 以及 `ExtendedAnalytics` / `Diagnostics` 全部按**往返**统计；腿数另以 `ClosedTradeLegs` 报出，两个口径在 report.txt、xlsx 与 PDF 摘要里都可见。唯一仍按腿统计的是 `lifecycle_coverage`——它的对照量 `Strategy.observed_close_events` 是按 lot close 计数的，所以 `build_diagnostics` 为它单独接收 `closed_legs`。

折叠时有两个反直觉的取舍：`initial_risk` 在 `core/lots.py` 里是**整个 lot** 的值、每次部分平仓都会重复带出，所以按不同 `lot_id` 各取一次求和而不是按腿累加（否则 1R 的交易会被算成 0.33R）；`mae`/`mfe` 是单位价格幅度，取各 lot 的最大值而不是求和。用 `core.metrics.calculate_equity_metrics` 算 CAGR/夏普/最大回撤；仅在权益时间轴严格等间隔时自动推断年化因子，缺 bar 或混频时需显式提供尺度，否则有关指标会报告不可计算。

**信号漏斗（BM3）**：`ReportGenerator.generate(..., event_log=...)` 传入引擎返回的 `event_log` 后，`ExtendedAnalytics["signal_funnel"]` 按 `correlation_id` 统计"风控评估 → 风控放行 → 订单生成 → 订单受理 → 成交"每一级的留存数。这是唯一能指出**信号在哪个环节被吞掉**的产物——成交明细和权益曲线都只能看到活下来的那些。注意回测只为**开仓**意图发布 `risk_decision`（`ensure_opening_reservation` 只为 buy/short 预留额度），所以漏斗描述的是入场链路，平仓单从 `order_created` 那一级才出现，`order_created` 大于 `risk_evaluated` 是正常的。

## 与其他模块的关系

参见 [`live_trading.md`](live_trading.md) 中"与其他模块的关系"一节——两个包是对同一套 `EventProcessor` 抽象的两种平行调度实现，`backtest/` 只是同步地循环 `market_data.stream()` 并收集权益曲线，没有实盘引擎那些持久性/健康监控相关的复杂度。
