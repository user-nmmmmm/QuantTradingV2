# ML 选币本轮滚动窗口与执行压力解读

本文以 `reports/ml_selection_next_full6_20261005` 为当前最终汇总入口，冻结协议为 `b79dc942e38c70c40c00fa6f8bedcdeb63511cb05051899d972cd9f6896d6eb6`。下文历史矩阵及原证据保留在 source5（协议 `6f41249b58f54e3ed2719637b003d59dbd509a599231d2fbc64163e309db05df`）：主窗口的十个账户、费用和执行压力，以及 WF1 实际来源于 source4；WF2/WF3/WF4 的登记尝试实际来源于 source5。source6 继承完整历史矩阵，只修复并重放固定 RL 的桥接审计，144/144 个动作与完整 audit 一致；新增训练 0、新门槛搜索 0，财务候选、权重、门槛与经济路径不变。source3/source4 的中断、source5 的原桥接不一致、WF2/WF3 低参与率真实失败及未知最终指标均保留，完整汇总不能写成全部压力通过或全部 source6 新评估。

这里分开回答四个问题：账户是否赚钱、风险是否在限制内、账务是否能对上、证据是否足以支持有效性和准入。正收益、较低回撤、账务通过可以分别成立，而最终准入仍不成立。历史 test 已进入开发材料，本轮保持回顾性研究身份，不把重放窗口、随机种子或参数更新视作新的独立最终样本。

**source3/source4 中断与 source5 恢复的科学口径。** source3 协议为 `ee4fb26d17e74a277047cdadd8bf6afb1f05dd3906e1b2e5bc1131c9366554f1`，其 run 保留 `complete=false`、`all_minimum_budgets_met=false`。它在 WF1 的 `low_participation` 情形因 `protective_position_identity_missing:UNI/USDT` 中断：权威 lot 账本已经没有 open lot，但汇总数量还剩 `4.263256414560601e-14`。不能把这次中断改写为 source3 已完成四窗口。source4 修复协议为 `9f9b3f8f18397b7a6219483b293895e5a484b5f1dcf857f4680d3e90f5ef51b9`。

source4 唯一变更的运行源码为 `core/portfolio.py`：只在权威 lot 账本为空、汇总残余不大于已有 lot quantity epsilon 时，把投影持仓归为精确零；真实成交现金不变。修复的定向重放真实经过 2022-08-26 UNI/USDT 的残余归零，WF1 低参与率账户账务通过，84 项针对性检查通过。该修复验证产生 0 次训练更新，未写回父 run 产物。

source4 的第二次中断保留为执行失败事实：WF2 `low_participation` 在窗口尾部关闭持仓时抛出 `End-window exit cannot fill within actual liquidity`。低参与率下，真实流动性不足以完成终端退出是合法的压力边界。该情形没有已完成终端账户，最终净收益、最终 reward 和最终 accounting 均未知；不能用零收益、最后已落盘的中途 equity 或假定成交补齐。WF2 已落盘的普通 test 与费用压力不代表其全部压力已完成；source4 的完整 `walk_forward.json` 只有一个已完成 item（WF1），最终四窗口收据不存在。

source5 只变更 `research/ml_selection/pipeline.py` 的研究汇总路径：将上述精确预期的流动性退出异常记录为失败的执行证据，继续其余登记压力；其他异常仍传播，原引擎成交与终端策略不变。source5 来源登记明确继承 source4 的 `main_validation_test_cost_execution` 和 `walk_forward_window_01` 评估范围；WF2/WF3/WF4 与 RL state bridge 是 source5 的继续评估范围。六个训练 cell 全部继承，source3 更新 103、source4 更新 21、source5 新训练更新 0，`training_equivalence_proven=false`。完整 matrix 指全部登记尝试及失败得到报告，不等于每个压力情形都成功。

source5 已实际把 WF2 的同一压力边界记录为 `failed_end_window_liquidity`、`execution_completed=false`。其原引擎部分证据有 95 行已记录成交：期末 AVAX/USDT 卖单请求数量 `73.62268620310697`，只完成 `22.081817700000002`，剩余 `51.540868503106964`，与真实 open lot 数量一致，`terminal_contract_satisfied=false`。这是真实未平持仓，不是 source3 的极小数值残余。失败时现金 `100996.77171029814` 只是部分账户状态，不能作为最终净值；最终净收益、回撤、reward、accounting 及是否风险终止均保存为 `null`。source5 在记录失败后实际继续完成 WF2 的开仓延迟与现金预算限制情形，不能将“继续汇总成功”写成“失败压力已成功退出”。

source4 导入主窗口 42/43/44 和 WF4 43/44 已在 source3 完成的 103 次参数更新与固定权重；这些更新的训练引擎、最佳 checkpoint 和门槛选择、继承训练/验证指标来源仍是 source3。source4 重新执行账户评估，WF4 seed42 是 source4 的新训练 cell。注册勘误明确撤回“成功完成即能证明没有经过相关路径”的绝对说法：最后一根 bar 的保护成交可能发生在当根身份核对之后，成功完成本身不能证明旧训练没有受该路径影响。因此 `training_equivalence_status=not_proven`，不能把 103 次导入更新称为 source4 重训或宣称重训位级等价。

