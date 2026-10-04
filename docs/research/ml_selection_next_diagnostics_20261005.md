# ML 选币本轮候选流、敞口与奖励诊断

本报告依据 `reports/ml_selection_next_full3_20261005` 的真实运行产物编写。冻结协议为 `ee4fb26d17e74a277047cdadd8bf6afb1f05dd3906e1b2e5bc1131c9366554f1`，覆盖登记的 60 个币。训练账户区间为 2020-01-01 至 2022-12-31，开发验证账户区间为 2023-01-01 至 2024-06-30。本轮属于回顾性研究，历史成员资格仍未完整证明。

低敞口首先来自原引擎的市场状态、策略信号、持仓、冷却和分配约束。验证原规则只有 51 个候选日期、21 个竞争日期；它们不能由完整特征表的行数替代。概率接近 0.5 的 RL 在不同门槛下确实能形成交易、少量交易或全现金账户，但“接受动作的概率”不是盈利概率。新回合的费用已进入原引擎净权益，本轮正式奖励为净权益的对数变化，没有另加重复费用或强制交易奖励。

本文件的候选流、原始训练诊断、退出/分配和学习证据来自 source3 已完成部分；该整轮后来在 WF1 部分成交处中断，不能称为完整成功运行。source4 修复账户浮点残余后重放主窗口十个 test、两组费用和三组执行情形，各项净收益、回撤、敞口、实际成交、奖励与账务值同 source3 完全一致；source4 又在 WF2 低参与率期末无法完成退出时中断。原失败记录和原数据均保留。

正式训练实际合计为 **124 次更新**：source3 主窗口三个种子 20/23/20，加 WF4 种子 43/44 各 20，再加 source4 WF4 种子 42 的 21；资源 pilot 的 18 次更新不计入正式预算。最终历史矩阵入口为 [full5 回执](../../reports/ml_selection_next_full5_20261005/next_execution_receipt.json)，主窗口及 WF1 评价明确继承 source4，其余窗口和失败情形由 source5 完整汇总；不把复制结果或零更新恢复冒充新增训练。

最终证据汇总为 [source6 回执](../../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)，历史矩阵来源继续保持上述范围。source6 修正桥接在风险阻断时误改原本已拒绝记录的审计原因；固定政策的 144 个原引擎状态、完整审计和动作全部重放一致，与 source5 的逐日权益、成交和奖励文件字节完全相同。source5 原完整审计 136/144 一致的结果已封存，不回填为通过；source6 新训练、门槛搜索和新增财务候选均为 0。[桥接修正经济路径核对](../../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)保留两版比较。

继承策略的固定验证在 source4 原引擎重放为 5/5 完全一致，仍不声称原训练已证明等价于修后重训；来源、路径解析和登记勘误见 [固定策略验证](../../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)、[恢复勘误](../../reports/ml_selection_next_full4_20261005/recovery_registration_erratum.json)、[执行与归档说明](../ml_selection_next_execution_20261005.md)。本文保留原始 source3 证据链接，所有窗口结果详见 [滚动与执行压力解读](ml_selection_next_robustness_20261005.md)。

**记录口径与证据。** 新运行显式启用完整研究审计，保留决策前现金、权益、已持仓币种、实际访问的门槛事实、原始策略信号、资本批准、仓位限制、风险预算和订单身份。训练原规则的 51,877 条 observation 全部具有 `gate_facts`，其中 380 条实际产生 `original_signal`；验证原规则的 31,952 条 observation 全部具有 `gate_facts`，其中 109 条产生原始候选。

这里的拒绝数来自 observation 顶层最终 `reason`，每条 observation 只计一次。内部 `gate_facts` 是访问过程的状态记录，不能简单按其中 `reason` 的文字累计失败。例如真实成功单的事实序列也有 `allocation_rejected` 标签，同时其资本批准数量为正；后面的 `zero_sizing` 标签也可以伴随正的 `sized_qty`。判断该阶段结果必须结合批准数量、资本分配原因、`allowed`、仓位限制和最终订单结果。

某币先被 `market_state_cash` 拦住时，本轮没有观察到它在下游策略、模型、分配和执行阶段会发生什么。以下是实际访问路径的描述，不能把未访问阶段的信号或盈利能力当作反事实结论。

