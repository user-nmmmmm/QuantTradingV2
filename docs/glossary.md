# 专业词汇表（回测与交易术语）

本文档统一本项目中出现的专业术语的**中文名 / 英文名 / 定义 / 计算口径 / 代码位置**，作为后续选币模块、仓位管理、合约基础设施和策略模型开发时的共同语言基础。

约定：
- "代码位置"给出实现该概念的函数/字段/配置键，便于查证口径而非凭记忆解释；标注"（未实现）"的条目是已识别的能力缺口，列入词汇表仅为统一语言。
- 涉及“空值语义”的指标遵循统一规则：`0` 表示计算结果确实为零；样本不足记为 `null` + `insufficient`；数学上不可定义（如分母为零）记为 `null` + `undefined`；不输出 `inf`/`-inf`。该规则的权威定义见 [`docs/backtest_assumptions.md`](backtest_assumptions.md) 第 8 节。
- 指标结果对象遵循 `{value, status, reason, unit, sample_size, periods_per_year, parameters}` 契约，见 [`docs/archive/2026-10-doc-consolidation/backtest_metrics_detailed_development_plan.md`](archive/2026-10-doc-consolidation/backtest_metrics_detailed_development_plan.md)。

---

## 1. 执行与撮合（Execution）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 次K线执行 | Next-Bar Execution | 信号在 bar `t` 收盘后生成，订单在 bar `t+1` 才撮合，防止使用未来数据（前视偏差）。市价单按 `t+1` 开盘价成交，限价单开盘穿价按开盘价（Taker）、盘中触及按限价（Maker），止损触发按 `max/min(open, stop)` 的较不利价成交。 | `core/broker/matching.py`；口径见 [`backtest_assumptions.md §1-3`](backtest_assumptions.md) |
| 前视偏差 | Look-Ahead Bias | 回测中错误使用了在该时间点实际不可得的信息（如用收盘后才确定的最高价去做当根K线的决策）。是回测结果失真的最常见原因之一。 | — |
| 市价单 | Market Order | 以下一根K线开盘价成交（叠加滑点），保证成交但不保证价格。 | `core/broker/` |
| 限价单 | Limit Order | 指定价格挂单；触及即成交，开盘直接穿价按开盘价（Taker），盘中触及按限价成交（Maker）。 | `core/broker/` |
| 止损单 | Stop Order | 触发价被触及后转为市价单，按更不利的“开盘价/触发价”成交（Taker）。 | `core/broker/` |
| 常驻止损（回测模拟） | Resident Protective Stop | 每个净持仓配一张只减仓止损单，在 bar **内部**按预注册的保守路径（open → 不利极值 → 有利极值 → close）撮合，而不是等收盘发现破位后次日开盘才离场。多头止损对 low 测试，跳空按 `min(open, stop)` 成交。契约见 [`docs/protective_stop_contract.md`](protective_stop_contract.md)。 | `backtest/protective_stops.py`；`config/params.yaml: protective_orders.backtest_resident` |
| 挂单方 / Maker | Maker | 提供流动性的一方（限价单盘中被动成交）。费率表必须标注场所与市场类型，并与账户模式一致（账户成本契约强制）。当前默认 0.10%（10 bps）。 | `config/params.yaml: execution.commission_rate_maker`、`execution.fee_schedule`；`core/account_cost_contract.py` |
| 吃单方 / Taker | Taker | 主动吃掉盘口流动性的一方（市价单及立即成交的限价单）。当前默认 0.10%（10 bps），与 Maker 相同（Binance 现货/杠杆标准档）。 | `config/params.yaml: execution.commission_rate_taker` |
| 滑点 | Slippage | 实际成交价与预期价之间的偏差。组成：固定滑点（默认 5 bps）+ 半个买卖价差（`spread_bps` 默认 2）+ 波动滑点（bar 内振幅 × `volatility_slippage_factor` 默认 0.02）；另支持 `[0, MaxSlip]` 均匀分布的随机滑点。 | `core/broker/`；`config/params.yaml: execution.slippage_bps / spread_bps / volatility_slippage_factor`；`--slippage` / `--random_slip` |
| 冲击成本 | Impact Cost | 订单量相对市场成交量过大时产生的额外成本，按非线性参与率模型 `impact_coefficient × participation^impact_exponent` 计算（默认 `0.10 × participation^1.5`）。是先验设定的近似，**不是**真实订单簿深度模型，参数尚未用成交数据校准。 | `config/params.yaml: execution.use_impact_cost / impact_coefficient / impact_exponent` |
| 参与率上限 | Max Participation Rate | 单笔订单每根 bar 最多成交该 bar 成交量的固定比例（默认 5%），超出部分拆到后续 bar 继续撮合。 | `config/params.yaml: execution.max_participation_rate` |
| 开仓单存活期 | Opening Order TTL | 开仓挂单最多可撮合的 bar 数（默认 10），超时撤单；部分成交不刷新 TTL，防止微量成交让挂单与标的状态锁永久续期。平仓单与常驻止损天然豁免。 | `config/params.yaml: execution.opening_order_ttl_bars` |
| 入场风险回填 | Entry-Risk Recheck (GapRiskResize) | 风险预占按信号收盘价计算，成交可能跳空；成交后实际风险超过预算 + 容差（默认 10%）时触发命名的 `GapRiskResize` 缩减仓位，而不是默默承担超配风险。 | `core/entry_risk.py`；`config/params.yaml: entry_risk` |
| 期末平仓模式 | End-of-Backtest Mode | 回测结束时仍有持仓的处理方式：`mark_to_market`（默认，按最后价格零成本平仓，与权益曲线的盯市口径一致）或 `forced_liquidation`（按真实平仓计手续费/滑点）。 | `config/params.yaml: backtest.end_of_backtest_mode` |
| 多标的时间对齐 | Multi-Symbol Time Alignment | 多标的回测的 bar 对齐模式：`union`（默认，只把当根有真实 bar 的标的送进该时间戳）或 `intersection`（要求全部标的同刻都有 bar）。 | `config/params.yaml: data.alignment_mode`；`backtest/engine.py` |
| 会计恒等式校验 | Accounting Identity Check | 每根 bar 落账后校验"现金 + 持仓市值 − 借负债 = 权益"等恒等式，任何漂移直接报错，杜绝账本静默腐烂。 | `core/accounting_check.py`（引擎内 `accounting.check_bar`） |
| 账户成本契约 | Account-Cost Contract | 账户模式（spot / spot_margin / perpetual）与成本模型（费率表、借利率、资金费）必须互相一致，回测与实盘入口都先校验，矛盾即拒绝启动。 | `core/account_cost_contract.py` |