source4 的五个继承摘要中的相对 `artifact_directory` 按映射解析到封存的 source3 原目录：cell 摘要使用 `OLD/cell_relative/summary.artifact_directory`；根学习曲线使用 `OLD/summary.artifact_directory`；根 winner history 的 artifact root 为 `OLD/rl_seeds/43`。解析报告保存 `all_recorded_summary_paths_resolved_and_hash_verified=true`，保留原始字节和 hash。source5 则逐 cell 使用 `recovery_provenance.inherited_cells.source_artifact_root / summary.artifact_directory`，其中五个 root 指向 source3，WF4 seed42 指向 source4；学习曲线、pilot 和原诊断的来源仍为 source3。不能误链接到后续 run 的空训练回合目录。

证据：[source3 中断收据](../../reports/ml_selection_next_full3_20261005/interruption_receipt.json)、[保护持仓归零修复验证](../../reports/ml_selection_next_full3_verification_20261005/protective_flat_fix_verification.json)、[source4 冻结协议](../../reports/ml_selection_next_full4_20261005/protocol.json)、[source4 中断及完整训练收据](../../reports/ml_selection_next_full4_20261005/interruption_receipt.json)、[source4 恢复来源](../../reports/ml_selection_next_full4_20261005/recovery_provenance.json)、[注册勘误](../../reports/ml_selection_next_full4_20261005/recovery_registration_erratum.json)、[继承回合解析与 hash 核对](../../reports/ml_selection_next_full4_20261005/inheritance_episode_resolution.json)、[source5 冻结协议](../../reports/ml_selection_next_full5_20261005/protocol.json)、[source5 混合来源登记](../../reports/ml_selection_next_full5_20261005/recovery_provenance.json)、[WF2 低参与率失败摘要](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/summary.json)、[WF2 原引擎未完成账户证据](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)。

**固定策略迁移验证。** 五个导入 best policy 各使用 source3 已选门槛，在 source4 下重放一次开发验证账户。以下 5/5 的每日 equity 形状、索引、列及数值精确相同，最大绝对差为 0，确定性 episode digest 相同，scalar differences 均为空。该补充验证产生 0 次新训练更新、搜索 0 个新门槛，`changed_original_selection=false`；它没有重新寻找 winner，也未使用 test 选择。

| 导入 cell | 原最佳回合 | 原冻结门槛 | 每日 equity 行数（旧 / 新） | 固定验证精确相同 | 训练等价证明 |
| --- | ---: | ---: | ---: | --- | --- |
| main42 | 13 | 0.50 | 547 / 547 | 是 | 未证明 |
| main43 | 20 | 0.51 | 547 / 547 | 是 | 未证明 |
| main44 | 15 | 0.49 | 547 / 547 | 是 | 未证明 |
| WF4 seed43 | 1 | 0.51 | 181 / 181 | 是 | 未证明 |
| WF4 seed44 | 1 | 0.51 | 181 / 181 | 是 | 未证明 |

固定验证的精确相同支持这五份已选权重与门槛的 source4 迁移；它没有检查所有旧训练轨迹在 source4 下的参数更新，也不改变 `training_equivalence_proven=false`。

证据：[五份固定策略验证报告](../../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)。

**账户与窗口口径。** 每个 arm、每次训练/验证回合及每个滚动窗口都重新建立原引擎账户；窗口协议保存 `account_initialization=separate_fresh_account_no_equity_stitching`。持仓、现金、订单、退出、健康及风险控制路径各自演化。主窗口和不同窗口的净收益不能直接相加，四个窗口也不是一条接续投资曲线。

主窗口 test 为 2024-07-01 至 2026-09-18。滚动窗口登记边界为：

| 窗口 | 训练截止边界（不含） | 验证区间 | test 区间 | 登记 RL |
| --- | --- | --- | --- | --- |
| WF1 | 2022-01-01 | 2022-01-01 至 2022-06-30 | 2022-07-01 至 2022-12-31 | 否 |
| WF2 | 2023-01-01 | 2023-01-01 至 2023-06-30 | 2023-07-01 至 2023-12-31 | 否 |
| WF3 | 2024-01-01 | 2024-01-01 至 2024-06-30 | 2024-07-01 至 2024-12-31 | 否 |
| WF4 | 2025-01-01 | 2025-01-01 至 2025-06-30 | 2025-07-01 至 2025-12-31 | 三种子，分别至少 20 次实际更新 |

前三个窗口各登记九个完整 arm：native、qualified_native、momentum、LightGBM、Ridge、LambdaRank 和 random 42/43/44。WF4 加入 RL，登记十个 arm。每个窗口在其开发验证账户上选择并冻结自己的候选，再运行其 test 和压力；窗口候选不替换主窗口已冻结模型。即使验证资格未过、test 为零或负、账户全现金，窗口仍须完整报告，不能从稳健性统计中删掉。

证据：[最终冻结协议](../../reports/ml_selection_next_full5_20261005/protocol.json)、[完整四窗口汇总](../../reports/ml_selection_next_full5_20261005/walk_forward.json)、[source4 已完成 WF1 汇总](../../reports/ml_selection_next_full4_20261005/walk_forward.json)。WF1 来源于 source4，WF2/WF3/WF4 来源于 source5。

**主窗口冻结候选。** 候选仍为 main43 的 RL，模型 ID `9db8c77c77e58997798c005b016f34a07bfb85a3fb7d77eacdce8f25bb8b851f`、父模型 ID `1e7f2d876452069b06dc2dc0702dfcc430c35c643d1b44688ccb98c7773cdb90`，冻结门槛 0.51，开发验证资格通过。主窗口历史 test 的 source4 账户净收益 2.8052%、最大回撤 2.5961%、平均总敞口 0.7406%，两个开仓订单产生四行实际成交，净权益奖励为 0.0276653180，账务通过且未因风险终止。原 winner、权重和门槛保持冻结，没有按 source4 test 改选。