证据：[训练原规则 summary](../../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/summary.json)、[完整 observation](../../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/entry_observations.json)、[训练诊断](../../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/diagnostics.json)、[验证原规则 observation](../../reports/ml_selection_next_full3_20261005/episodes/validation_native/entry_observations.json)。

**原规则为什么仍大部分时间持有现金。** 训练账户平均现金占权益 95.4337%，平均总敞口占权益 4.5663%；账户净收益 64.4703%，最大回撤 8.1638%。这些是一个历史账户路径的结果，没有说明增加交易次数会改善收益。

| 最终观察到的门槛或结果 | 训练原规则 observation 数 | 验证原规则 observation 数 |
| --- | ---: | ---: |
| 市场状态要求现金 `market_state_cash` | 40,364 | 25,026 |
| 已访问策略没有产生信号 `no_signal` | 5,603 | 4,119 |
| 已持仓 `position_held` | 2,248 | 791 |
| 路由冷却 `router_cooldown` | 1,564 | 1,012 |
| 路由切换冷却 `router_switch_cooldown` | 783 | 506 |
| 历史预热 `warmup` | 780 | 120 |
| 分配拒绝 `allocation_rejected` | 290 | 66 |
| 策略健康限制 `strategy_health_block` | 100 | 251 |
| 组合限制 `portfolio_block` | 47 | 0 |
| 币种入场限制 `symbol_entry_blocked` | 0 | 10 |
| OBV 策略过滤 `obv_filter` | 8 | 8 |
| 订单获接受 `order_accepted` | 90 | 43 |
| 合计 | 51,877 | 31,952 |

市场状态现金占训练记录 77.8071%、验证记录 78.3237%。训练阶段 380 个原始候选中，290 个最终被分配拒绝，90 个形成开仓订单；验证阶段 109 个原始候选中，66 个被分配拒绝，43 个形成开仓订单。门槛之后的实际成交还必须用订单和成交账本核对，不能把候选数、被 selector 接受的数量、开仓数与成交行数混在一起。

验证原规则平均现金占权益 97.2781%，平均总敞口仅 2.7219%，账户状态为 `strategy_health_paused`，未因账户风险终止。低敞口在加入 ML 之前已经存在，且验证期健康限制的终止记录为 251 条。它们说明本轮确实经历了健康控制器的暂停路径，不等于证明这些被拦住的交易本来会盈利。

**全部监督验证方案的真实机会。** 以下每一行都是单独重放的原引擎账户路径，均有 547 条被评价的权益记录、没有冻结尾部权益被计入奖励，账务核对均通过。表中敞口用百分比表示；原字段 `mean_gross_exposure_pct_equity` 保存的是比例，展示时乘以 100。

| 方案 | 原始候选数 | 候选日期 / 竞争日期 | selector 接受数 | 实际成交行数 | 平均总敞口 | 净收益 | 最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 原规则 native | 109 | 51 / 21 | 未使用 selector 计数 | 132 | 2.7219% | -0.3620% | 6.6533% |
| 合格候选原规则 qualified_native | 109 | 51 / 21 | 109 | 132 | 2.7219% | -0.3620% | 6.6533% |
| 动量 momentum | 110 | 52 / 21 | 110 | 132 | 2.5726% | -0.7384% | 7.0081% |
| LightGBM 回归 | 110 | 52 / 21 | 110 | 132 | 2.5726% | -0.7384% | 7.0081% |
| Ridge | 191 | 77 / 33 | 143 | 124 | 3.3879% | -2.7891% | 8.2604% |
| LambdaRank | 196 | 80 / 34 | 147 | 116 | 3.0274% | -4.6808% | 9.9851% |
| 随机排序 seed 42 | 110 | 52 / 21 | 110 | 132 | 2.7089% | -0.3597% | 6.6533% |
| 随机排序 seed 43 | 110 | 52 / 22 | 110 | 132 | 2.3843% | -0.4674% | 6.7559% |
| 随机排序 seed 44 | 110 | 52 / 21 | 110 | 132 | 2.4772% | -0.6326% | 6.8439% |

各方案后续持仓、冷却、健康状态不同，候选机会也随账户路径变化。Ridge 的 77/33 和 LambdaRank 的 80/34 不能解释为模型从同一批固定原规则机会中创造了额外独立样本。验证期这些监督方案均未取得正净收益。LightGBM 被选为 RL 的监督 parent，是三个监督模型之间按已登记验证权益奖励进行的比较；这不构成其通过生产准入的证据。