---

## 2. 市场状态与路由（Regime & Routing）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 市场状态机 | Regime Detection | 基于 SMA 结构 + ADX 强度 + ATR% 波动扩张，识别 `TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE` 四种互斥状态。`VOLATILE` 只认领"动得很凶但均线不成方向"的 bar，避免吞掉全部趋势 bar。输出是确定性标签（0/1），**不含概率/置信度**。 | `core/state.py`；`config/params.yaml: state.*` |
| 稳定性滤波 | Stability Filter | 候选状态需连续出现 `stability_period`（当前 5）次才确认切换，压制 regime 边界抖动；候选集合 {2,3,5,10} 已登记为研究候选。 | `core/state.py: _apply_stability_filter`；`config/params.yaml: state.stability_period` |
| ADX | Average Directional Index | 趋势强度指标（周期 14，阈值 25），用于过滤弱趋势下的假信号。 | `core/indicators.py` |
| ATR / ATR% | Average True Range | 平均真实波幅（周期 14）；`ATR/close > atr_pct_threshold`（当前 2.5%）是高波动判据之一。 | `core/indicators.py`, `core/state.py` |
| 策略路由 | Router | 按当前 regime 把空仓标的的入场候选交给映射策略；持仓仍由开仓策略自己按 lot 归因管理。regime 切换只**撤销挂单 + 进入冷却**（`transition_action: stop_new_entries`），不强平持仓。 | `router/router.py: collect_candidate`；`config/params.yaml: routing / router` |
| 冷却期 | Cooldown | regime 切换后强制等待的 bar 数（当前 2），避免过度交易。 | `config/params.yaml: router.cooldown_bars` |
| 时间退出 | Max Holding Period Exit | 持仓超过 `max_holding_days`（默认 365 天）强制退出，防止资金被多年锁死。 | `config/params.yaml: router.max_holding_days` |
| 候选评分 | Candidate Score | 同一根 bar 多个入场候选的排序分，由无量纲分量加权和构成：突破幅度（ATR 单位，权重 1.0）、趋势强度（ADX 超出量，0.5）、量能确认（OBV，0.3）、流动性（对数成交额，0.2）；各分量截断到 ±5 防止单项极端值主导。只决定**排序**，不决定仓位大小。 | `core/candidate_scoring.py`；`config/params.yaml: candidate_scoring` |
| 组合信号分配器 | Portfolio Signal Allocator | 同时间戳候选批次按评分排序（`allocation.order: score_strategy_symbol`），在资本/风险预算内依次分配，取代早期按字母序的隐性平局规则。 | `core/allocation.py` |
| 点位宇宙 | Point-in-Time (PIT) Universe | 按标的真实上市/退市时间裁剪历史数据的宇宙表，消除"用未来才存在的币回测过去"的存活偏差；退市标的在最后可交易 bar 打 `scheduled_exit` 标记触发强制平仓。 | `core/universe.py`；`config/universe_binance_spot_1d.csv`；`main.py --universe-file` |
| 存活偏差 | Survivorship Bias | 只用"最终仍存在"的标的做回测，忽略已下架/退市标的，导致结果虚高。本项目用 PIT 宇宙 + `scheduled_exit` 退市强平路径对冲该偏差。 | `core/universe.py`；`config/universe60_lifecycle.json` |