十个完整 arm 的 source4 历史 test 账户结果如下，净收益和回撤百分比均来自实际账户；全部账务通过且没有风险终止。Ridge/LambdaRank test 净收益高于冻结 RL，这个结果必须保留，不能事后改选。

| arm | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 净权益奖励 |
| --- | ---: | ---: | ---: | ---: | ---: |
| native | -4.1013% | 7.6314% | 1.9696% | 108 | -0.0418779789 |
| qualified_native | -3.4045% | 7.0368% | 1.9593% | 102 | -0.0346384167 |
| momentum | -2.9897% | 6.6376% | 1.9709% | 102 | -0.0303526324 |
| LightGBM | -2.9897% | 6.6376% | 1.9709% | 102 | -0.0303526324 |
| Ridge | 3.3273% | 6.4339% | 1.9703% | 88 | 0.0327318302 |
| LambdaRank | 3.2118% | 6.5386% | 1.9525% | 88 | 0.0316131996 |
| random42 | -3.2422% | 6.8806% | 1.9647% | 102 | -0.0329597226 |
| random43 | -3.1151% | 6.7583% | 1.9668% | 102 | -0.0316467366 |
| random44 | -3.2887% | 6.9253% | 1.9686% | 104 | -0.0334395057 |
| RL（冻结候选） | 2.8052% | 2.5961% | 0.7406% | 4 | 0.0276653180 |

极低敞口不能只由净收益符号解释。RL 两个实际开仓为 BTC/USDT 与 XRP/USDT，均在 2024 年 11 月开仓、12 月退出，其余时期多数保持现金。候选概率与门槛的敏感性、原引擎市场状态和资金约束、实际成交联接见上述诊断。不能将两个开仓上的较低回撤推广为广泛候选的稳定风控或盈利能力。

**主窗口费用与执行压力。** 所有情形都使用冻结候选和门槛、重置账户，并重新运行原引擎；费用已进入净权益，表格是实际账户结果，没有用简单比例扣费推算。

| 情形 | 登记参数 | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 账务 / 风险终止 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 基准 | 原费用及执行设置 | 2.8052% | 2.5961% | 0.7406% | 4 | 通过 / 无 |
| 费用 1.5 倍 | `cost_multiplier=1.5` | 2.7495% | 2.6230% | 0.7407% | 4 | 通过 / 无 |
| 费用 2.0 倍 | `cost_multiplier=2.0` | 2.6938% | 2.6498% | 0.7409% | 4 | 通过 / 无 |
| 开仓延迟 | 额外延迟 1 个日线 bar | 2.4461% | 2.3722% | 0.6440% | 5 | 通过 / 无 |
| 低参与率 | 最大参与率 0.00001，开仓单 TTL 3 bars | 2.8058% | 2.5955% | 0.7406% | 5 | 通过 / 无 |
| 现金预算限制 | 初始资金 10,000 | 2.8052% | 2.5961% | 0.7406% | 4 | 通过 / 无 |

费用压力仍为正，说明这条历史路径在登记情形下没有被费用抹去。低参与率和开仓延迟出现五行成交，提醒成交数量受真实分批撮合影响，不能把行数变化当成独立交易数增加。资金限制下收益比例接近基准，仍只是该账户、参数及规模的实测，不是无限容量结论。上述情形全部只有少量开仓，无法弥补样本集中。

证据：[主窗口候选](../../reports/ml_selection_next_full5_20261005/candidate.json)、[继承 source4 的主窗口 test](../../reports/ml_selection_next_full5_20261005/test_comparison.json)、[继承 source4 的费用压力](../../reports/ml_selection_next_full5_20261005/cost_stress.json)、[继承 source4 的执行压力](../../reports/ml_selection_next_full5_20261005/execution_stress.json)、[继承 source4 的主窗口裁决](../../reports/ml_selection_next_full5_20261005/adjudication.json)、[候选流与奖励诊断](ml_selection_next_diagnostics_20261005.md)。

**主窗口随机种子差异。** 三个种子的最佳 checkpoint 和门槛均由 source3 开发验证选择；以下指标保留 source3 来源，source4 的五份固定策略报告已核实其选定验证账户精确一致。表格不能解释成 source4 三次重训，也不能解释成三个独立历史 test。

| 种子 | 最佳 checkpoint 回合 / 门槛 | 完成的实际更新 | 验证净收益 | 验证奖励 | 验证最大回撤 | 平均总敞口 | selector 接受 / 成交行数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 42 | 13 / 0.50 | 20 | 0.0403% | 0.0004028174 | 2.6105% | 1.4846% | 2 / 4 |
| 43 | 20 / 0.51 | 23 | 5.5041% | 0.0535797875 | 5.0123% | 2.0560% | 4 / 32 |
| 44 | 15 / 0.49 | 20 | 2.9679% | 0.0292470112 | 6.5577% | 5.3505% | 125 / 149 |

三个最佳验证账户都为正、账务通过且未因风险终止，但收益幅度、门槛、接受数和敞口差异很大。main42 接近零收益，main44 接受更多候选而回撤也更大。重复随机训练改变策略和账户路径，没有提供三个新市场；不能仅用“3/3 验证为正”证明种子稳健性。多个 checkpoint、种子和门槛之间的最佳值比较也没有消除验证选择的乐观偏差。