九个验证方案的完整漏斗均报告 31,952 个 identified decision、零 unidentified decision、零 unlinked actual fill 和空的 unlinked opening order ID 列表。各方案的上游门槛分布与账户现金/敞口均保存在各自的 `summary.json` 与 `diagnostics.json`，不是把原规则的门槛计数复制给所有模型。

Ridge 的最终记录含 44 次 `model_gate` 拒绝、4 次流动性拒绝、103 次分配拒绝和 40 次订单接受；LambdaRank 对应为 45、4、105 和 42。排序分数不能解释成预期收益：LambdaRank 的收益 gate 是另一个只在保留训练行拟合的 Ridge，而非用排名分数代替收益门槛。

证据：[全部监督验证比较](../../reports/ml_selection_next_full3_20261005/validation_comparison.json)、[监督模型选择](../../reports/ml_selection_next_full3_20261005/parent_model.json)、[Ridge 诊断](../../reports/ml_selection_next_full3_20261005/episodes/validation_ridge/diagnostics.json)、[LambdaRank 诊断](../../reports/ml_selection_next_full3_20261005/episodes/validation_lambdarank/diagnostics.json)。

**真实竞争与排序影响。** 原规则训练期有 163 个候选日期、73 个至少两个候选的竞争日期；验证期只有 51/21。相同候选批次的分配对照实际记录 12 批，11 批遇到资金或持仓容量限制，1 批改变了获批候选或数量。总体遇到 21 个竞争批次，但对照预登记上限为 12；其余批次不能据此推断。

2023-02-05 的真实账户状态下，固定原候选为 XTZ/USDT 与 BAT/USDT。原评分及固定随机评分分别批准 BAT 数量 13,284.015259；模型及动量评分分别批准 XTZ 数量 3,542.186156。当时已有七个币的持仓，被拒绝的候选显示 `position_slots_exhausted`。这说明排序在真实持仓容量竞争下确实可能改变入选，而非所有批次都只改一个无关的排序列表。

此对照复制当时原引擎的 allocator 对象图并调用原分配链，未执行撮合，各方案新增成交数均为零，`portfolio_return_difference` 明确为 null。因此 1/12 是“同批次审批或数量改变”的测量，不能写成 1/12 的盈利改善或选币收益提升。

证据：[同批次原引擎分配对照](../../reports/ml_selection_next_full3_20261005/fixed_batch_allocation_comparison.json)。

**成交与退出的联接。** 训练原规则的 90 个开仓 decision 均有真实 order ID、开仓成交及退出联接；唯一开仓 fill ID 为 90 个，唯一退出 fill ID 为 420 个，两者并集恰好等于实际 fill ledger 的 510 行。验证原规则为 43 个开仓、89 个唯一退出 fill ID，并集等于 132 行实际成交。两个回合均没有未识别 decision、未联接实际成交或未联接开仓 order ID。

完整训练 decision ledger 仍有 51,787 行 `execution_linkage=unknown`，验证有 31,909 行；这些是没有开仓订单的路径记录，不能把它们写成同数量的丢失成交。`individual_equity_contribution` 保持未知：订单、lot 和退出的联接没有把组合净权益虚构分摊给每个候选。

证据：[训练 decision ledger](../../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/decision_ledger.csv)、[训练 fill ledger](../../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/fill_ledger.csv)、[验证 decision ledger](../../reports/ml_selection_next_full3_20261005/episodes/validation_native/decision_ledger.csv)、[验证 fill ledger](../../reports/ml_selection_next_full3_20261005/episodes/validation_native/fill_ledger.csv)。

**旧 77/22 的准确含义与新奖励。** V1 旧运行的 77 个已存回合全部完成权益、成本、奖励与终止对账，其中 22 个回合净收益为正但总奖励为负。77 和 22 都是回合数量，不是收益率。旧 seed 42 的训练回合 2，净权益对数收益为 0.3457220741，累计回撤加深惩罚为 0.5759756949，换手惩罚为零，奖励为 -0.2302536208。惩罚项确实改变了优化目标；对账最大逐项误差为零，不能把该现象直接归因为账务漏费或重复扣费。