---

## 3. 组合与风控（Portfolio & Risk）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 权益 | Equity | 账户总价值 = 现金 + 所有持仓的市值（现货口径；保证金账户另计借负债）。 | `core/portfolio.py`；`equity.csv` |
| 敞口 / 名义敞口 | Exposure / Notional Exposure | 持仓的市值风险暴露。区分总敞口（Gross，各标的绝对值求和）与净敞口（Net，多空相抵后求和）。 | `core/metrics/performance.py`；`equity.csv` 敞口列 |
| 杠杆 | Leverage | 总敞口相对权益的倍数（Gross Exposure / Equity），上限默认 3.0。 | `config/params.yaml: risk.max_leverage` |
| 集中度限制 | Concentration Limit | 单一标的仓位市值占组合权益的最大比例，当前 `max_pos_size_pct=30%`。 | `core/risk/position_sizing.py: _entry_notional_caps` |
| 风险定仓 | Risk-Based Sizing | `qty = 权益 × risk_per_trade ÷ |入场价 − 止损价|`（当前 `risk_per_trade=2%`），止损越紧名义仓位越大；是固定分数风险模型，**不随信号质量/胜率调整**（无 Kelly、无波动目标，见 §11）。 | `core/risk/position_sizing.py: calculate_position_size` |
| 削减（而非拒单） | Clamp (vs Reject) | 算出的仓位超过风控上限时削减到上限内成交而非整单作废——否则止损最紧（风险最小）的信号反而永远无法成交；低于最小开仓名义（`minimum_entry`）的尘埃仓位仍返回 0。 | `core/risk/position_sizing.py: clamp_entry_qty`；口径见 [`backtest_assumptions.md §4`](backtest_assumptions.md) |
| 流动性约束 | Liquidity Constraint | 单笔订单量不得超过该 bar 成交量的 1%（`liquidity_limit_pct`），防止不现实的巨额瞬时成交。 | `core/risk/`；`config/params.yaml: risk.liquidity_limit_pct` |
| 回撤阶梯 | Drawdown Ladder | 组合回撤的分级动作：日亏 5%（UTC 日界重置）→ 回撤 10% 减半风险（REDUCE）→ 15% 禁止新开仓（BLOCK_NEW，可经 30 天冷卻 + 0.25 试错期恢复）→ 20% 强平（LIQUIDATE，终止运行）→ 25% 锁定（LOCKED，终止且需 `manual_resume` 人工恢复）。峰值水位永不重置。 | `core/risk/`（CircuitBreakerMixin）；`config/params.yaml: drawdown` |
| 回撤预算 | Drawdown Budget | 回撤逼近阈值时对开仓单的预算钳制：超支时撤销开仓并按比例强制减仓（`force_liquidate(reason='DrawdownBudgetReduce')`），`headroom_fraction` 控制缓冲。 | `core/risk/drawdown_budget.py`、`backtest/drawdown_budget.py`；`config/params.yaml: drawdown_budget` |
| 相关性簇预算 | Correlation Cluster Budget | 把同涨同跌的标的编入簇（静态映射，未映射的一律归入 `crypto_beta`），对簇总敞口（≤1.5×权益）、全加密 beta 敞口（≤2.0）、单批次新开风险（≤6%）、簇内在险止损风险（≤10%）设预算；核心动机：15 个加密主流币在系统性回撤里其实是"一个仓位"。簇划分是**人工静态表**，非估计的协方差结构（见 §11）。 | `core/risk/portfolio_governor.py`；`config/params.yaml: portfolio_risk` |
| 在途风险预占 | Risk Reservation | 信号已发出但订单未成交期间，对其名义/现金占用做投影记账，使并发信号共享同一份预算，防止"每单都合规、合起来超限"。 | `core/risk/reservation.py` |
| 策略健康生命周期 | Strategy Health Lifecycle | 策略按队列（cohort）盈亏在 `ACTIVE → COOLDOWN → PROBATION → MANUAL_LOCK` 间迁移：连续 3 个负队列进入冷却，试错期以缩减风险（0.25）运行并需满足队列数/标的数/总 R 门槛，恢复按 [0.10, 0.25, 0.50, 1.00] 阶梯放量。账户级风控退出不计入健康证据。 | `core/strategy_health.py`；`config/params.yaml: strategy_health`；契约见 [`docs/strategy_health_contract.md`](strategy_health_contract.md) |
| 策略治理状态 | Strategy Governance Status | 策略的准入身份（如 `paused_revalidation / paused_redesign / isolated_research`）：无独立 holdout 证据的策略不得视为生产可用。当前仅 TrendBreakout 交易且处于复验暂停态。 | `core/strategy_governance.py`；`config/params.yaml: strategy_governance` |
| 容量曲线 | Capacity Curve | 同一输入按递增资金档（1万/10万/100万/1000万）重跑，用交易路径签名、拒单/部分成交/冲击成本解释策略容量随资金的衰减。（部分实现：只测参与率冲击，非微观结构估计。） | `backtest/capacity.py`；`config/params.yaml: capacity.capital_levels` |