证据：[原主窗口三种子比较](../../reports/ml_selection_next_full3_20261005/rl_seed_comparison.json)、[source4 保留的三种子比较](../../reports/ml_selection_next_full4_20261005/rl_seed_comparison.json)、[固定迁移验证](../../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)。

**WF4 三种子的实际开发验证差异。** 这三个 cell 的训练均已完成，以下 best checkpoint 只使用各自开发验证选择；seed42 来源为 source4，seed43/44 来源为 source3。source4 的五份固定策略迁移验证覆盖两个导入 WF4 best policy，不能把它们记作 source4 重训。

| WF4 种子 / 训练来源 | 最佳 checkpoint 回合 / 门槛 | 实际更新 | 验证净收益 | 验证奖励 | 验证最大回撤 | 平均总敞口 | 实际 selected_count / 成交行数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 42 / source4 | 18 / 0.49 | 21 | 0.5042% | 0.0050288539 | 4.4708% | 3.6658% | 3 / 6 |
| 43 / source3 | 1 / 0.51 | 20 | 0.0000% | 0.0000000000 | 0.0000% | 0.0000% | 0 / 0 |
| 44 / source3 | 1 / 0.51 | 20 | 0.0000% | 0.0000000000 | 0.0000% | 0.0000% | 0 / 0 |

三份 best 验证账户账务通过、没有风险终止，但只有 seed42 为正收益；seed43/44 是合法现金账户，其零收益和零风险不能计为正盈利效力。全部实际更新均属于各协议的重复历史训练，种子差异没有形成三个独立市场样本。这张开发验证表也不代替 WF4 的最终 test、费用和执行压力。

证据：[WF4 seed42 source4 原训练记录](../../reports/ml_selection_next_full4_20261005/walk_forward/window_04/rl_seeds/42/rl_training.json)、[WF4 seed42 source4 完成收据](../../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_completion.json)、[WF4 seed43 source3 原训练记录](../../reports/ml_selection_next_full3_20261005/walk_forward/window_04/rl_seeds/43/rl_training.json)、[WF4 seed44 source3 原训练记录](../../reports/ml_selection_next_full3_20261005/walk_forward/window_04/rl_seeds/44/rl_training.json)。

**四个滚动窗口的候选资格与评估来源。** source5 的真实最终汇总包含四个窗口，前三个各九个 arm、WF4 十个 arm，共 37 个完整 test 账户。WF1 的账户与裁决来自 source4，WF2/WF3/WF4 来自 source5；主窗口来源仍为 source4。每个候选只按本窗口开发验证冻结，全部保存 test_used_for_selection=false，不替换主窗口冻结 RL。WF1/WF3 的验证资格未过，也完整保留。

| 窗口 / 评估来源 | 冻结候选 | 验证净收益 | 验证奖励 | 验证最大回撤 | 验证账务 | qualification |
| --- | --- | ---: | ---: | ---: | --- | --- |
| WF1 / source4 | Ridge | -4.1360% | -0.0422396005 | 4.9386% | 通过 | 否 |
| WF2 / source5 | LightGBM | 1.1112% | 0.0110509253 | 4.5136% | 通过 | 是 |
| WF3 / source5 | LightGBM | -3.7792% | -0.0385246679 | 4.3114% | 通过 | 否 |
| WF4 / source5 | LightGBM | 0.6667% | 0.0066447107 | 5.1902% | 通过 | 是 |

WF4 的候选是 LightGBM：其验证奖励 0.0066447107 高于已冻结 RL 最佳策略的 0.0050288539，按开发验证冻结后完成 test。WF4 Ridge/LambdaRank 的 test 净收益约 32.39%，高于候选 LightGBM 的约 6.90%，没有按 test 改选。WF4 三种子训练与开发验证来源见前表，source5 没有重训这些 RL cell。

**完整九/十个 test arm。** 以下 37 个 test 账户全部 accounting_ok=true、terminated_by_risk=false；净权益奖励、净收益、回撤、敞口和成交行数来自完整原引擎账户。事件组是实际持仓区间的连通重叠分组，不是成交行数。全部 arm 的事件集中度为 insufficient，independent_events_proven=false。

WF1（source4，九个完整 arm）。

| arm | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 净权益奖励 | 连通持仓事件数 / 集中度 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| native | -4.2680% | 7.5186% | 3.7350% | 20 | -0.0436170583 | 3 / insufficient |
| qualified_native | -4.2680% | 7.5186% | 3.7350% | 20 | -0.0436170583 | 3 / insufficient |
| momentum | -4.2638% | 7.4779% | 3.7264% | 20 | -0.0435733252 | 3 / insufficient |
| LightGBM | -4.2638% | 7.4780% | 3.7265% | 20 | -0.0435735248 | 3 / insufficient |
| Ridge（冻结候选） | -3.1590% | 5.9182% | 3.7463% | 16 | -0.0321001421 | 3 / insufficient |
| LambdaRank | -3.1590% | 5.9182% | 3.7463% | 16 | -0.0321001421 | 3 / insufficient |
| random42 | -4.2665% | 7.5045% | 3.7321% | 20 | -0.0436022355 | 3 / insufficient |
| random43 | -4.2562% | 7.4089% | 3.7119% | 20 | -0.0434938289 | 3 / insufficient |
| random44 | -4.2562% | 7.4089% | 3.7119% | 20 | -0.0434938289 | 3 / insufficient |

WF2（source5，九个完整 arm）。

