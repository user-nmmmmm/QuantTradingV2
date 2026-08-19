# 专业词汇表（回测与交易术语）

本文档统一本项目中出现的专业术语的**中文名 / 英文名 / 定义 / 计算口径 / 代码位置**，作为后续选币模块、仓位管理、合约基础设施和策略模型开发时的共同语言基础。

约定：
- "代码位置"给出实现该概念的函数/字段，便于查证口径而非凭记忆解释。
- 涉及“空值语义”的指标遵循统一规则：`0` 表示计算结果确实为零；样本不足记为 `null` + `insufficient`；数学上不可定义（如分母为零）记为 `null` + `undefined`；不输出 `inf`/`-inf`。该规则的权威定义见 [`docs/backtest_assumptions.md`](backtest_assumptions.md) 第 8 节。

---

## 1. 执行与撮合（Execution）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 次K线执行 | Next-Bar Execution | 信号在 bar `t` 收盘后生成，订单在 bar `t+1` 才撮合，防止使用未来数据（前视偏差）。 | `core/broker.py`；口径见 [`backtest_assumptions.md §1`](backtest_assumptions.md) |
| 前视偏差 | Look-Ahead Bias | 回测中错误使用了在该时间点实际不可得的信息（如用收盘后才确定的最高价去做当根K线的决策）。是回测结果失真的最常见原因之一。 | — |
| 市价单 | Market Order | 以下一根K线开盘价成交（叠加滑点），保证成交但不保证价格。 | `core/broker.py` |
| 限价单 | Limit Order | 指定价格挂单；触及即成交，开盘直接穿价按开盘价（Taker），盘中触及按限价成交（Maker）。 | `core/broker.py` |
| 止损单 | Stop Order | 触发价被触及后转为市价单，按更不利的“开盘价/触发价”成交（Taker）。 | `core/broker.py` |
| 挂单方 / Maker | Maker | 提供流动性的一方（限价单盘中被动成交），费率通常更低。默认 0.05%（5 bps）。 | `config/params.yaml: execution.commission_rate_maker` |
| 吃单方 / Taker | Taker | 主动吃掉盘口流动性的一方（市价单及立即成交的限价单），费率通常更高。默认 0.10%（10 bps）。 | `config/params.yaml: execution.commission_rate_taker` |
| 滑点 | Slippage | 实际成交价与预期价之间的偏差。支持固定滑点（按比例平移）和随机滑点（`[0, MaxSlip]` 均匀分布）。 | `core/broker.py`；`--slippage` / `--random_slip` |
| 冲击成本 | Impact Cost | 订单量相对市场成交量过大时产生的额外成本惩罚（简化模拟，非真实订单簿深度模型）。 | `config/params.yaml: execution.use_impact_cost` |
| 多标的时间对齐 | Multi-Symbol Time Alignment | 多标的回测按时间戳交集对齐；若为日线及更慢周期，退化为按日历日期对齐。 | `backtest/engine.py` |

---

## 2. 市场状态与路由（Regime & Routing）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 市场状态机 | Regime Detection | 基于 SMA 结构 + ADX 强度 + ATR% 波动扩张，识别 `TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE` 四种状态。 | `core/state.py` |
| ADX | Average Directional Index | 趋势强度指标，用于过滤弱趋势下的假信号。 | `core/indicators.py` |
| ATR / ATR% | Average True Range | 平均真实波幅；`ATR/close` 超过阈值时状态被覆盖为 `VOLATILE`。 | `core/indicators.py`, `core/state.py` |
| 策略路由 | Router | 按当前 regime 把每根 bar 的交易决策路由给对应策略，并带状态切换互斥与冷却期防止频繁翻转。 | `router/` |
| 冷却期 | Cooldown | regime 切换后强制等待的 bar 数，避免过度交易。 | `config/params.yaml: router.cooldown_bars` |

---