---

## 4. 核心绩效指标（Performance Metrics）

| 术语 | 英文 | 定义 / 公式 | 代码位置 |
| --- | --- | --- | --- |
| 年化收益率 | CAGR (Compound Annual Growth Rate) | `(End/Start)^(365.25/elapsed_days) - 1`。要求起始权益 `>0`、结束权益 `>=0`、经过天数 `>0`，否则标记 `undefined`。 | `calculate_equity_metrics`（`core/metrics/performance.py`） |
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
| 胜率 | Win Rate | 盈利交易数 / 总交易数。 | `calculate_trade_quality`（`core/metrics/trade_quality.py`） |
| 盈亏比 | Avg Win / Avg Loss | 平均盈利交易金额 与 平均亏损交易金额（后者为负数）。 | `calculate_trade_quality` |
| 期望值 | Expectancy | `胜率 × 平均盈利 + (1-胜率) × 平均亏损`，即单笔交易的期望净盈亏。 | `calculate_trade_quality` |
| 盈利因子 | Profit Factor (PF) | `总盈利金额 / 总亏损金额（绝对值）`。附带 95% Bootstrap 置信区间；闭合交易数 `<30` 标记 `insufficient`；无亏损交易时 `undefined`。 | `calculate_profit_factor` |
| 持仓时长 | Holding Duration | 单笔交易从 `entry_time` 到 `exit_time` 的小时数，报告均值/中位数/最小/最大。 | `_holding_duration_hours` |
| R 值 / R-Multiple | R-Multiple | `net_pnl / initial_risk`，`initial_risk` 是入场时承担的美元风险（如 `qty × |entry_price - stop_price|`）。缺少 `initial_risk` 的交易被排除并单独计数（`excluded_no_initial_risk`）。 | `calculate_r_multiple_stats` |
| SQN | System Quality Number | `sqrt(样本数) × mean(R) / std(R)`，衡量交易系统整体质量（收益/风险的稳定性），标准差为 0 时 `undefined`。 | `calculate_r_multiple_stats` |
| 最大不利/有利变动 | MAE / MFE (Maximum Adverse/Favorable Excursion) | 持仓期间价格相对入场价的最大不利/有利偏离；本项目只汇总调用方提供的逐笔 MAE/MFE 字段，不从价格路径反推。 | `calculate_r_multiple_stats` |
| 队列证据 | Cohort Evidence | 按 日期 × 策略 × 退出控制者 聚合平仓盈亏的评审证据单元，附 bootstrap 置信区间与剔除 top-5/10 盈利的集中度敏感性；账户级风控退出单列、不充当 alpha 失效证据。 | `analysis/strategy_review.py: cohort_evidence` |

---