| arm | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 净权益奖励 | 连通持仓事件数 / 集中度 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| native | -1.5054% | 6.5966% | 7.8679% | 40 | -0.0151688072 | 3 / insufficient |
| qualified_native | -1.4380% | 6.5966% | 7.8835% | 40 | -0.0144839168 | 3 / insufficient |
| momentum | -1.4380% | 6.5966% | 7.8835% | 40 | -0.0144839168 | 3 / insufficient |
| LightGBM（冻结候选） | -1.8908% | 6.6851% | 7.6508% | 40 | -0.0190894653 | 3 / insufficient |
| Ridge | -1.6300% | 2.8262% | 4.4988% | 46 | -0.0164338611 | 4 / insufficient |
| LambdaRank | -1.6300% | 2.8262% | 4.4988% | 46 | -0.0164338611 | 4 / insufficient |
| random42 | -1.4380% | 6.5966% | 7.8835% | 40 | -0.0144839168 | 3 / insufficient |
| random43 | -1.4380% | 6.5966% | 7.8835% | 40 | -0.0144839168 | 3 / insufficient |
| random44 | -1.4380% | 6.5966% | 7.8835% | 40 | -0.0144839168 | 3 / insufficient |

WF3（source5，九个完整 arm）。

| arm | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 净权益奖励 | 连通持仓事件数 / 集中度 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| native | -3.2389% | 5.7747% | 3.7405% | 30 | -0.0329253126 | 4 / insufficient |
| qualified_native | -3.2389% | 5.7747% | 3.7405% | 30 | -0.0329253126 | 4 / insufficient |
| momentum | -2.8233% | 5.5844% | 3.7915% | 30 | -0.0286395278 | 4 / insufficient |
| LightGBM（冻结候选） | -2.8233% | 5.5844% | 3.7915% | 30 | -0.0286395278 | 4 / insufficient |
| Ridge | -2.9818% | 5.5844% | 3.5960% | 30 | -0.0302715298 | 4 / insufficient |
| LambdaRank | -2.9245% | 5.5739% | 3.5963% | 28 | -0.0296816390 | 4 / insufficient |
| random42 | -3.0763% | 5.6650% | 3.7644% | 30 | -0.0312466183 | 4 / insufficient |
| random43 | -2.9490% | 5.6141% | 3.7735% | 30 | -0.0299336321 | 4 / insufficient |
| random44 | -3.1228% | 5.6617% | 3.7812% | 32 | -0.0317264014 | 4 / insufficient |

WF4（source5，十个完整 arm）。

| arm | 净收益 | 最大回撤 | 平均总敞口 | 实际成交行数 | 净权益奖励 | 连通持仓事件数 / 集中度 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| native | 6.7612% | 4.1749% | 11.6457% | 22 | 0.0654247198 | 2 / insufficient |
| qualified_native | 6.7612% | 4.1749% | 11.6457% | 22 | 0.0654247198 | 2 / insufficient |
| momentum | 6.7654% | 4.1715% | 11.6451% | 22 | 0.0654639035 | 2 / insufficient |
| LightGBM（冻结候选） | 6.8967% | 4.4982% | 11.6369% | 18 | 0.0666927604 | 2 / insufficient |
| Ridge | 32.3868% | 5.2356% | 13.1703% | 52 | 0.2805576528 | 2 / insufficient |
| LambdaRank | 32.3868% | 5.2356% | 13.1703% | 52 | 0.2805576528 | 2 / insufficient |
| random42 | 6.7436% | 4.1890% | 11.6480% | 20 | 0.0652595578 | 2 / insufficient |
| random43 | 6.7741% | 4.1646% | 11.6440% | 22 | 0.0655448344 | 2 / insufficient |
| random44 | 6.7436% | 4.1890% | 11.6480% | 20 | 0.0652595578 | 2 / insufficient |
| RL | 2.4197% | 5.2273% | 13.0571% | 17 | 0.0239084256 | 3 / insufficient |

**各窗口费用与执行压力。** 每项都使用本窗口冻结候选、重置原引擎账户；费用已进入净权益，不用比例扣费估算。基准见上方各窗口冻结候选行。登记费用倍数为 1.5/2.0；开仓延迟为 1 个日线 bar；低参与率为 0.00001、开仓订单 TTL 为 3 bars；现金预算限制初始资金为 10,000。未完成情形的成交记录列只显示已记录部分成交，不能与完整账户行数作成功性比较。

