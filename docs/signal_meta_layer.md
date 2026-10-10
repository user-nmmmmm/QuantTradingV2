# 信号元层 P0–P3(合并)

> 合并自 P0 观察、P1 条件 EV、P2/P3 因果软状态与影子账户三份文档。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/p0_signal_observation.md -->
## P0：原始信号观察与门控诊断

本模块落实论文讨论中的数据与验证底座：先记录“哪些信号出现、为何没有交易、之后发生什么”。它不是新的交易策略，也不实现条件 EV 拟合、软状态模型、动态权重或仓位放大。

默认关闭；只在研究回测显式启用。没有修改生产路由、健康门控、组合风控、策略准入或实盘下单。共享运行内核提供观察接口，但本批不包含实盘观察日志持久化、断点恢复和实时部署。

### 已实现的契约

| 能力 | 当前行为 | 主要实现 |
| --- | --- | --- |
| 原始候选 | 四个已注册策略都有无副作用的 `raw_entry_signal`；观察不调用会推进健康状态的 `should_enter` | `strategies/` |
| 不可变事件 | 确定性候选 ID、策略源码/参数版本、配置与成本指纹；嵌套信号与上下文冻结，重复 symbol-bar 不重复计数 | `core/signal_observation_types.py` |
| 信号时点上下文 | 趋势效率、波动比、ATR%、ADX、动量、成交额、价差、市场状态、当时策略健康/账户风险、持仓/挂单；不拟合分桶 | `core/signal_observation.py` |
| 首次门控归因 | 原始候选在正式路由前收集；事后关联实际审计路径；未经过的后续门控一律未知，提交订单不等于成交 | `core/runtime.py` |
| 固定周期标签 | 默认 1/3/5/20 根；下一根连续 bar 开盘参考价至第 H 根收盘，记录双边执行成本、适用融资、MAE/MFE；到期前不输出已知收益 | `core/signal_outcomes.py` |
| 实际结果关联 | candidate → opening order → 部分成交 → FIFO 分段平仓；退出控制器、成本、已实现 R；期末零成本估值单列 | `core/signal_actuals.py` |
| Ghost 回放 | 独立 Broker；next-bar、部分成交、参与率、订单过期、借贷/资金费、占位阻断；独立轨道和决策组内共享有限本金两种模式 | `backtest/signal_ghost.py` |
| 可审计报告 | 原始事件 JSONL、17 张 CSV、中文诊断报告、版本与异常摘要；完整报告 profile 的 manifest 另行校验观察数据摘要 | `backtest/reporting/signal_observation.py` |

观察失败、未实现原始接口的第三方策略、无法解释的门控、实际开仓成交缺少候选关联、Ghost 执行错误，会令观察状态为 `incomplete`，不会被当成没有信号。正常的数据尾部删失不代表观察功能出错。

### 时间、成本与结果口径

- 输入 K 线时间戳标记开盘；`context.available_at` 为开盘加周期，即本根收盘。策略原始接口只能拿到当前及以前的行；私有缓存仅预计算已验证的向后滚动指标。输入带 `available_at` 时还检查其可用时间。
- 候选时点的 `estimated_round_trip_cost_bps` 只用已收盘信号 bar 估算执行成本，不预知之后的波动、成交量、资金费或借贷时长。
- 参考数量在信号时固定为 `reference_notional / signal_price`。下一根跳空可能改变实际参考名义金额，净 bp 用下一根开盘名义金额作分母。
- 固定周期结果是独立信号诊断：每个候选各自计算，可以重叠；不受账户占位约束。现货做空、参与率超限、借币缺失等记录为不可执行标志，不能当成可交易收益。非 market 信号不强行按市价标签处理。
- 缺 bar、必需资金费缺失、输入无效或数据尾部不足分别标记删失；未知收益为 `null`，绝不补零。只把 `status=matured` 的标签用于成熟结果汇总。
- 固定周期标签在第 H 根收盘到期。Ghost 则在第 H 根真实 bar 收盘决定退出，下一根真实 bar 开盘撮合；两者不是同一收益口径。
- Ghost 挂单和部分成交会占位，到期撤掉开仓剩余量，退出订单可继续部分成交。没有复制策略动态退出或保护性止损，也不是“关闭某个门控后的完整策略收益”。回放成本来自同一个 Broker，随机基础滑点使用确定性的配置费率，不消耗正式账户随机数。
- `isolated_track` 按决策组、策略、标的划分独立账户；`capital_constrained` 在决策组内共享本金，提交时限制已持仓加挂单预算不超过权益一倍。跳空后敞口、可成交性和保证金仍受 Broker 的账户规则约束；这不是持续再平衡的一倍杠杆组合。
- Ghost 回放异常会保留已发生的成交与融资事实，停止该研究账户；后续信号标为未知，不重置本金重新开始。保证金不足同样停止并标为未知，不虚构强平路径或继续计算资不抵债账户的收益。空仓期间不会跨交易累计借贷天数。模拟成交成本使用执行 bar 的区间和成交量，所以完成标签的可用时间保守地放在执行 bar 收盘。
- 实际 `realized_net_pnl_ex_carry` 扣双边手续费，滑点/冲击已体现在成交价中，不再重复扣。`realized_R` 为该净损益除以对应已退出数量的初始止损风险。账户融资单列，不把无法逐候选分摊的融资当成零。
- 所有门控表都是描述性结果：候选间相关、周期可能重叠、路由选择也并非随机。既不能把被挡信号的均值视为门控的因果效果，也不能把独立账户盈亏相加为组合收益。