## 6. 归因与对比（Attribution & Benchmark）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 归因分析 | Attribution | 按策略 / 标的 / 月份拆分总净盈亏的贡献，缺失分组记为 `"UNKNOWN"` 而非丢弃，保证各拆分之和精确等于总额。 | `calculate_attribution`（`core/metrics/attribution.py`） |
| 基准 | Benchmark | 多标的等权基准，两种模式：`fixed`（买入并持有，默认）与 `dynamic`（等权再平衡，再平衡成本 `dynamic_rebalance_cost_bps` 默认 5 bps 入账），用于衡量策略的超额收益。 | `core/benchmarks.py`；`config/params.yaml: benchmark.mode`；`benchmark.csv` |
| 超额收益 | Excess Return | 策略总收益 − 基准总收益（在两者时间索引的交集上计算）。 | `calculate_benchmark_comparison` |
| 滚动收益 | Rolling Return | 固定窗口的滚动累计收益，只回看不前视（`shift(window)`）。 | `calculate_rolling_returns` |
| 分段收益 | Segment Returns | 把权益曲线按位置（非按日历）切成 N 段等长区间，粗略检验各阶段表现是否一致。 | `calculate_segment_returns` |

---

## 7. 稳健性验证（Robustness / OOS）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 样本外验证 | Out-of-Sample (OOS) | 用训练期之外、未参与调参的数据检验策略表现，防止过拟合。 | `analysis/validation.py` |
| 训练/测试切分 | Train/Test Split | 按时间顺序（而非随机打乱）切分收益序列，避免未来信息泄漏进训练集。 | `train_test_split_returns` |
| 滚动验证 | Walk-Forward Validation | 训练/验证/测试窗口按时间滚动前进，强制 `step ≥ test_size`（测试段不重叠），并支持 purge（间隔带）与 embargo（禁带）隔离相邻段；每个 split 只用验证段选参，测试段保持未触碰。 | `analysis/walk_forward.py`；`walk_forward_windows`（`core/metrics/validation.py`） |
| 最终样本（单次裁决） | Holdout / HoldoutVault | 数据三段划分（60/20/20）中最后一段，由 `HoldoutVault` 冻结且只允许 `open_final` 开启一次做准入裁决；协议边界带 SHA-256 指纹防篡改。 | `analysis/research_validation.py: HoldoutProtocol / HoldoutVault`；`config/params.yaml: phase5.partition` |
| 自助法 / Bootstrap | Bootstrap | 对收益序列做有放回重抽样，估计统计量（均值/夏普）的置信区间；假设独立同分布，未建模自相关，区间是不确定性的下界估计。 | `bootstrap_return_distribution` |
| 蒙特卡洛交易序列重排 | Monte Carlo Trade Sequence | 对已实现的逐笔盈亏做**无放回重排**（排列组合），估计不同交易顺序下的最终盈亏与最大回撤分布，度量“顺序风险”，不产生新的交易结果。 | `monte_carlo_trade_sequence` |
| 多重检验校正 | Multiple Testing Correction (Benjamini-Hochberg) | 对多个假设检验（如尝试了 N 个策略变体）的 p 值做 FDR 校正，防止“矮子里拔将军”式的虚假显著性。 | `benjamini_hochberg` |
| 缩减夏普比率 | Deflated Sharpe Ratio (DSR) | 在多重试验与收益非正态（偏度/峰度）下，检验观测夏普是否显著异于"运气基线"的修正指标；试验次数越多，同一夏普的可信度越低。 | `analysis/research_validation.py: deflated_sharpe_ratio` |
| 参数平台 | Parameter Plateau | 要求入选参数位于"邻域参数同样盈利"的平坦区而非孤立尖峰——尖峰多为噪声拟合，平台才可能是真实效应。 | `analysis/research_validation.py: parameter_plateau` |
| 因子消融 | Factor Ablation | 逐一移除信号的组成要素（如 OBV 确认、regime 限制、健康门），量化各要素对收益的边际贡献，识别"装饰性"组件。 | `analysis/research_validation.py: factor_ablation`；`composition/factory.py: research.strategy_ablation` |
| 跨市场验证 | Cross-Market Validation | 用参数所在市场之外的数据复核同一协议，检验结论是否依赖单一市场/周期。 | `analysis/research_validation.py: cross_market_validation` |
| 准入门槛 | Admission Gates | 策略从研究走向生产必须经过的量化裁决：phase5 要求 holdout 上 `PF ≥ 1.15` 且 PF 置信区间下界 `≥ 1.0`、`MDD ≤ 20%`、剔除 top-5/10 后仍成立、成本乘数 {1, 1.5, 2, 3} 下稳健；失败需要新假设/新协议而非继续调参。 | `core/admission_gates.py`；`config/params.yaml: phase5.admission` |
| 幽灵回放 | Ghost Replay | P0 反事实测量：对未成交的信号克隆独立账户，固定持有期、按下一根真实开盘价双边成交，回答"被风控/路由挡下的信号如果放行会怎样"。只观测，绝不影响真实订单。 | `backtest/signal_ghost.py`；`config/params.yaml: signal_observation` |
| 成本敏感性分析 | Cost Sensitivity Analysis | 在固定成交量价的前提下，用不同的手续费/滑点乘数重新计算净盈亏网格，检验策略对成本假设的敏感程度。 | `calculate_cost_sensitivity` |