## 3. 组合与风控（Portfolio & Risk）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 权益 | Equity | 账户总价值 = 现金 + 所有持仓的市值。 | `core/portfolio.py`；`equity.csv` |
| 敞口 / 名义敞口 | Exposure / Notional Exposure | 持仓的市值风险暴露。区分总敞口（Gross，各标的绝对值求和）与净敞口（Net，多空相抵后求和）。 | `calculate_exposure`（`core/metrics.py`） |
| 杠杆 | Leverage | 总敞口相对权益的倍数（Gross Exposure / Equity）。 | `config/params.yaml: risk.max_leverage` |
| 集中度限制 | Concentration Limit | 单一标的仓位市值占组合权益的最大比例，默认 `max_pos_size_pct=20%`。 | `core/risk.py` |
| 日内回撤熔断 | Intraday Drawdown Circuit Breaker | 当日回撤触及阈值后禁止新开仓（不强平已有持仓）。 | `config/params.yaml: risk.max_drawdown_limit` |
| 流动性约束 | Liquidity Constraint | 单笔订单量不得超过该 bar 成交量的一定比例，防止不现实的巨额瞬时成交。 | `core/risk.py` |

---

## 4. 核心绩效指标（Performance Metrics）

| 术语 | 英文 | 定义 / 公式 | 代码位置 |
| --- | --- | --- | --- |
| 年化收益率 | CAGR (Compound Annual Growth Rate) | `(End/Start)^(365.25/elapsed_days) - 1`。要求起始权益 `>0`、结束权益 `>=0`、经过天数 `>0`，否则标记 `undefined`。 | `calculate_equity_metrics` |
| 夏普比率 | Sharpe Ratio | `mean(returns) / std(returns) * sqrt(periods_per_year)`，用样本标准差（`ddof=1`）。标准差为 0 或年化因子缺失时为 `undefined`。 | `calculate_sharpe` |
| 年化周期数 | Periods Per Year | 由权益曲线时间索引的**中位正间隔**推断得到，而非硬编码；加密日线用 365.25，4h/1h/15m 分别按每天 6/24/96 个周期折算。 | `infer_periods_per_year` |
| 最大回撤 | Max Drawdown (MDD) | 权益从历史峰值到之后最低点的最大跌幅（百分比 `max_pct` 与金额 `max_amount`）。同时记录峰值/谷底/恢复时间、持续与恢复的周期数和天数、是否尚未恢复（`is_open`）。 | `calculate_drawdown` |
| 回撤事件（枚举） | Drawdown Events | 把整段权益曲线拆分为多段独立的“峰→谷→恢复”回撤事件，而非只报告最差的一次；`min_depth_pct` 可过滤掉过浅的噪音回撤。 | `calculate_drawdown_events` |
| 水下比例 | Underwater Ratio | 权益曲线处于历史峰值以下（即“水下”）的时间占比。 | `calculate_drawdown` |
| 月收益 | Monthly Return | 基于连续月末权益的 `pct_change`；缺少上月基准的首个月不参与平均。 | `monthly_returns` |
| 总收益率 | Total Return | `EndEquity / StartEquity - 1`。 | `calculate_equity_metrics` |

---

## 5. 交易质量与盈亏结构（Trade Quality）

