# 回测研究总纲（2026-11-16）

> 状态：**草案，未冻结，没有任何结果**。本文与 R1–R6 六份协议只是研究设计；其中的阈值都是草案阈值，
> 所有者在冻结前确认或修改，冻结之后不得因为看到结果而改动。
> 本批只交付协议文档，不含脚本、不改配置、不读 `final20`。

## 1. 目的与边界

回答一条链上的六个问题：策略有没有胜过"无技能"基准（R1）；选参流程在样本外是否站得住（R2）；
结论是尖峰还是高原（R3）；在更差的成交假设下是否还在（R4）；收益集中在哪些行情、年份和标的（R5）；
风控预算能否约束回撤与尾部（R6）。

- 这些研究产生的是**研究证据**。"通过"不等于准入或实盘放行；`formal_admission=false` 保持不变，
  准入仍由 [`analysis/research_validation.py`](../../analysis/research_validation.py) 的 holdout 流程与统一 Roadmap 决定。
- 与已有研究的关系：[`current_strategy_protocol.json`](current_strategy_protocol.json) 的鲁棒性矩阵
  （成本倍数 1.0/1.5/2.0/3.0、四类行情、去除最大盈利笔 5/10、参数邻域）在本批里被展开成可执行的协议；
  [入场延迟研究](entry_delay_study_20261110.md) 等 SR 系列研究的判定格式（预注册、逐段双通过）被沿用。

## 2. 共同约束

| 项 | 规定 |
| --- | --- |
| 数据 | `data/binance/1d` 已跟踪的冻结 CSV，经标准本地研究入口读取，遵守 [ADR 0041](../adr/0041-approved-data-adoption-and-research-window.md)：统一截止 2026-08-26，MATIC 截止 2024-09-09，不拼接 POL。清单见 [ADR 0020](../adr/0020-frozen-research-inputs.md)。**不得与 `reports/strategy_review_20260919/frozen_inputs` 的 60 币快照混用**；若要复用 SR 系列的数字，必须单独声明口径不同。 |
| 研究起点 | **2020-01-01**（所有者 2026-11-21 决定，[ADR 0044](../adr/0044-thirty-symbol-research-window-start.md)）。BTC、ETH 的已采用数据从这一天开始，更早的区间里只有当时已有数据的山寨币。train60 / validation20 / final20 的边界在冻结时按 2020-01-01 至各标的研究截止日计算，与 60 币口径的 SR 系列边界不同。加载器不拒绝更早的起点，研究脚本须把起点写死并在登记里记录。 |
| 来源状态 | 其中 28 个文件只固定了字节，来源未核验，点时间可得性为 `unknown`（ADR 0020）。所有结论带这一条限制。 |
| 分段 | 时间顺序 train60 / validation20 / final20，purge 30 根、embargo 30 根，与 `current_strategy_protocol.json` 一致。**R1–R6 一律禁止读取 final20**；任何 final20 的使用是之后单独的所有者决定，且只经 [`analysis/controlled_holdout.py`](../../analysis/controlled_holdout.py) 的原子保险库，对事先冻结的候选打开一次。 |
| final20 已被消耗 | SR5-SUCC-001 已在 final20 上评价过一次 C1 后继候选（60 币口径，见 CHANGELOG）。本批的 30 币数据与它时间重叠，所以 final20 的日期范围已**不是干净的样本**；今后若要一个干净的样本外，只能是观察窗 [2026-11-19, 2027-05-18) 之后产生的前瞻数据。R1–R6 因此不依赖 final20。 |
| 污染声明 | validation20 的日期范围已被 SR 系列研究看过（口径不同但时间重叠），因此本批把它当作**部分污染**样本：它可以用于"确认"，不得用于"选择"。选择只在 train60 内（或 R2 的滚动训练/验证窗内）进行。 |
| 冻结范围 | 研究代码放 `scripts/` 与 `analysis/`，通过调用方传入参数。不改 `core/`、`backtest/`、`strategies/`、`config/` 等冻结范围目录；需要的随机入场等"零模型"策略类写在研究脚本里，不放进 `strategies/`。观察窗 [2026-11-19, 2027-05-18) 内改冻结范围须走重新登记。 |
| 引擎口径 | 次 K 线开盘成交、费率、价差、滑点、冲击与参与率上限均按 [回测假设](../backtest_assumptions.md)。日线数据没有逐 bar 的 `spread_bps` 时使用配置默认值，**日线结论不能外推到日内执行**。 |
| 登记 | 每个研究运行前，协议文件与脚本的哈希写入 `reports/<研究>/registration.json`（本地、不入库）；每一次尝试，包括失败的，都经 `ResearchEvidenceRun` 记账，并受 [`analysis/search_scope.py`](../../analysis/search_scope.py) 的有限范围合同约束。任一哈希变化，脚本拒绝继续。 |
| 随机性 | 固定种子，写入协议；种子不得在看过结果后更换。 |
| 多重检验 | 六个研究共用一份累计尝试数，按**唯一候选**计（每个实际搜索过的配置只计一次，不做乘积）。R3 与 R2 对获选候选做 deflated Sharpe 时必须用 `deflated_sharpe_evidence` 的全面板结果，以其 `probability` 作门槛；单序列的 `deflated_sharpe_ratio` 只给诊断值，不能作门槛。 |