---

## 8. 事件与执行链路（Events & Reproducibility）

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 信号漏斗 | Signal Funnel | 按 `correlation_id` 把同一笔信号在“风控评估 → 风控通过 → 订单创建 → 订单被交易所接受 → 成交”链路中各阶段的转化情况统计出来。 | `calculate_signal_funnel` |
| 关联 ID | Correlation ID | 同一笔信号在整条处理链路（信号→风控→订单→成交）中共享的确定性 ID，用于串联事件。 | `core/events/` |
| 事件溯源 | Event Sourcing | 交易事实（信号/订单/成交/平仓）以只追加事件流为权威记录，状态由事件重放推导；`SQLiteEventStore` 用触发器强制 INSERT-only。 | `core/events/`；契约见 [`docs/authoritative_ledger.md`](authoritative_ledger.md) |
| 幂等键 | Idempotency Key | 事件去重键（确定性 UUID5）：同键重复投递被忽略，同键不同内容直接报错，保证重放/恢复不会产生重复副作用。 | `core/events/`（`TradingEventPipeline`） |
| 运行清单回放 | Run Manifest Replay | full profile 回测落盘 `run_manifest.json`（配置/数据哈希/种子），可用 `--replay-manifest` 重跑并逐字节比对 digest；退出码 7=拒绝、8=不一致。 | `main.py: replay_manifest` |
| 引擎等价基线 | Engine Equivalence Baseline | 固定种子合成数据跑完整生产链路录制的快照基线，重跑比对浮点容差 `1e-9/1e-12`、其余字段严格相等；行为变更只新增基线版本，**永不覆盖旧版本**。 | `tests/fixtures/backtest/engine/`；`tests/engine_baseline_harness.py` |
| 固定基线（手写） | Fixed Fixture Baseline | 手工编写、可手算复核的冻结输入输出 bundle（orders/fills/closed_trades/equity/metrics），做内部一致性往返校验，不跑引擎。 | `tests/fixtures/backtest/`；`tests/baseline_harness.py` |

---

## 9. 合约/衍生品相关术语（当前为能力预留，尚未打通）

> 这些术语的检测/数据结构已在 `core/exchange/__init__.py` 中定义，但保证金、强平等实际行为尚未在生产链路验证（见 [README「当前能力边界」](../README.md)）。列在此处是为了让后续「仓位管理系统」「合约基础设施」阶段的开发使用统一术语。

| 术语 | 英文 | 定义 | 代码位置 |
| --- | --- | --- | --- |
| 衍生品市场类型 | Derivative Market Types | `future` / `futures` / `swap`（永续）/ `margin`，区别于 `spot`（现货）。 | `DERIVATIVE_MARKET_TYPES`（`core/exchange/__init__.py`） |
| 只减仓 | Reduce-Only | 订单只能减少现有仓位，不能反向开新仓，常用于止损/止盈以防止意外反手。 | `OrderIntent.reduce_only` |
| 双向持仓模式 | Hedge Mode | 同一标的允许同时持有多头和空头两个独立仓位（对冲模式），区别于单向模式（One-Way）。 | `ExchangeCapabilities.supports_hedge_mode` |
| 合约面值 | Contract Size | 单张合约对应的标的数量，用于把“张数”换算为名义价值。 | `MarketSpecification.contract_size` |
| 线性/反向合约 | Linear / Inverse Contract | 线性合约以计价货币（如 USDT）结算盈亏；反向合约以标的币本身结算盈亏。 | `MarketSpecification.linear/inverse` |
| 资金费率 | Funding Rate | 永续合约用于锚定现货价格的周期性多空资金结算。数据端已可抓取（`fetch_binance_data.py --with-funding`），账户配置要求 perpetual 模式必须声明 funding；回测撮合中的资金费建模仍按 `not_modeled` 对待，见 [`backtest_assumptions.md §4`](backtest_assumptions.md)。 | `config/params.yaml: account.funding_rate_required`；`data/binance/funding/` |
| 强平 / 强平引擎 | Liquidation / Liquidation Engine | 保证金不足以维持仓位时被强制平仓；回测侧有保证金快照与强平/熔断动作路径（含 25 bps 强平惩罚），实盘侧仍在能力预留阶段。 | `core/broker/liquidation.py`；`config/params.yaml: account.liquidation_penalty_bps` |
| 持仓方向 | Position Side | `long`（多头）/ `short`（空头），双向模式下同一标的可同时存在。 | `CanonicalPosition.position_side` |