### 使用

普通回测中增加 `--observe-signals` 即可。以下示例只读取本地缓存，不访问交易所：

```powershell
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --observe-signals --report-profile full --seed 20260918
```

配置中的 `signal_observation` 只控制研究测量：默认期限 `[1, 3, 5, 20]`、参考名义金额 `1000`、Ghost 持有期 `5`、每个研究账户初始本金 `10000`。这些不是正式订单预算。Python 调用可使用 `BacktestEngine(signal_observation={"enabled": True})`，结果在 `result["signal_observation"]`。

`full` profile 保留行情快照与 `run_manifest.json`，其复现命令同时校验正式结果和 P0 观察结果。`workbook` / `compact` 也导出观察文件，但不提供完整行情快照和复现 manifest。观察不完整时 CLI 返回非零退出码，不表示正式账户结果被改变。

### 验收与证据

专项测试覆盖：四个接口的无副作用、深层不可变、重复事件、时间截断与未来扰动、未成熟标签、缺 bar / 缺资金费、成本不重扣、next-bar 双腿成交、部分成交、占位、本金竞争、异常不重启、实际部分平仓与退出归因、随机滑点下正式结果等价、确定性研究摘要。

复现还保存输入标的顺序并保留初始资金的原始数值类型，避免快照键名排序或整数/浮点转换导致审计摘要发生非交易层面的变化；新增测试覆盖顺序恢复与非法顺序拒绝。

```powershell
.\.venv\Scripts\python.exe scripts/run_portable_tests.py tests/test_signal_observation.py tests/test_refactor_contracts.py -q
.\.venv\Scripts\python.exe scripts/validate_signal_observation.py
```

验收脚本对同一段本地行情、配置、初始本金和随机种子分别运行观察开/关，比较正式成交、权益、基准、会计/风控摘要、策略健康及分配结果；生成新的、不会覆盖旧证据的报告目录和行情快照。默认使用 2019–2020 年 BTC、ETH、BNB 日线，是历史工程验证，不是新样本外收益证据，也没有重新使用保留期调参。

本次已保存的验收包：`reports/p0_signal_observation_20260918_final/`。先读 `acceptance.json` 和 `gate_effectiveness.md`；逐笔查 `signal_candidates.csv`、`signal_outcomes.csv`、`signal_actual_closes.csv`、`signal_ghosts.csv`。完整性、原始策略覆盖与版本见 `signal_observation_summary.json`。

2026-09-18 的本地验收结果：

| 检查 | 结果 |
| --- | --- |
| 数据范围 | BTC/USDT、ETH/USDT、BNB/USDT，2019-01-01 至 2020-12-31，各 731 根日线 |
| 原始候选覆盖 | TrendBreakout 187；TrendBreakdown 42；RangeMeanReversion 10；VolatilityReversion 228；合计 467 |
| 首次门控分区 | 接受 12；路由/状态 310；已有持仓 111；路由冷静期 27；最低名义金额 2；预热 5 |
| 固定周期标签 | 成熟 1,826；尾部删失 42；合计为 467 × 4 |
| 正式账户等价 | 观察开/关的成交、权益、基准、会计/风控摘要、策略健康、分配结果完全一致 |
| 逐笔关联 | 无缺失原始候选的正式开仓成交；候选唯一性与决策分区检查通过 |
| 快照复现 | 正式结果与观察结果的摘要均完全一致，复现退出码 0 |
| 全量回归 | 777 passed、1 skipped，另 46 subtests passed；新增 P0 专项 29 个测试用例 |
| 静态检查 | 本批新增/修改的观察、回放、报告、验收代码通过 Ruff；改动通过 diff 空白检查 |