| 窗口 | 情形 | 执行状态 | 最终净收益 | 最终最大回撤 | 成交记录 | 最终净权益奖励 | 最终账务 | 风险终止 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| WF1 | 费用 1.5 倍 | 完成 | -3.3390% | 6.0347% | 16 | -0.0339598781 | 通过 | 否 |
| WF1 | 费用 2.0 倍 | 完成 | -3.5334% | 6.1655% | 16 | -0.0359734675 | 通过 | 否 |
| WF1 | 开仓延迟 1 bar | 完成 | -2.2974% | 4.5630% | 16 | -0.0232415488 | 通过 | 否 |
| WF1 | 低参与率 | 完成 | -1.0047% | 1.4884% | 58 | -0.0100977265 | 通过 | 否 |
| WF1 | 现金预算限制 | 完成 | -3.1590% | 5.9181% | 16 | -0.0320994671 | 通过 | 否 |
| WF2 | 费用 1.5 倍 | 完成 | -2.1154% | 6.7880% | 40 | -0.0213811487 | 通过 | 否 |
| WF2 | 费用 2.0 倍 | 完成 | -2.3396% | 6.8908% | 40 | -0.0236743443 | 通过 | 否 |
| WF2 | 开仓延迟 1 bar | 完成 | 5.6342% | 7.1646% | 127 | 0.0548116171 | 通过 | 否 |
| WF2 | 低参与率 | 未完成：流动性退出失败 | 未知 | 未知 | 95（部分） | 未知 | 未知 | 未知 |
| WF2 | 现金预算限制 | 完成 | -1.8907% | 6.6850% | 40 | -0.0190881384 | 通过 | 否 |
| WF3 | 费用 1.5 倍 | 完成 | -3.0052% | 5.6466% | 30 | -0.0305130318 | 通过 | 否 |
| WF3 | 费用 2.0 倍 | 完成 | -3.1869% | 5.7088% | 30 | -0.0323874781 | 通过 | 否 |
| WF3 | 开仓延迟 1 bar | 完成 | -2.4729% | 4.6499% | 32 | -0.0250398683 | 通过 | 否 |
| WF3 | 低参与率 | 未完成：流动性退出失败 | 未知 | 未知 | 138（部分） | 未知 | 未知 | 未知 |
| WF3 | 现金预算限制 | 完成 | -2.8233% | 5.5844% | 30 | -0.0286389057 | 通过 | 否 |
| WF4 | 费用 1.5 倍 | 完成 | 6.7684% | 4.4859% | 20 | 0.0654917149 | 通过 | 否 |
| WF4 | 费用 2.0 倍 | 完成 | 6.6556% | 4.4624% | 20 | 0.0644347653 | 通过 | 否 |
| WF4 | 开仓延迟 1 bar | 完成 | 7.4337% | 4.4996% | 20 | 0.0717036698 | 通过 | 否 |
| WF4 | 低参与率 | 完成 | -0.2253% | 3.2948% | 57 | -0.0022553730 | 通过 | 否 |
| WF4 | 现金预算限制 | 完成 | 6.8968% | 4.4982% | 18 | 0.0666935421 | 通过 | 否 |

WF2 与 WF3 的低参与率情形保存 failed_end_window_liquidity，execution_completed=false，最终净收益、回撤、reward、accounting 和风险终止均为 null。WF3 已记录 138 行成交；期末 AAVE/USDT 卖单请求 8.435609813011324，实际完成 2.51212136，仍有 5.923488453011323 的真实 lot 持仓，terminal_contract_satisfied=false。失败时现金 108232.42173105621 不是最终净值。两项失败的原引擎现金、持仓、未完成订单和成交均保留，未强制假成交、未降低原终端约束；后续开仓延迟和现金限制账户仍实际完成。

WF4 基准和费用 1.5/2.0 倍收益为正，但低参与率真实账户净收益为 -0.2253%，reward 为 -0.0022553730；压力账务通过并不把负收益变成盈利。WF2 的延迟账户收益 5.6342% 也不能替代其负的基准账户或未知的低参与率最终结果。

**事件集中与独立性。** 下表只列各窗口冻结候选，金额为实际账户净 PnL 单位。连通持仓事件与固定 5 日开仓块是两个不同口径：WF2/WF3/WF4 的固定块统计可显示 ok，但实际连通事件仍只有 2–4 组、全部 insufficient。全部事件和 cohort 计算均保存独立性未证明，不能把较多块数解释为独立市场样本。

| 窗口 | 连通事件数 / 状态 | 连通事件净 PnL | 移除最高五组后 PnL | 移除最高十组后 PnL | 固定 5 日块数 / 状态 | 固定块 positive_after_top_5 | 事件 / cohort 独立性证明 |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| WF1 | 3 / insufficient | -3159.0401 | -3159.0401 | -3159.0401 | 5 / insufficient | null（不足） | false / false |
| WF2 | 3 / insufficient | -1890.8415 | -5407.8629 | -5407.8629 | 10 / ok | 否 | false / false |
| WF3 | 4 / insufficient | -2823.3304 | -4206.3250 | -4206.3250 | 6 / ok | 否 | false / false |
| WF4 | 2 / insufficient | 6896.6999 | -531.1513 | -531.1513 | 6 / ok | 否 | false / false |

**分开的窗口裁决。** 四个窗口均为 retrospective_research_only，retrospective_checks_passed=false、formal_admission=false。所有 test 账户回撤均在登记限制内，没有风险终止；WF2/WF3 失败低参与率情形的最终风险和账务未知，不能写成全压力确认通过。

| 窗口 | 收益层 | 风险层 | 会计层 | 证据层 |
| --- | --- | --- | --- | --- |
| WF1 | 基准及费用 1.5/2.0 倍均负；超过 native | 基准回撤限制内；完成压力无风险终止 | test 与全部压力通过 | 验证资格未过；3 事件组不足；独立性未证明；回顾检查未过 |
| WF2 | 基准及费用 1.5/2.0 倍均负；未超过 native | 基准回撤限制内；低参与率最终风险终止未知 | test 与完成压力通过；低参与率账务未知，all_stress=false | 3 事件组不足；独立性未证明；回顾检查未过 |
| WF3 | 基准及费用 1.5/2.0 倍均负；超过 native | 基准回撤限制内；低参与率最终风险终止未知 | test 与完成压力通过；低参与率账务未知，all_stress=false | 验证资格未过；4 事件组不足；独立性未证明；回顾检查未过 |
| WF4 | 基准及费用 1.5/2.0 倍均正；超过 native；低参与率负 | 基准回撤限制内；完成压力无风险终止 | test 与全部压力通过 | 2 事件组不足；移除正收益事件后为负；独立性未证明；回顾检查未过 |