旧 seed 42 训练回合 1 的净权益对数收益为 0.1187704382、回撤惩罚为 0.2354599787，总奖励 -0.1166895405。新 full3 main42 首回合保留同一净权益对数收益 0.1187704382，但正式预登记的回撤和换手系数都为零，奖励因此等于 0.1187704382。新 main42 首两回合的训练及三个门槛验证共八套实际账本已逐行核对 `reward=log(equity/previous_equity)`，最大误差为零、账务 discrepancy 均为零、无重复扣费。

这没有将手续费、滑点或融资从奖励里删除：它们已经进入原引擎的净权益；当前正式奖励没有再重复减去这些费用。独立 9-cell reward/resource pilot 在 0、0.25、0.5 三个回撤系数、三个种子下全部成功并提交 18 次更新，但不计入正式最低更新数，也未用于打开历史 test。

旧报告仍缺少上游原策略 observation 和完整 opening order 联接；旧历史 test 已被观察。旧 77 个回合只能按其真实已存账本解释，不能重建其全部门槛反事实，不能升级成独立样本，也不能覆盖成新的 V1 有效裁决。

证据：[V1 只读对账](../../reports/ml_selection_next_full3_20261005/v1_diagnostic/audit.json)、[main42 首两回合语义核对](../../reports/ml_selection_next_verification_20261005/main42_first_two_semantic_check.json)、[完整 reward/resource pilot](../../reports/ml_selection_next_full3_20261005/pilot_resource_report.json)。

**全拒绝与概率门槛。** V1 的已观察 `test_rl` 有 338 个保存概率，范围 0.4841661606–0.4992113512，全部低于默认 0.5，因此接受动作数为零。其中 331 个概率距 0.5 不超过 0.01。旧报告在 0.49 下会接受 331 个的计数只是固定已存概率的敏感性描述，其 `economic_result` 为 null；它没有重放新的账户路径，不能据此认定降门槛会赚钱。

本轮只在开发验证比较预登记 0.49/0.50/0.51。main44 已独立完成 20 次实际更新、20 个回合；其最佳验证 checkpoint 是回合 15，选择门槛 0.49，净收益 2.9679%、净权益奖励 0.0292470112、账务核对通过。以下是该同一 checkpoint 重新运行原引擎的三条验证路径：

| 门槛 | 原候选 decision | 保存 policy 概率 | 概率门槛拒绝 | 前置流动性拒绝 | selector 接受 | 实际成交行数 | 平均总敞口 | 净收益 | 奖励 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.49 | 305 | 291 | 166 | 14 | 125 | 149 | 5.3505% | 2.9679% | 0.0292470112 |
| 0.50 | 315 | 301 | 281 | 14 | 20 | 40 | 3.6560% | -1.1935% | -0.0120066313 |
| 0.51 | 428 | 414 | 414 | 14 | 0 | 0 | 0.0000% | 0.0000% | 0.0000000000 |

0.51 路径保存概率的最大值为 0.5071964624，因此全部 414 个实际 policy 动作被确定性拒绝；另外 14 个候选在 policy 前就被流动性规则拒绝，没有保存 policy 概率。纯现金回合奖励零且费用零是合法账户结果，没有额外强制交易奖励。

回合 15 更新后的**同一批 403 个训练轨迹候选**，概率中位数为 0.4798277158、最大值 0.5036024074、平均熵 0.6904953892。固定这批训练特征，0.49/0.50/0.51 两侧计数分别为接受 94/10/0、拒绝 309/393/403。这解释了门槛附近的集中分布为什么对确定性动作十分敏感，但训练轨迹计数不能替代上表三个验证账户的真实成交与收益。三个验证账户的持仓、健康与后续候选不同，完整重放后的机会数也不同。

训练动作仍由 seed 44 的 Bernoulli 随机采样产生，不受验证门槛影响。该回合梯度范数 0.2810968148、未触发梯度裁剪；最佳 checkpoint 的更新水位为 15，最终完成水位为 20。main44 最佳模型的 `metadata.evaluation_threshold=0.49` 已纳入其模型 ID。它是三个正式种子中一个比较对象；最终冻结候选来自 main43，不能把 main44 的开发验证最佳值写成本轮最后 winner 或历史 test 表现。

