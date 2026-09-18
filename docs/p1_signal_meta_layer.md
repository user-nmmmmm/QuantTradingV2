# P1：条件 EV 账本与冻结滚动研究

P1 在 P0 完整的原始候选与固定周期净收益标签上，回答“相同策略在相近的已知市场状态下，历史净期望收益及不确定性如何”。它是默认关闭的回测后处理，不是新的交易策略或实盘门控。开启后不改变正式成交、权益、风控、健康状态、分配或策略准入；P0 观察摘要也应保持一致。

论文来源：[Jimmy Hu, Regime-Aware Multiaxial Signal Meta-Layer](https://alphanet.global/Regime-Aware_Multiaxial_Signal_Meta-Layer.pdf)。采用了入场时点固定状态归属、时间衰减、条件收益账本、向总体均值收缩和信号／执行分离的思路。下述固定轴、支持下限、时间块与保守研究下界是本项目的工程选择，不声称完整复现论文或复制其收益。

## 实现范围

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

## 时间与训练数据契约

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

## 估计、支持与不确定性

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

## 使用与产物

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

## 验收边界

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

### 2026-09-18 历史验收实测

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