WF2/WF3 的 all_stress=false 表示无法确认全部压力账务通过，unknown_stress_accounting 明确列出 low_participation；它不是把原失败账户最终会计判成已知不平。四窗口的 PIT、独立最终样本和事件独立性仍未满足，前向成熟度仍为 pending。重复窗口、种子、策略 arm 和多重尝试不能当成独立样本数量。

证据：[完整四窗口与全部 arm](../../reports/ml_selection_next_full5_20261005/walk_forward.json)、[WF1 候选与资格](../../reports/ml_selection_next_full5_20261005/walk_forward/window_01/candidate.json)、[WF2 候选与资格](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/candidate.json)、[WF3 候选与资格](../../reports/ml_selection_next_full5_20261005/walk_forward/window_03/candidate.json)、[WF4 候选与资格](../../reports/ml_selection_next_full5_20261005/walk_forward/window_04/candidate.json)。

费用与执行证据：[WF1 费用](../../reports/ml_selection_next_full5_20261005/walk_forward/window_01/cost_stress.json) / [执行](../../reports/ml_selection_next_full5_20261005/walk_forward/window_01/execution_stress.json) / [裁决](../../reports/ml_selection_next_full5_20261005/walk_forward/window_01/adjudication.json)；[WF2 费用](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/cost_stress.json) / [执行](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/execution_stress.json) / [裁决](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/adjudication.json)；[WF3 费用](../../reports/ml_selection_next_full5_20261005/walk_forward/window_03/cost_stress.json) / [执行](../../reports/ml_selection_next_full5_20261005/walk_forward/window_03/execution_stress.json) / [裁决](../../reports/ml_selection_next_full5_20261005/walk_forward/window_03/adjudication.json)；[WF4 费用](../../reports/ml_selection_next_full5_20261005/walk_forward/window_04/cost_stress.json) / [执行](../../reports/ml_selection_next_full5_20261005/walk_forward/window_04/execution_stress.json) / [裁决](../../reports/ml_selection_next_full5_20261005/walk_forward/window_04/adjudication.json)。

未完成账户证据：[WF2 低参与率原状态](../../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)、[WF3 低参与率原状态](../../reports/ml_selection_next_full5_20261005/walk_forward/window_03/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)。

**预算及资源核对：六 cell 训练与历史尝试汇总完成。** 正式矩阵只有主窗口与 WF4 的三个种子，共六个 cell；每个 cell 最低 20 次实际参数更新，最低总数为 120。真实 source4 完成/中断收据与 source5 最终执行收据共同确认总实际更新 124：source3 五个 cell 导入 103，source4 新训练 WF4 seed42 完成 21，source5 新训练 0；六个 cell 的 `minimum_budget_met=true`、`all_minimum_budgets_met=true`，缺失 cell 为空。source5 收据保存 `inherited_formal_updates=124`、`new_source_formal_updates=0`，这些训练步骤只汇总一次。父目录的 winner receipt、辅助执行副本及恢复调用不另加更新；九个 reward/resource pilot 的 18 次更新也不计入正式预算。完整历史 matrix 保存 `completed_historical_matrix_including_real_failed_execution_scenarios`，表示尝试和失败已完整报告，并不表示四窗口压力全成功。

| cell | source3 实际更新 | source4 实际更新 | source5 新更新 | 已完成 cell 停止原因 | 验证停滞计数 | 提前停止生效起点 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| main42 | 20 | 0 | 0 | validation_early_stop | 7 | 20 |
| main43 | 23 | 0 | 0 | validation_early_stop | 3 | 20 |
| main44 | 20 | 0 | 0 | validation_early_stop | 5 | 20 |
| WF4 seed42 | 0 | 21 | 0 | validation_early_stop | 3 | 20 |
| WF4 seed43 | 20 | 0 | 0 | validation_early_stop | 19 | 20 |
| WF4 seed44 | 20 | 0 | 0 | validation_early_stop | 19 | 20 |

六个已完成 cell 的实际 early stopping 均在最低 20 次更新之后生效：main43 继续到 23 次，WF4 seed42 继续到 21 次，其他四个在 20 次停止。更新是重复历史账户轨迹上的参数步骤，既不是独立事件数，也不是新增独立市场样本。source4 的五个导入恢复调用新增更新为 0，其调用耗时不能代表原训练资源。

真实训练的原始资源已在恢复前归档，资源表使用这些训练收据，不能采用后续 0-update resume 写出的近零耗时。每行峰值是 Windows 进程生命周期工作集，不是该 cell 增量内存，也不能相加成研究的并发峰值。

| 保存原始资源的训练 cell | 实际更新 | 正式训练调用秒数 | 含加载准备的执行全程秒数 | 进程生命周期峰值工作集（bytes） |
| --- | ---: | ---: | ---: | ---: |
| main44（source3 helper） | 20 | 1,659.7413 | 1,683.8579 | 850,399,232 |
| WF4 seed42（source4） | 21 | 1,317.4711 | 1,331.6142 | 1,127,063,552 |
| WF4 seed43（source3 helper） | 20 | 1,279.1449 | 1,291.9401 | 1,123,414,016 |
| WF4 seed44（source3 helper） | 20 | 1,436.5593 | 1,503.4049 | 1,121,742,848 |

WF4 两个 helper 顺序执行的全程合计为 2,795.3450 秒，其最大顺序 helper 峰值为 1,123,414,016 bytes；该统计既不包括 root 的 WF4 seed42，也不证明整轮并发峰值。main44 的资源以保存的辅助完成收据及其原 `rl_budget_receipt` / `rl_resources` 副本为准。