| 术语 | 英文 | 定义 / 公式 | 代码位置 |
| --- | --- | --- | --- |
| 胜率 | Win Rate | 盈利交易数 / 总交易数。 | `calculate_trade_quality` |
| 盈亏比 | Avg Win / Avg Loss | 平均盈利交易金额 与 平均亏损交易金额（后者为负数）。 | `calculate_trade_quality` |
| 期望值 | Expectancy | `胜率 × 平均盈利 + (1-胜率) × 平均亏损`，即单笔交易的期望净盈亏。 | `calculate_trade_quality` |
| 盈利因子 | Profit Factor (PF) | `总盈利金额 / 总亏损金额（绝对值）`。附带 95% Bootstrap 置信区间；闭合交易数 `<30` 标记 `insufficient`；无亏损交易时 `undefined`。 | `calculate_profit_factor` |
| 持仓时长 | Holding Duration | 单笔交易从 `entry_time` 到 `exit_time` 的小时数，报告均值/中位数/最小/最大。 | `_holding_duration_hours` |
| R 值 / R-Multiple | R-Multiple | `net_pnl / initial_risk`，`initial_risk` 是入场时承担的美元风险（如 `qty × |entry_price - stop_price|`）。缺少 `initial_risk` 的交易被排除并单独计数（`excluded_no_initial_risk`）。 | `calculate_r_multiple_stats` |
| SQN | System Quality Number | `sqrt(样本数) × mean(R) / std(R)`，衡量交易系统整体质量（收益/风险的稳定性），标准差为 0 时 `undefined`。 | `calculate_r_multiple_stats` |
| 最大不利/有利变动 | MAE / MFE (Maximum Adverse/Favorable Excursion) | 持仓期间价格相对入场价的最大不利/有利偏离；本项目只汇总调用方提供的逐笔 MAE/MFE 字段，不从价格路径反推。 | `calculate_r_multiple_stats` |

---

## 6. 归因与对比（Attribution & Benchmark）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 归因分析 | Attribution | 按策略 / 标的 / 月份拆分总净盈亏的贡献，缺失分组记为 `"UNKNOWN"` 而非丢弃，保证各拆分之和精确等于总额。 | `calculate_attribution` |
| 基准 | Benchmark | 当前实现为“多标的等权买入并持有”（Equal Weight Buy & Hold），用于衡量策略的超额收益。 | `calculate_benchmark_comparison`；`benchmark.csv` |
| 超额收益 | Excess Return | 策略总收益 − 基准总收益（在两者时间索引的交集上计算）。 | `calculate_benchmark_comparison` |
| 滚动收益 | Rolling Return | 固定窗口的滚动累计收益，只回看不前视（`shift(window)`）。 | `calculate_rolling_returns` |
| 分段收益 | Segment Returns | 把权益曲线按位置（非按日历）切成 N 段等长区间，粗略检验各阶段表现是否一致。 | `calculate_segment_returns` |

---

## 7. 稳健性验证（Robustness / OOS）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 样本外验证 | Out-of-Sample (OOS) | 用训练期之外、未参与调参的数据检验策略表现，防止过拟合。 | `analysis/validation.py` |
| 训练/测试切分 | Train/Test Split | 按时间顺序（而非随机打乱）切分收益序列，避免未来信息泄漏进训练集。 | `train_test_split_returns` |
| 滚动验证 | Walk-Forward Validation | 训练窗口与测试窗口按时间顺序滚动前进，测试窗口紧接训练窗口之后（无重叠、无间隔）。 | `walk_forward_windows` |
| 自助法 / Bootstrap | Bootstrap | 对收益序列做有放回重抽样，估计统计量（均值/夏普）的置信区间；假设独立同分布，未建模自相关，区间是不确定性的下界估计。 | `bootstrap_return_distribution` |
| 蒙特卡洛交易序列重排 | Monte Carlo Trade Sequence | 对已实现的逐笔盈亏做**无放回重排**（排列组合），估计不同交易顺序下的最终盈亏与最大回撤分布，度量“顺序风险”，不产生新的交易结果。 | `monte_carlo_trade_sequence` |
| 多重检验校正 | Multiple Testing Correction (Benjamini-Hochberg) | 对多个假设检验（如尝试了 N 个策略变体）的 p 值做 FDR 校正，防止“矮子里拔将军”式的虚假显著性。 | `benjamini_hochberg` |
| 成本敏感性分析 | Cost Sensitivity Analysis | 在固定成交量价的前提下，用不同的手续费/滑点乘数重新计算净盈亏网格，检验策略对成本假设的敏感程度。 | `calculate_cost_sensitivity` |