---

## 10. 选币与宇宙术语（Universe Selection）

> PIT 宇宙与存活偏差防治已落地（见 §2）；以下术语对应**规划中的主动选币模块**，项目中尚无实现，先占位以便后续统一：

- **标的池 / Universe**：某一时间点参与选币评估的候选标的集合（当前实现为静态 PIT 表 `config/universe_binance_spot_1d.csv`，非主动评分筛选）。
- **动态标的池 / Dynamic Universe**：标的池随时间变化（新增上市、剔除下架/低流动性标的）。
- **选币前视偏差**：用某个历史时间点实际不可得的数据（如未来才确定的成交量排名）去决定该时间点“是否该被选中”。
- **再平衡周期 / Rebalance Period**：选币结果多久重新评估一次（如每日/每周换仓）。
- **换手率 / Turnover**：相邻两次再平衡之间标的池变动的比例，过高会侵蚀收益（交易成本）。

---

## 11. 数学与统计基础（研究缺口，未实现）

> 本节概念在策略内核中**尚缺**（验证侧的 DSR/BH-FDR 等已有，见 §7），是当前策略"只有规则、没有生成模型"的具体所指。列入词汇表以便研究文档统一引用；各条的"缺口位置"指当前代码中对应的简化实现。

| 术语 | 英文 | 定义 | 缺口位置 / 状态 |
| --- | --- | --- | --- |
| 凯利准则 | Kelly Criterion | 最大化长期对数增长率的下注比例：`f* = p − q/b`（p 胜率、q=1−p、b 盈亏比）。实务用分数 Kelly（如 ½f*）抑制估计误差带来的破产风险。现状：固定 2% 风险定仓，不随胜率/赔率调整。 | `core/risk/position_sizing.py`（未实现） |
| 波动目标 | Volatility Targeting | 以预期波动为分母缩放仓位（`qty ∝ 目标波动 ÷ 实现波动`），使每笔/每日风险贡献恒定。现状：仓位只经止损距离隐含波动，无显式波动目标层。 | 未实现 |
| 风险平价 | Risk Parity / ERC | 使各持仓对组合总风险的边际贡献相等（等风险贡献），而非等金额或等分数。现状：组合层为排序 + 静态上限钳制。 | `core/allocation.py`（未实现） |
| 隐马尔可夫 / 马尔可夫切换 | HMM / Markov-Switching | 把市场 regime 建模为带转移概率矩阵的隐状态，输出各状态后验概率与期望持续期，可量化"当前处于趋势态的把握"。现状：确定性规则 + 计数滤波，无概率语义。 | `core/state.py`（未实现） |
| 波动聚集模型 | GARCH 族 | 描述波动率的自相关（大波动后跟大波动），给出条件波动预测，用于自适应阈值与风险缩放。现状：固定阈值（ADX 25、z ±2.0、布林固定倍数）。 | 未实现 |
| 肥尾与期望短缺 | Heavy Tails / ES (CVaR) | 加密收益尾部远厚于正态；ES（在险价值 VaR 之外的平均损失）是比"最大回撤单点"更稳健的尾部预算口径。现状：回撤阶梯为经验阈值，无 ES/CVaR。 | `config/params.yaml: drawdown`（未实现） |
| 协整 | Cointegration | 两个各自非平稳的序列存在平稳线性组合（Engle-Granger / Johansen 检验）；配对交易的合法前提是协整而**不是**相关。现状：`PairsTradingModel` 用相关系数筛选 + 滚动 OLS 对冲比，无协整检验。 | `strategies/statistical_arbitrage.py`（未注册进 factory） |
| OU 半衰期 | Ornstein-Uhlenbeck Half-Life | 均值回归速度的估计：把价差拟合 OU 过程，`half_life = ln2 / θ`，用于决定持仓期与是否值得交易。 | 未实现 |
| 平稳性检验 | Stationarity Test (ADF/KPSS) | 检验序列（或价差）是否均值回复的统计前提；未通过检验的"均值回归"信号没有统计基础。 | 未实现 |
| 协方差收缩 | Covariance Shrinkage (Ledoit-Wolf) | 样本协方差在资产数接近样本数时病态，向结构化目标收缩得到可逆、低噪的相关结构估计。现状：相关性簇为人工静态表。 | `core/risk/portfolio_governor.py`（未实现） |
| 因子模型 | Factor Model / PCA | 把多标的收益分解为少数公共因子（如 crypto beta）+ 特质收益，使相关性预算按因子暴露而非名义簇记账。 | 未实现 |
| 有效样本量 | Effective Sample Size | 自相关序列的信息量小于样本数；信号评估与置信区间应按有效样本量折算，否则高估显著性。 | 部分实现（`signal_meta_layer` 的 `min_effective_samples/blocks`） |