证据：[source5 最终执行收据](../../reports/ml_selection_next_full5_20261005/next_execution_receipt.json)、[source3 中断时的五 cell 预算](../../reports/ml_selection_next_full3_20261005/interruption_receipt.json)、[source4 六 cell 完整训练预算](../../reports/ml_selection_next_full4_20261005/interruption_receipt.json)、[WF4 seed42 source4 完成收据](../../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_completion.json)、[WF4 seed42 原预算](../../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_original_rl_budget_receipt.json)、[WF4 seed42 原资源](../../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_original_rl_resources.json)、[main44 原辅助完成资源](../../reports/ml_selection_next_verification_20261005/main_seed44_auxiliary_completion_receipt.json)、[main44 原预算](../../reports/ml_selection_next_verification_20261005/main_seed44_rl_budget_receipt.json)、[main44 原资源](../../reports/ml_selection_next_verification_20261005/main_seed44_rl_resources.json)、[WF4 两 helper 完成汇总](../../reports/ml_selection_next_verification_20261005/wf4_auxiliary_completion.json)、[WF4 seed43 原预训练收据](../../reports/ml_selection_next_verification_20261005/wf4_seed43_pretrain_receipt.json)、[WF4 seed44 原预训练收据](../../reports/ml_selection_next_verification_20261005/wf4_pretrain_receipt.json)、[WF4 seed43 原预算](../../reports/ml_selection_next_verification_20261005/wf4_seed43_rl_budget_receipt.json)、[WF4 seed43 原资源](../../reports/ml_selection_next_verification_20261005/wf4_seed43_rl_resources.json)、[WF4 seed44 原预算](../../reports/ml_selection_next_verification_20261005/wf4_seed44_rl_budget_receipt.json)、[WF4 seed44 原资源](../../reports/ml_selection_next_verification_20261005/wf4_seed44_rl_resources.json)。

**主窗口与最终准入裁决。** 主窗口收益层：净收益为正、超过原规则、费用 1.5/2.0 倍仍为正。风险层：回撤在登记限制内，主账户和执行压力没有风险终止。会计层：主账户与所有压力检查通过。证据层：历史 PIT、独立最终样本、独立事件组和前向成熟度仍未满足。四窗口的分开裁决见上表，不能由主窗口的正收益覆盖。

主窗口裁决的集中度使用固定 5 日开仓块，只有两个 cohort，`status=insufficient`，未证明独立性；移除最高五/十组后剩余净 PnL 都为零，`positive_after_top_5=null`。因此 `retrospective_checks_passed=false`，不能写成完整稳健性门槛通过。后续窗口的 `event_concentration` 使用实际持仓区间连通重叠分组，与固定 5 日块不同；即使事件数足够，该计算仍保存 `independent_events_proven=false`，不自动证明事件独立。

现金账户的零收益与零风险属于合法执行结果；它们可以通过会计检查，不能算作正盈利效力。`insufficient` 集中度或验证资格未通过也必须保留，不能改写为成功窗口。

source5 的原固定策略历史桥接报告继续保存 `all_decisions_identical=false`：144/144 个快照的 selected-ID 比较一致，136/144 个完整 decision-facts / audit 比较一致，`verification_errors=[]`。source6 只修复 `research/ml_selection/forward_bridge.py` 的风险审计装饰：已被策略拒绝的行保留原审计事实，只对原选中后被风险预算撤销的行添加风险拒绝字段。真实 source6 原引擎重放已取得 144/144 个动作与完整 audit 一致、`all_decisions_identical=true`、0 验证错误，不覆盖 source5 原报告。

source6 桥接与 source5 的 equity、trades、rewards、fill_ledger、closed_positions 五份经济文件 SHA 精确相同；净收益、最大回撤、敞口、换手、reward、fill_count、accounting 七个标量相同，`all_exact=true`。新训练更新和新门槛搜索均为 0，`new_financial_candidate=false`，训练等价仍未证明。这证明此次固定策略桥接审计修复的经济路径相同，没有重评主窗口和四个历史窗口，也不提供独立市场样本；该历史重放仍保存 `forward_evidence=false`。

真实前向证据当前有 1 条 recorded observation，但 0 个核验通过的前向 decision 日期、0 个成熟 proxy 标签，模拟账户收益仍为 `null`。登记开始为 2026-10-06，协议与候选在开始前冻结；实际状态仍为 `pending_forward_evidence`，缺少冻结 RL 的真实账户和策略状态，决策桥接不能提供原引擎 fills/equity。独立最终样本登记为 `preregistered_future_sample_not_opened`，没有打开。

最终执行收据保存 `formal_admission=false`、`production_enabled=false`、`historical_pit_universe_verified=false`。至少 30 个真实前向 decision 日期、成熟标签与未打开的独立最终样本仍需将来真实证据，历史回放、多个种子或最低更新数不能替代这些条件。

证据：[source5 原桥接不一致报告](../../reports/ml_selection_next_full5_20261005/rl_bridge_verification.json)、[source6 实际完整桥接](../../reports/ml_selection_next_full6_20261005/rl_bridge_verification.json)、[source6 桥接经济路径精确对照](../../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)、[source6 混合来源登记](../../reports/ml_selection_next_full6_20261005/recovery_provenance.json)、[当前前向证据](../../reports/ml_selection_next_full6_20261005/forward_evidence.json)、[当前最终执行收据与准入身份](../../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)。