证据：[main44 完整训练历史](../../reports/ml_selection_next_full3_20261005/rl_seeds/44/rl_training.json)、[0.49 验证 selection](../../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_49/selection.csv)、[0.50 验证诊断](../../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_5/diagnostics.json)、[0.51 验证诊断](../../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_51/diagnostics.json)、[独立执行原始资源及 SHA256 收据](../../reports/ml_selection_next_verification_20261005/main_seed44_auxiliary_completion_receipt.json)。

**最终冻结候选与实际历史 test。** 主窗口 seed 42/43/44 分别提交 20/23/20 次更新，三个种子均达到预登记的最低 20 次。候选来自 main43 的最佳 checkpoint，其更新水位为 20；完成训练的最新水位为 23。这 63 次更新是相同历史账户上的参数更新，不是 63 个独立市场样本。

候选于 `2026-10-04T19:54:35.861478+00:00` 冻结为 RL，模型 ID 为 `9db8c77c77e58997798c005b016f34a07bfb85a3fb7d77eacdce8f25bb8b851f`，监督 parent 是 LightGBM `1e7f2d876452069b06dc2dc0702dfcc430c35c643d1b44688ccb98c7773cdb90`。实际 metadata 门槛为 **0.51**；候选文件记载 `test_used_for_selection=false`、开发验证资格检查通过，选择依据为预登记门槛下的开发验证净权益奖励。该候选验证净收益 5.5041%、奖励 0.0535797875、最大回撤 5.0123%、平均总敞口 2.0560%，selector 接受 4 个候选，实际成交账本为 32 行。main43 在 0.51 下仍产生交易，与前文 main44 在同门槛下全现金的结果不同；门槛数字本身不能决定另一模型是否全拒绝。

历史 test 区间为 2024-07-01 至 2026-09-18。以下十个方案均按已冻结设置重放；完整账户均未因风险终止、未计入冻结尾部权益，账务检查均通过。

| 方案 | 原始候选数 | 候选日期 / 竞争日期 | selector 接受数 | 实际成交行数 | 平均总敞口 | 净收益 | 最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 原规则 native | 172 | 60 / 21 | 未使用 selector 计数 | 108 | 1.9696% | -4.1013% | 7.6314% |
| 合格候选原规则 qualified_native | 172 | 60 / 21 | 164 | 102 | 1.9593% | -3.4045% | 7.0368% |
| 动量 momentum | 168 | 60 / 21 | 160 | 102 | 1.9709% | -2.9897% | 6.6376% |
| LightGBM 回归 | 168 | 60 / 21 | 160 | 102 | 1.9709% | -2.9897% | 6.6376% |
| Ridge | 214 | 95 / 25 | 144 | 88 | 1.9703% | 3.3273% | 6.4339% |
| LambdaRank | 219 | 95 / 25 | 149 | 88 | 1.9525% | 3.2118% | 6.5386% |
| 随机排序 seed 42 | 170 | 60 / 21 | 162 | 102 | 1.9647% | -3.2422% | 6.8806% |
| 随机排序 seed 43 | 171 | 60 / 21 | 163 | 102 | 1.9668% | -3.1151% | 6.7583% |
| 随机排序 seed 44 | 172 | 60 / 21 | 164 | 102 | 1.9686% | -3.2887% | 6.9253% |
| 冻结 RL main43，门槛 0.51 | 342 | 146 / 44 | 2 | 4 | 0.7406% | 2.8052% | 2.5961% |

Ridge 和 LambdaRank 的历史 test 净收益高于冻结 RL，但没有因此改选候选。本轮开发已经见过旧历史 test，新的重放保留回顾性身份；这十个结果也不能作为十份独立最终裁决。

冻结 RL 的 45,564 条 observation 中，39,380 条被市场状态现金门槛终止，4,017 条为已访问策略无信号，1,113/559 条为路由/切换冷却，63 条为已持仓，82 条为币种入场限制，8 条为 OBV 过滤。实际原始候选为 342 个，10 个在 policy 前被流动性规则拒绝；其余 332 个保存概率中，330 个低于 0.51、2 个达到门槛。保存概率范围为 0.4931642103–0.5104824683，中位数 0.5027950275。两次接受均提交真实开仓订单，形成两行开仓成交及两行退出成交，全部实际成交均联接成功、没有未识别 decision 或未联接 opening order ID。