---

## 12. 经济学与 Alpha 架构（研究缺口，未实现）

> 本节回答"策略凭什么赚钱"。当前代码库没有任何一处陈述 alpha 的经济学命题及其可证伪推论——这是比任何单一数学工具都更底层的缺口。列入以便研究/评审文档统一语言。

| 术语 | 英文 | 定义 | 现状 |
| --- | --- | --- | --- |
| 预期收益模型 | Expected Return Model (Alpha Model) | 信号 → 条件预期收益分布（大小 × 置信度）的显式映射，是仓位与组合分配的输入。现状：信号是布尔事件，评分为手工权重排序分，仓位与信号质量脱钩。 | 未实现（评分见 `core/candidate_scoring.py`） |
| 风险溢价 | Risk Premium | 承担某种系统性风险（如时序动量、波动率、流动性）而获得的长期补偿；一个策略应说明自己收割的是哪种溢价。 | 未陈述 |
| 行为偏差 | Behavioral Bias | 对手盘系统性错误（追涨杀跌、处置效应、锚定）构成的超额收益来源；趋势策略的经典解释之一。 | 未陈述 |
| 拥挤度 | Crowding | 同一公开信号被越多资金跟踪，其预期收益越被提前透支。Donchian 通道类信号是极度拥挤的公开信号，需要拥挤度/衰减建模。 | 未实现 |
| Alpha 衰减 | Alpha Decay | 信号预期收益随时间/拥挤度上升而衰减的过程；要求定期重估信号的条件收益（P1 元层的半衰期加权是雏形）。 | 雏形（`core/signal_meta_layer.py` 研究专用，不碰真实订单） |
| 时序动量 / 横截面动量 | Time-Series vs Cross-Sectional Momentum | 前者对单标的自身历史做趋势跟随（当前 TrendBreakout 属此类）；后者在多标的间做相对强弱排序配置，两者经济学来源与容量不同。 | 仅有时序动量 |
| Carry / 资金费套利 | Carry | 以持有成本/收益差（资金费率、借贷利差、期限结构）为收益来源的策略族；当前资金费在撮合端 `not_modeled`，更未作为信号。 | 未实现 |
| 机会成本 | Opportunity Cost | Cash 状态的资金收益假设（零收益 vs 无风险利率/货币基金/资金费），影响"何时不交易"的决策基准。 | 未建模 |
| 市场微观结构 | Market Microstructure | 订单簿深度、买卖价差、做市库存等决定"信号能否以何代价成交"的机制层；当前用参与率冲击近似，参数未经校准。 | 近似（`backtest/capacity.py` + impact 模型） |
| 零和与手续费转移 | Zero-Sum & Fee Drag | 剔除成本后，投机交易总体为零和/负和；策略预期收益必须显著覆盖双边费率与滑点才有经济学意义。准入门槛的成本乘数检验（×1.5/×2/×3）是该思想的工程化。 | 部分实现（`phase5.admission.cost_multipliers`） |

---

## 参考

- 指标口径的权威说明：[`docs/backtest_assumptions.md §8`](backtest_assumptions.md)
- 指标实现：[`core/metrics/`](../core/metrics/__init__.py)（`performance.py` / `trade_quality.py` / `attribution.py` / `validation.py`）
- 交易所边界与合约能力检测：[`core/exchange/`](../core/exchange/__init__.py)
- 能力验收矩阵：[`docs/roadmap_acceptance_log.md`](roadmap_acceptance_log.md)
- 止损/健康/账本契约：[`docs/protective_stop_contract.md`](protective_stop_contract.md)、[`docs/strategy_health_contract.md`](strategy_health_contract.md)、[`docs/authoritative_ledger.md`](authoritative_ledger.md)
- 自动化回测总体方案：[`docs/archive/2026-10-doc-consolidation/automated_backtest_plan.md`](archive/2026-10-doc-consolidation/automated_backtest_plan.md)
