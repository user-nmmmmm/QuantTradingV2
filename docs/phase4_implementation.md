# Phase 4 — 路由、退出与组合分配完成记录

## 结论

Phase 4 的生产路径已从“逐标的即时下单 + Router 状态切换强平”改为“两阶段候选收集 + 组合统一排序分配”。Router 的生产接口 `collect_candidate` 只做状态映射、候选筛选、旧入场意图取消和冷却；持仓退出交还给开仓策略或具名的全局退出控制器。所有新闭合交易可按 `entry strategy × exit controller` 联合归因。

## T-4.1 Router 职责契约

- Router：状态识别结果到候选策略的映射、候选收集、冷却和组合分配入口。
- 开仓策略：自身止损、策略退出及进入不允许状态后的退出。
- MaxHoldingPeriod：超过配置持仓上限后的具名超时退出。
- RiskManager：降仓、阻止开仓、强平和锁定。
- EndOfBacktest：期末清算。

生产路径不再隐式提交 `strategy_id=Router, exit_reason=StateSwitch` 的平仓。状态变化默认动作是 `stop_new_entries`；`reduce` 和 `flatten` 只能由显式风控或运维控制器触发。

## T-4.2 状态稳定期分析

分析源为仓库内记录量最大的路由日志 `reports/20260803_162555_3864d_23Syms_Ret27.2pct/routing_log.csv`，把既有状态序列分别施加稳定期 2、3、5、10 后统计持续时间和切换矩阵：

| 稳定期 | 总切换次数 | 相对 period=2 降幅 | 典型影响 |
|---:|---:|---:|---|
| 2 | 1,921 | — | 最敏感，状态翻转最多 |
| 3 | 1,815 | 5.5% | 降噪有限 |
| 5 | 1,596 | 16.9% | 每增加一个确认 bar 的降噪效率最高 |
| 10 | 1,204 | 37.3% | 延迟过大，TREND_UP 运行段从 23 个压缩到 4 个 |

选择 `stability_period=5`。选择规则是相对 period=2 的“切换减少数 / 额外确认 bar”最大，而不是盲目选择最慢的 period=10。

## T-4.3 状态切换动作矩阵

| 事件 | 停止新开仓 | 降仓 | 立即平仓 | 控制器 |
|---|---|---|---|---|
| 策略映射变化 | 是（含 cooldown） | 否 | 否 | Router |
| 策略自身退出/状态不允许 | 是 | 否 | 是 | 开仓策略 |
| 组合回撤 reduce 门槛 | 是/按风控状态 | 是 | 否 | RiskManager |
| 组合回撤 liquidate/lock | 是 | 否 | 是 | RiskManager |
| 最大持仓期 | 是 | 否 | 是 | MaxHoldingPeriod |
| 回测结束 | 是 | 否 | 是或盯市 | EndOfBacktest |

## T-4.4 TrendBreakout 退出

TrendBreakout 的 Donchian 退出继续使用前一时点通道，且生产路由在持仓期间始终调用该笔 lot 的开仓策略 `process_exit_only`。即使市场状态已切换，也由 TrendBreakout 自己提交“不允许状态”退出，不再由 Router 抢先强平。联合归因因此能显示 TrendBreakout 自身退出，而不是基线中的 0%。逐笔行为由 Phase 4 测试和既有 `tests/test_trend_breakdown.py`/策略退出测试覆盖。

## T-4.5～T-4.7 策略治理

- `TrendBreakdown`：`paused_redesign`。基线 21 笔、PF 0.5524、净 PnL -241.46，修复前路由到 Cash。重构必须补充趋势持续性、量价确认、独立止损/追踪退出和样本外 PF 置信区间。
- `RangeMeanReversion`：`paused_redesign`。基线 78 笔、PF 0.4080、净 PnL -2,421.87，修复前路由到 Cash。重构需把极值入场、波动过滤、止损和回归退出拆成可消融规则。
- `VolatilityReversion`：`isolated_research`。基线 153 笔、已记录真实手续费/滑点后的 PF 0.8592、净 PnL -1,519.77，未达到 PF 1.15 准入门槛，因此路由到 Cash。

## T-4.8 资产归因

基于 Phase 0 的 10 标的基线逐笔重建：

| 资产 | 交易数 | 净 PnL | 结论 |
|---|---:|---:|---|
| BNB-USDT | 55 | -2,067.62 | 停用，等待独立复验 |
| DOGE-USDT | 23 | -131.01 | 停用，等待独立复验 |
| SOL-USDT | 11 | -177.41 | 停用，等待独立复验 |
| AVAX-USDT | 7 | -248.02 | 停用，等待独立复验 |
| ETH-USDT | 40 | +362.09 | 保留在隔离复验池；当前所选基线并非负贡献 |

这里没有把路线图中的“ETH 负贡献”当成预设结论；以实际基线证据为准，ETH 只保留复验，不直接加入生产组合。

## T-4.9～T-4.10 组合级分配与顺序不变性

每个时间戳先收集所有 `EntryCandidate`，再按以下稳定全序排序：

1. `score` 降序；
2. `strategy_name` 升序；
3. `symbol` 升序。

只有排序完成后才逐项调用统一风险定仓；后续候选能看到此前已接受挂单的 pending notional。原序、逆序和随机输入顺序因此得到相同的候选排名与资金分配路径。自动化测试覆盖三种显式顺序，并对排序键做确定性断言。

## T-4.11 持仓长尾

新增 `phase4.max_holding_days=365`。Router 在生产候选路径中读取 lot 账本最早 `entry_time`，超过上限后提交具名 `MaxHoldingPeriod` 退出；报告诊断同时输出 median、p95、max 和 timeout 列表。超时退出不会伪装成策略自身退出。

## T-4.12 联合归因

报告新增 `Diagnostics.joint_entry_exit_attribution`，矩阵单元包含闭合交易数和净 PnL。维度分别来自开仓 lot 的 `strategy` 和闭合 fill 的 `exit_strategy`，可独立识别策略退出、MaxHoldingPeriod、RiskManager、EndOfBacktest 等控制器的贡献。

## Phase 4 门槛

- G10 退出可解释：代码和报告契约已完成；TrendBreakout 不再被 Router 的 StateSwitch 抢先强平。
- G11 顺序不敏感：组合候选排序不依赖输入 symbols 顺序，自动化测试通过。

证据源：`docs/baseline/phase0/archived_reports/20260824_163836_3498d_10Syms_Ret-15.8pct/trades.csv`、对应 `report.txt`，以及上述最大路由日志。分析生成器为 `scripts/run_phase4_analysis.py`。