该账户不是全现金：平均现金占权益 99.2594%，平均总敞口 0.7406%，最后成交日期为 2024-12-24。两个实际开仓为 2024-11-21 的 BTC/USDT 与 2024-11-23 的 XRP/USDT，分别于 2024-12-24、2024-12-23 退出。其净收益和较低回撤由极少开仓形成，不能推断为广泛候选上的稳定选币能力。342 个候选和 146 个候选日期也不是 342 笔交易或 146 个有成熟结果的独立事件组；未来观测的 decision 日期更不能用这些历史日期补足。保存概率在 0.50 下会接受 301 个的敏感性计数仍为 `economic_result=null`，不能据此对 test 降门槛或宣称额外收益。

RL 的 810 行实际权益/奖励记录对账通过，净权益奖励为 0.0276653180，等于净权益对数收益；回撤和换手附加惩罚均为零，各项奖励重建最大误差为零。账务检查 discrepancy 为零，最大绝对差为约 `2.91e-11`；手续费 35.59695066、滑点成本 75.01443234 已进入净权益，未重复扣减。结束采用 `forced_liquidation`，未因风险、健康暂停或账户停用截断；其余九个 test 账户各有 811 行被评价的权益记录。

已存费用压力的 1.5/2.0 倍账户净收益分别为 2.7495%/2.6938%，最大回撤为 2.6230%/2.6498%，均为四行实际成交、账务通过且未因风险终止。三种执行压力也已实际完成：

| 执行压力 | 登记参数 | 净收益 | 最大回撤 | 实际成交行数 | 账务 / 风险终止 |
| --- | --- | ---: | ---: | ---: | --- |
| 资金限制 limited_cash | 初始资金 10,000 | 2.8052% | 2.5961% | 4 | 通过 / 无 |
| 低参与率 low_participation | 最大参与率 0.00001，开仓单有效期 3 bars | 2.8058% | 2.5955% | 5 | 通过 / 无 |
| 开仓延迟 opening_delay_1d | 额外延迟 1 个日线 bar | 2.4461% | 2.3722% | 5 | 通过 / 无 |

这些压力测量只说明登记情形下这条稀疏账户路径的收益、风险及账务结果，没有消除收益集中和独立性限制。实际 `adjudication.json` 将收益、风险、会计和证据分别裁决：净收益为正、超过原规则、1.5/2.0 倍费用仍为正；回撤在登记限制内，主账户及执行压力均无风险终止；主账户与全部压力账务通过。证据层仍记载历史 PIT 未完整证明、独立最终窗口未打开、独立事件组未证明、前向成熟度 pending。

集中度按固定 5 日开仓块分组，实际只有两个 cohort，`status=insufficient`，`independent_cohorts_proven=false`。移除最高五组或十组后剩余净 PnL 都为零；由于组数不足，`positive_after_top_5` 检查保存为 **null**，不是已通过的集中度检查。因此最终 `retrospective_checks_passed=false`、`formal_admission=false`、`independent_holdout=false`，身份为 `retrospective_research_only`。收益与账务通过不能覆盖这一证据不足裁决。

证据：[候选冻结记录](../../reports/ml_selection_next_full3_20261005/candidate.json)、[实际主窗口预算](../../reports/ml_selection_next_full3_20261005/rl_budget_receipt.json)、[冻结 policy 及门槛](../../reports/ml_selection_next_full3_20261005/models/policy_best.json)、[十个历史 test 比较](../../reports/ml_selection_next_full3_20261005/test_comparison.json)、[冻结 RL test summary](../../reports/ml_selection_next_full3_20261005/episodes/test_rl/summary.json)、[RL 概率/漏斗/对账](../../reports/ml_selection_next_full3_20261005/episodes/test_rl/diagnostics.json)、[真实 RL 成交账本](../../reports/ml_selection_next_full3_20261005/episodes/test_rl/fill_ledger.csv)、[费用压力](../../reports/ml_selection_next_full3_20261005/cost_stress.json)、[执行压力](../../reports/ml_selection_next_full3_20261005/execution_stress.json)、[实际最终裁决](../../reports/ml_selection_next_full3_20261005/adjudication.json)。

即使后续历史 test、压力和所有正式更新成功完成，本轮仍保留回顾性身份、历史成员资格证据限制，以及至少 30 个真实前向 decision 日期等未满足条件。未来样本不由本轮历史重放或多个种子提供。最终验收还须以实际总执行收据及 [运行恢复指南](../ml_selection_next_execution_20261005.md) 记录的能力边界为准。