## 3. 结论分级

- **通过**：该协议"预注册判定规则"全部满足，且样本量门槛满足。
- **未通过**：样本量足够，但至少一条判定规则不满足。这是有价值的结论，同样发布。
- **证据不足**：样本量门槛没有达到（例如组合层平仓交易少于 30 笔、有效窗口少于 8 个），不对策略好坏下结论。

不允许在看过结果后增加、放宽或替换判定规则；想换指标就开新的协议条目。

## 4. 被研究的对象

| 代号 | 对象 | 备注 |
| --- | --- | --- |
| S1 | `TrendBreakout`（Donchian 20/10，OBV 开） | 只允许 TREND_UP；见 [`strategies/trend_breakout.py`](../../strategies/trend_breakout.py) |
| S2 | `TrendPortfolioV3`（默认参数） | 组合层，见 [`strategies/trend_portfolio_v3.py`](../../strategies/trend_portfolio_v3.py) |
| S3 | `RangeStrategy`（SIDEWAYS） | 样本预计很小，大概率"证据不足" |
| S4 | `VolatilityReversionStrategy`（VOLATILE） | 同上 |
| 基准 | 现金、BTC 买入持有、等权再平衡、E0 简单 20 日突破 | 见 R1 |

S3、S4 只进入 R1（零模型比较）与 R3（参数邻域），样本不足时按"证据不足"如实记录，不为凑样本拼接。

## 5. 顺序与依赖

```text
R1 零模型基线 ──► R2 滚动样本外 ──► R4 成本与容量 ──► R3 参数稳健性 ──► R5 行情与集中度 ──► R6 回撤与尾部
```

- R1 没有通过的对象，不进入 R2/R3 的调参环节（调一个没有信号的对象没有意义），但仍参加 R4–R6 的"现状体检"，
  结论标注为"无独立优势下的风险画像"。
- R4 早于 R3：成本假设一变，参数排序可能整体改变；R3 的网格在 R4 确定的基准成本（1×）下运行，
  并在 2× 成本下复核一次排序是否改变。
- R5、R6 读取 R1–R4 已登记的运行产物，不重新选择任何参数。

## 6. 协议清单

| 编号 | 协议 | 一句话 |
| --- | --- | --- |
| R1 | [零模型与被动基线](backtest_r1_null_baseline_20261116.md) | 策略是否优于随机/平移后的同结构策略与被动持有 |
| R2 | [滚动样本外](backtest_r2_walk_forward_20261116.md) | 窗口内选参，窗口外检验，调参是否比固定默认值更好 |
| R3 | [参数稳健性](backtest_r3_parameter_robustness_20261116.md) | 收益是高原还是尖峰，且扣除多重检验 |
| R4 | [成本与容量压力](backtest_r4_cost_capacity_20261116.md) | 盈亏平衡成本倍数与容量曲线 |
| R5 | [行情切片与集中度](backtest_r5_regime_concentration_20261116.md) | 收益集中在哪些行情、年份、标的 |
| R6 | [回撤与尾部风险](backtest_r6_drawdown_tail_20261116.md) | 回撤分布、恢复时间、保护止损的跳空成交 |

## 7. 本批没有做的事

- 没有写任何研究脚本，没有运行回测，没有结果。
- 没有读取 S1–S4 当前配置里的具体数值；凡写"冻结时读取"的位置，冻结前由脚本从 `config/params.yaml` 读出并写入登记。
- 没有改变数据采用、研究窗口或观察窗登记。