---

## 8. 事件与执行链路（Events）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 信号漏斗 | Signal Funnel | 按 `correlation_id` 把同一笔信号在“风控评估 → 风控通过 → 订单创建 → 订单被交易所接受 → 成交”链路中各阶段的转化情况统计出来。 | `calculate_signal_funnel` |
| 关联 ID | Correlation ID | 同一笔信号在整条处理链路（信号→风控→订单→成交）中共享的确定性 ID，用于串联事件。 | `core/events.py` |

---

## 9. 合约/衍生品相关术语（当前为能力预留，尚未打通）

> 这些术语的检测/数据结构已在 `core/exchange_boundary.py` 中定义，但保证金、强平等实际行为尚未在生产链路验证（见 [README「当前能力边界」](../README.md)）。列在此处是为了让后续「仓位管理系统」「合约基础设施」阶段的开发使用统一术语。

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 衍生品市场类型 | Derivative Market Types | `future` / `futures` / `swap`（永续）/ `margin`，区别于 `spot`（现货）。 | `DERIVATIVE_MARKET_TYPES`（`core/exchange_boundary.py`） |
| 只减仓 | Reduce-Only | 订单只能减少现有仓位，不能反向开新仓，常用于止损/止盈以防止意外反手。 | `OrderIntent.reduce_only` |
| 双向持仓模式 | Hedge Mode | 同一标的允许同时持有多头和空头两个独立仓位（对冲模式），区别于单向模式（One-Way）。 | `ExchangeCapabilities.supports_hedge_mode` |
| 合约面值 | Contract Size | 单张合约对应的标的数量，用于把“张数”换算为名义价值。 | `MarketSpecification.contract_size` |
| 线性/反向合约 | Linear / Inverse Contract | 线性合约以计价货币（如 USDT）结算盈亏；反向合约以标的币本身结算盈亏。 | `MarketSpecification.linear/inverse` |
| 资金费率 | Funding Rate | 永续合约用于锚定现货价格的周期性多空资金结算；**当前系统未模拟**，假设现货为 0 或永续多空平衡。 | `backtest_assumptions.md §4` |
| 强平 / 强平引擎 | Liquidation / Liquidation Engine | 保证金不足以维持仓位时被交易所强制平仓；**当前系统尚未实现强平引擎**，假设账户保证金始终充足。 | `backtest_assumptions.md §4` |
| 持仓方向 | Position Side | `long`（多头）/ `short`（空头），双向模式下同一标的可同时存在。 | `CanonicalPosition.position_side` |

---

## 10. 待建立术语（选币模块规划中）

以下术语目前项目中**尚无实现**，是选币模块（Universe Selection）设计阶段需要明确定义、避免歧义的概念，先占位以便后续统一：

- **标的池 / Universe**：某一时间点参与选币评估的候选标的集合。
- **动态标的池 / Dynamic Universe**：标的池随时间变化（新增上市、剔除下架/低流动性标的）。
- **存活偏差 / Survivorship Bias**：只用“最终仍存在”的标的做回测，忽略已下架/退市标的，导致结果虚高。
- **选币前视偏差**：用某个历史时间点实际不可得的数据（如未来才确定的成交量排名）去决定该时间点“是否该被选中”。
- **再平衡周期 / Rebalance Period**：选币结果多久重新评估一次（如每日/每周换仓）。
- **换手率 / Turnover**：相邻两次再平衡之间标的池变动的比例，过高会侵蚀收益（交易成本）。

---

## 参考

- 指标口径的权威说明：[`docs/backtest_assumptions.md §8`](backtest_assumptions.md)
- 指标实现：[`core/metrics.py`](../core/metrics.py)
- 交易所边界与合约能力检测：[`core/exchange_boundary.py`](../core/exchange_boundary.py)
- 能力验收矩阵：[`docs/missing_capabilities_acceptance.md`](missing_capabilities_acceptance.md)