两种 Ghost 模式合计 380 条完成平仓记录、538 条占位阻断、6 条尾部未完成记录；这是不同模拟账户的记录数，不能解释为一个账户的交易数或收益。

```powershell
.\.venv\Scripts\python.exe main.py --replay-manifest reports/p0_signal_observation_20260918_final/run_manifest.json
```

验收通过只说明 P0 测量底座成立，不改变当前策略治理状态。后续 P1 的条件 EV 账本、有效样本量/收缩、保守研究下界和严格滚动验证已经独立实现，见 [P1 契约与证据](signal_meta_layer.md)。上表和旧验收包保留 P0 完成时的历史数据；新增配置后旧 manifest 的配置摘要可能不再匹配，不应跳过校验，应使用 P1 的新联合验收包复现当前版本。


---

<!-- 合并自 docs/p1_signal_meta_layer.md -->
## P1：条件 EV 账本与冻结滚动研究

P1 在 P0 完整的原始候选与固定周期净收益标签上，回答“相同策略在相近的已知市场状态下，历史净期望收益及不确定性如何”。它是默认关闭的回测后处理，不是新的交易策略或实盘门控。开启后不改变正式成交、权益、风控、健康状态、分配或策略准入；P0 观察摘要也应保持一致。

论文来源：[Jimmy Hu, Regime-Aware Multiaxial Signal Meta-Layer](https://alphanet.global/Regime-Aware_Multiaxial_Signal_Meta-Layer.pdf)。采用了入场时点固定状态归属、时间衰减、条件收益账本、向总体均值收缩和信号／执行分离的思路。下述固定轴、支持下限、时间块与保守研究下界是本项目的工程选择，不声称完整复现论文或复制其收益。

### 实现范围

| 能力 | 当前实现 |
| --- | --- |
| 状态轴 | 既有 market_state；趋势效率 efficiency_ratio_10 的 0.25/0.5 固定分桶；波动比 volatility_ratio_8_48 的 0.8/1.2 固定分桶 |
| 归属 | 信号可用时刻一次固定；P1 使用 one-hot，账本支持非负且和为 1 的软归属；缺值保留 unknown |
| 隔离 | 按策略、源码／参数版本、方向、timeframe、P0 上下文／成本版本、horizon 分账；不混用多空或不同持有期 |
| 条件 EV | 已成熟、可执行标签的衰减加权均值，向同账本总体先验收缩；三个轴等权相加，不假定独立 |
| 支持量 | 原始记录数、衰减权重质量、Kish 有效记录数、跨币种共同时间块有效数分别输出 |
| 研究判断 | 支持充分且净 EV 保守下界严格高于门槛 → allow；充分但下界不过门槛 → veto；缺证据 → abstain |
| 验证 | 以首个候选可用时刻为全局锚点；滚动训练窗口、embargo、每段测试期冻结训练集；不做结果驱动调参 |
| 可审计输出 | 逐笔预测／结果、状态单元、训练折、分组误差、中文报告、独立研究摘要与快照复现 |

`allow` 只是“按该研究条件会通过”的诊断字段，不能授权订单。`abstain` 是未知，不可并入 veto。本阶段没有 Wasserstein 状态学习、动态归因权重、择优 horizon、仓位放大、P1 有限资金反事实组合、实时持久化或实盘接线。

### 时间与训练数据契约

候选的 `timestamp` 标记信号 bar 开盘，`context.available_at` 必须等于本根收盘。结果最早在 `entry_available_at + horizon × timeframe` 成熟；允许更晚发布，但必须按实际 `label.available_at` 延迟进入训练，不允许提前入账。

默认训练／测试／隔离期为 365／90／20 个日历日，而不是 365／90／20 根 K 线。对每折：

```text
anchor = 最早候选的 context.available_at（UTC）
test_start = anchor + train_days + embargo_days + k × test_days
cutoff = test_start - embargo_days
train_start = cutoff - train_days
训练：entry >= train_start 且 label.available_at < cutoff
预测：test_start <= candidate.available_at < test_end
```

结果成熟下限同时保证训练候选在 cutoff 之前。恰好在 cutoff 成熟的标签被排除；恰好在 test_end 的预测进入下一折。同一时刻不同标的使用相同冻结账本。折内只允许旧记录继续衰减，不能加入测试期后来成熟的标签。训练窗口锚定 cutoff，不能把 embargo 偷偷计入训练期。embargo 不必覆盖最长 horizon，严格成熟截止独立负责排除未可用结果。

只使用 P0 的 `independent_fixed_notional_signal_diagnostic` 净 bp，不混入真实交易或 Ghost 收益。删失、执行受限、P0 首次门控为 warmup/data/universe 的标签不训练，也不进入有效结果均值；这些候选的预测与排除原因仍保留。路由、持仓等其他门控不会删掉原始候选，以免只学习被正式系统选中的信号。

输入缺标签、重复身份、版本冲突、非法时刻或非有限收益使研究状态为 incomplete，不填零、不默默丢弃。账本单独使用时，相同 `(candidate_id, horizon)` 的精确重放幂等，冲突重放拒绝。研究异常输出错误摘要，不用它覆盖正式账户结果。

### 估计、支持与不确定性

设信号入场可用时刻为 `t_i`，查询时刻为 `T`，半衰期为 `H` 天，冻结状态归属为 `p_i`：

```text
w_i(T) = p_i × 2^(-(T - t_i) / H)
mass = Σ w_i
record_ESS = (Σ w_i)^2 / Σ w_i^2
raw_mean = Σ(w_i × net_return_bps_i) / mass
alpha = mass / (mass + prior_strength)
conditional_EV = alpha × raw_mean + (1 - alpha) × same_book_global_mean
```

半衰期从信号入场可用时刻算，不从标签到达时刻重新计龄。先验也只能使用同账本、同一冻结训练集。内部以相对权重计算均值／方差／ESS，独立计算绝对衰减质量：共同 aging 不会错误降低 ESS，但会降低 mass 并加强收缩；极小权重下溢不会除零。

默认 `half_life_days=90`、`prior_strength=20`。时间块以 UTC Unix epoch 对齐，宽度为 `max(block_days=5, horizon × timeframe)`；同块所有币种合并权重，然后再算 block ESS。同日 60 个高度相关币种可以有 record ESS≈60，但只有 block ESS≈1。

所有被赋非零概率的条件状态以及总体先验，必须各自满足 `record_ESS >= 20`、`block_ESS >= 8`、`mass >= 5`。支持不能从先验借给稀疏单元；未知状态、冷启动、稀疏、时间块不足、质量过旧分别记录原因。输出顶层支持量采用参与状态中的最弱值，完整明细在 axes 与单元表。

标准误取以下三者最大值：加权样本方差得到的个体标准误、带有限块数修正的时间块残差标准误、`min_std_bps / sqrt(block_ESS)`。其中默认 `min_std_bps=50` 是**波动尺度下限**，不是固定 50 bp 标准误。收缩、软状态混合、多轴混合均线性组合标准误，不按独立项平方相加使误差虚假缩小。

最终研究下界为 `EV - confidence_z × SE`，默认 `confidence_z=1.96`，门槛 `min_ev_bps=0`。这些是近似保守诊断，**没有校准覆盖率或统计显著保证**：跨时间块仍可能相关，相邻标签也可能跨块重叠。下界过零不等于盈利已证实。

### 使用与产物

以下命令只读取本地行情；P1 自动启用 P0，无须重复加 `--observe-signals`：

```powershell
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --signal-meta-layer --report-profile full --seed 20260918
```

Python 可调用 `BacktestEngine(signal_meta_layer={"enabled": True})`，也可以传完整 `EVPolicy`。结果位于 `result["signal_meta_layer"]`。独立使用 `build_signal_meta_layer(p0_payload, policy)` 不启动 Broker。

所有 profile 都导出研究文件；只有 full 提供完整行情快照与回放 manifest：

| 文件 | 内容 |
| --- | --- |
| ev_predictions.csv | 信号时点冻结预测、上下界、支持、abstain 原因、每轴详情、模型版本 |
| ev_evaluations.csv | 原预测与事后成熟标签关联；未知结果保留空值 |
| ev_cells.csv | 每折起点的条件单元、先验、收缩系数、record/block ESS |
| ev_folds.csv | 每折训练起止、严格截止、测试范围、训练记录数和训练摘要 |
| ev_diagnostics.csv | 按折／策略／方向／horizon／预测状态分组的成熟率、均值、MSE、秩相关 |
| ev_summary.json / ev_report.md | 版本、参数、完整性、限制及中文说明 |

条件模型与总体先验的 MSE 使用同一组成对有限预测；秩相关至少需要 3 条记录且两侧都有变化。不能把不同分母的误差比较为模型提升，也不能把 allow/veto 的均值差当成门控因果效应。净标签已扣执行成本，不重复扣候选时点估算成本；重叠标签不能加总成组合收益。

模型身份包含 P1 实现源码、完整政策、固定轴定义及每折训练集／截止摘要；完整研究身份另含 P0 候选、结果、决策事实摘要。候选／标签／决策输入顺序规范化，不影响评分或研究摘要。追加未来数据可改变完整数据身份，不改变已有折的模型身份或预测。manifest 分别核验正式结果、P0、P1 三组摘要；旧 manifest 没有 P1 时显式禁用，不会被新的配置默认值偷偷开启。

### 验收边界

```powershell
.\.venv\Scripts\python.exe scripts/run_portable_tests.py tests/test_signal_ev_ledger.py tests/test_signal_meta_layer.py tests/test_signal_meta_reporting.py tests/test_signal_meta_cli.py -q
.\.venv\Scripts\python.exe scripts/validate_signal_meta_layer.py
```

验收脚本使用固定历史、种子与参数对照 P0-only / P0+P1，比较正式结果、健康、分配及 P0 摘要。输出新目录，不覆盖旧包。默认使用 2019–2020 年三币日线，不碰保留期做选择；这只是既有历史上的工程验证，不是新样本外准入证据。样本支持不足、全部 abstain 也可以是正确工程结果，不能为了得到 allow 而下调门槛。

本批证据目录：`reports/p1_signal_meta_layer_20260918_final/`。先读 `acceptance.json` 与 `ev_report.md`；复现命令：

```powershell
.\.venv\Scripts\python.exe main.py --replay-manifest reports/p1_signal_meta_layer_20260918_final/run_manifest.json
```

工程测试与历史验收完成后，应先检查支持覆盖、冻结预测校准与跨时期稳定性，再决定是否设计下一阶段试验。当前不授权策略重新准入，不调整正式风险预算，也不接入实盘。

#### 2026-09-18 历史验收实测

| 检查 | 结果 |
| --- | --- |
| 行情 | BTC/USDT、ETH/USDT、BNB/USDT，2019-01-01 至 2020-12-31，各 731 根日线 |
| P0 候选／期限 | 467 个候选 × 4 个期限 = 1,868 条记录 |
| 结果完整性 | 1,826 条成熟，42 条尾部删失；成熟标签中 20 条因预热排除，1,806 条可用于对应的事后诊断 |
| 冻结验证 | 4 折；第一折从 2020-02-16 开始；训练截止、预测分区、折内冻结检查全部通过 |
| 研究判断 | allow 0；veto 43；abstain 1,825 |
| 弃权原因 | 初始训练期 1,100；有效记录数不足 647；有效时间块不足 45；冷启动 33 |
| 正式账户隔离 | 开关对照正式成交、权益、基准、报告摘要、健康与分配完全一致；P0 研究摘要也完全一致 |
| 快照复现 | 正式结果、P0、P1 三组摘要一致，复现退出码 0 |
| 全量回归 | 887 passed、1 skipped，另 46 subtests passed；结果保存于验收目录 tests_junit.xml |
| 静态与产物检查 | 本批 P1 代码与接线通过 Ruff；diff 空白检查通过；manifest 登记的 29 份产物摘要全部吻合 |

实际测试期共有 768 条候选×期限记录，其中 725 条弃权、43 条有充分支持但保守下界未通过、没有 allow。不能把初始训练期的未知记录当成测试期被拒交易；也不能把不同 horizon 当成相互独立的交易次数。

P1 研究摘要为 `905073547cb930c44daad879e51f7677355dcbc4207ff69f32ff3d7a1f633db1`；对应 P0 为 `cebecf458288d001ccc82c8b133fa30fc2401c9c7a135fe57a77d5a8abe40286`。首轮曾因把尾部删失标签误要求为成熟标签的字段结构而失败；修复兼容性后重新完整验收，原失败包保留在 `reports/p1_signal_meta_layer_20260918_attempt1_failed/`，未覆盖。没有据此调整模型参数或放宽支持门槛。

结论是工程实现与复现成立，但本历史片段没有给出满足预设保守条件的正净优势证据；不是利润改善或策略准入结论。

后续 P2/P3 已作为独立、默认关闭的研究组件实现，见 [P2/P3 说明](signal_meta_layer.md)。P1 核心源码与现有配置保持不变，旧 P1 manifest 不会隐式启用新研究层。


---

<!-- 合并自 docs/p23_signal_meta_layer.md -->
## P2/P3：软状态、动态轴归因与有限资本影子回放

范围：将 Jimmy Hu 的 *Regime-Aware Multiaxial Signal Meta-Layer* 中状态条件化与元层思想，扩展到已有 P0/P1 研究链路。P2 负责冻结状态模型、条件 EV 与动态权重；P3 负责独立的交易政策回放。**默认关闭，不修改正式交易、健康状态、资金分配、风险阈值或策略准入。**

这不是论文全部实验的复刻，也不是把论文中的收益数字移植到本项目。当前没有跨信号 composite 合成器、自动状态数扩容、在线再训练或实盘接入。

### 1. P2 实现及防前视

每个账本由策略、信号版本、方向、时间周期、成本/观察快照和持有期限共同确定；不同方向和期限不混账。三个轴分别使用已知特征 `return_12`、`efficiency_ratio_10`、`volatility_ratio_8_48`。

每折使用预先声明的训练窗口（默认 365 天）、隔离段（20 天）和测试段（90 天）。训练窗口再次按时间切分：

1. 较早 120 天拟合状态原型，只纳入入场和标签均在拟合截止前的样本。
2. 对每个轴，用该特征的历史近邻构造净收益经验分位数。训练分布排除样本自身，推断分布只使用冻结训练集与当前已知特征。
3. 在分位数网格上近似一维 Wasserstein-2 距离，执行确定种子、多次初始化的 K-means；分位数均值是该离散表示的重心。软归属由负平方距离的 softmax 给出，温度来自训练期簇内距离。样本不足、常数特征或模型不可用时显式返回未知，不伪造高置信度状态。
4. 较晚训练窗口逐个信号重放：先入账严格早于当前入场时刻成熟的标签，再冻结当前入场的软归属及各轴预测；当前信号自己的结果不可用于预测自己。结果成熟时使用入场时保存的归属，不按新状态回写。
5. 动态归因仅使用截止前成熟的这些历史冻结预测，且所有轴使用同一支持充分的样本集。正 Spearman 相关的平方作为权重原料，经上下限、EMA 与有界单纯形投影，最终权重和为 1 且每轴保持在边界内。缺乏足够样本、有效时间块或正相关证据时等权回退；时间块不保证统计独立。
6. 外层测试期间模型、EV 账本与轴权重冻结，不用测试期标签更新。每次重新拟合状态模型都重建 EV 账本，不把不同质心的同名状态单元混在一起。

P1 的衰减、收缩先验、有效样本、时间块和保守下界机制被复用；每个有正概率的状态都需要满足支持门槛。动态加权不能绕过弃权条件。

#### 与论文的明确差异

- 分布构造采用**历史特征近邻**，确保信号当时可推断；不是用当前信号的未来收益构造分布。W2 在有限分位数网格上近似，非任意精度直方图运输求解。
- 软归属不是已校准的真实状态概率。簇间距离、熵只描述模型，不证明预测有效。
- 负相关不取平方奖励；额外的有界投影保证平滑后的最终权重仍满足边界。
- 所有轴都按方向隔离；不复制论文某些轴的对称合并。
- 不使用论文的 1.5 倍放大仓位；P3 上限不超过固定名义基线。未实现跨策略信号合成或 native score 到 bp 的通用校准器。

### 2. 默认政策与接口

`core/signal_adaptive_types.py` 声明独立、冻结且可序列化的政策；不修改 `config/params.yaml`，以保留旧 P0/P1 配置摘要和精确复现。

| P2 参数 | 默认值 |
| --- | --- |
| 每账本每轴状态数 | 2 |
| 拟合子窗口 / 最少样本 | 120 天 / 40 |
| 近邻 / 分位数网格 | 20 / 21 |
| 重启 / 最大迭代 / 种子 | 3 / 50 / 20260918 |
| 归因窗口 / 最少样本 / 最少有效时间块 | 90 天 / 50 / 8 |
| 轴权重下限 / 上限 / EMA 更新率 | 0.15 / 0.60 / 0.25 |
| 其余 EV 支持与冻结窗口 | 继承 `EVPolicy` 默认值，不因历史结果放宽 |

```powershell
# P2；自动启用 P1 与 P0
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --adaptive-signal-meta --report-profile full --seed 20260918

# P2 + P3；自动启用全部研究依赖
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --signal-meta-replay --report-profile full --seed 20260918
```

Python 接口为 `BacktestEngine(signal_adaptive={"enabled": True}, signal_meta_replay={"enabled": True})`，也可以传入完整政策对象。返回键为 `signal_adaptive` 和 `signal_meta_replay`；关闭时为 `None`。默认关闭以及随机滑点开关两种情况均有隔离回归。

P3 开启会依次打开 P2/P1/P0，但只改变这些依赖的 enabled 值，不暗改其参数。P3 持有期限必须已登记在 P0 horizons 中，不自动新增训练标签。

### 3. P3 的三个独立政策账户

各账户独立从 10,000 初始资本开始；同一政策内候选竞争真实资金和标的占用。固定名义 1,000，交易前持仓加待成交预留预算不超过标记权益。它不是正式账户，也不是为每笔交易重新充值的标签组合。

| 政策 | allow | veto | abstain |
| --- | --- | --- | --- |
| baseline | 固定名义 | 固定名义 | 固定名义 |
| gate | 固定名义 | 不开仓 | 不开仓 |
| sizing | 下界 / 100 bp，裁剪至 0.25～1 倍 | 不开仓 | 预先声明的 0.25 倍 |

弃权减仓不是正 EV 证据。基线仅排除 P0 预热、数据、universe 不合格候选，忽略正式路由等后续门禁；三政策都不使用事后标签或事后执行标志挑选订单。

- 同刻按 native score 降序、策略、标的和候选 ID 确定顺序，非因容器遍历顺序抢占资金。
- 收盘时决定订单，下一真实 bar 撮合；默认自首次成交计 5 根真实 bar，期满撤销未完成开仓，下根真实 bar 平仓。部分成交、订单 TTL、部分退出都保留真实账户路径。
- 手续费、滑点、融资成本使用隔离的研究 Broker；不共享正式 Broker 或随机数状态。研究账户采用配置中的确定性滑点率。
- 融资缺失、执行异常、保证金违约或候选数据缺失不能当作正常零收益；保留成交事实，停止受影响路径并使账户绩效未知。
- 尾部未完成候选保持删失，不强行平仓；报告分别列出已平候选损益、期末标记权益和未平持仓。缺行情标的使用最后已知标记并明确标为过时价格。

P3 只是预先声明的政策对比，不能将差值叫作因果收益提升；尤其 gate 不交易时的零收益不是模型成功证明。

### 4. 产物、复现与验收

P2 产物包括 `p2_predictions.csv`、`p2_evaluations.csv`、`p2_folds.csv`、`p2_cells.csv`、`p2_regime_models.json`、`p2_attribution.csv`、`p2_calibration_predictions.csv`、`p2_diagnostics.csv`、`p2_summary.json`、`p2_report.md`。

P3 产物包括 `p3_candidates.csv`、`p3_accounts.csv`、`p3_equity.csv`、`p3_fills.csv`、`p3_financing.csv`、`p3_execution_audit.csv`、`p3_summary.json`、`p3_report.md`。

三个报告 profile 都导出研究表；full 还写入数据快照及 manifest。manifest 独立登记 P0/P1/P2/P3 政策、摘要和产物。旧 manifest 明确关闭新功能；新研究缺少摘要/依赖时拒绝复现，摘要不符时返回失败。

P2 报告在共同有限预测、成熟可执行标签上比较 prior、P2 等权、P2 动态及可选 P1 参考的 MSE。每行单独报告共同样本数，不能跨不同分母排名；预测误差也不是交易收益。报告同时给出未知状态、熵、训练分离度、样本不足和等权回退，避免只看总平均。

```powershell
.\.venv\Scripts\python.exe scripts/run_portable_tests.py -q tests/test_signal_regime_model.py tests/test_signal_axis_attribution.py tests/test_signal_adaptive.py tests/test_signal_meta_replay.py tests/test_signal_adaptive_reporting.py tests/test_signal_adaptive_cli.py
.\.venv\Scripts\python.exe scripts/validate_signal_adaptive.py
# 用新验收目录中的 run_manifest.json 替换路径
.\.venv\Scripts\python.exe main.py --replay-manifest reports/<run>/run_manifest.json
```

验收脚本使用已有 2019–2020 三币日线做开关对照，目录必须新建、不得覆盖旧证据；检查正式摘要、健康、分配与 P0/P1 完全相同，并保存新研究报告和快照。不使用结果选择参数，也不把已有历史叫作新的未见样本。旧 P1 验收目录仍须可精确重放。

自动测试覆盖 W2/重心/软归属、未来结果扰动、追加数据、同刻标签、冻结入场状态、模型重拟合隔离、归因稀疏回退、权重边界、真实资金竞争、两端 next-bar、部分成交/退出、融资错误、尾部删失及新旧 manifest。

#### 2026-09-18 本地验收结果

最终证据目录：`reports/p23_signal_meta_20260918_verified/`；先读 `acceptance.json`、`p2_report.md`、`p3_report.md`。工程检查全部通过：

- 全项目测试：1,079 passed、1 skipped、46 subtests passed；覆盖率 87.99%，高于 55% 门槛。运行中有 2 条 SQLite 未释放资源警告，无测试失败。
- 未知轴熵最终统一为 `null`，不是 0；修订后 P2、P3、CLI 和报告共 93 项回归通过。全量与补充 JUnit 分别保存为 `tests_junit.xml`、`entropy_followup_junit.xml`。
- Ruff、关键运行时类型检查、依赖环境和锁文件校验通过。
- 相同种子、相同三币日线开关对照：正式账户 74 笔成交不变；成交、权益、基准、报告摘要、策略健康、资金分配、P0/P1 研究摘要全部一致。
- 旧 P1 manifest 保持精确复现；最终新 manifest 的正式结果与 P0/P1/P2/P3 摘要全部一致，退出码 0，记录在 `replay_verification.log`。
- manifest 登记的 47 份产物逐一核验 SHA-256 与大小，无不符。测试日志为额外附件，不重写已登记证据。

研究覆盖情况：467 个候选 × 4 个期限 = 1,868 条预测，4 个冻结测试折。1,100 条处于初始训练期；测试期的 768 条均因未知状态而弃权。66 个账本/折拟合记录中，没有一个三轴全部可用；趋势轴和效率轴各 6 个可用，波动轴 0 个可用。部分早期窗口中趋势/效率有 43～46 个样本，但波动轴仅 36～38 个，未达到预先声明的 40 个有效特征样本门槛；不能为消除弃权而事后降到 36。80 个归因账本全部等权回退。

| 影子政策 | 期末标记收益 | 最大回撤 | 成交笔数 | 期末未平持仓数 |
| --- | ---: | ---: | ---: | ---: |
| baseline | 25.10% | 8.15% | 254 | 2 |
| gate | 0.00% | 0.00% | 0 | 0 |
| sizing | 6.28% | 2.15% | 254 | 2 |

这些是既有历史上的政策路径，不是实盘业绩。gate 没有交易，因此不能宣称择时成功；sizing 在本片段中始终执行预设的四分之一弃权规模，更低回撤主要反映更低仓位，并未验证动态归因或门控带来增益。两项有交易政策的期末收益含未平仓标记损益，不能等同于已实现收益。

最终 P2 摘要：`7ac39d3816ae66fa8ef5162d8160bf7acd41ea9b17c12b098e930173b2d73474`。
最终 P3 摘要：`165ce7074ef7aa844915dc94fe10137f30c0578ec34aa09619b24a8ac648b8bb`。

首轮包 `reports/p23_signal_meta_20260918_final/` 保留未覆盖，但其中逐笔未知状态的熵仍为 0；随后仅修正该诊断字段、重新完整运行并生成上述 verified 包，没有调整模型参数或交易政策。当前源码应重放 verified 包，不以旧包的代码身份比较失败作为新结果失败。

```powershell
.\.venv\Scripts\python.exe main.py --replay-manifest reports/p23_signal_meta_20260918_verified/run_manifest.json
```

结论：P2/P3 工程能力和证据链完成；本片段仍不提供满足门槛的正净优势、动态权重有效性或正式策略准入证据。下一阶段应预先冻结数据窗口和检验方案，再积累足够样本，而不是根据本表调参。
