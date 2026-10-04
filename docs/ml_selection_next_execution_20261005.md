# ML 选币下一轮工程运行与恢复指南（2026-10-05）

本指南对应 [下一轮路线图](ml_selection_next_roadmap_20261005.md)、`config/ml_selection_next.yaml` 和 `scripts/run_ml_selection_next.py`。它说明已经存在的运行入口、结果证据和恢复边界；实验实际完成情况以 `next_execution_receipt.json` 的实际值为准。配置中的 120 次最低更新与 180 回合上限属于预登记预算，不能直接写成已完成结果。

全部实验使用离线日线现货研究账户。原引擎负责策略、仓位、资金分配、风控、策略健康、订单、撮合和终止计价。合并代码、取得历史研究结果、取得真实前向证据、正式准入是不同状态；本轮不自动启用生产交易。

## 1. 目录、运行环境与冻结条件

从 `D:\QuantTradingV1` 使用已有 `.venv\Scripts\python.exe`。系统默认 Python 可能没有项目测试依赖。Ridge、CPU LightGBM、LambdaRank 和 NumPy REINFORCE 都在 CPU 上运行；当前流程不要求 GPU，也不需要为本轮安装新的 GPU 训练栈。

| 用途 | 目录或文件 |
|---|---|
| 保留的 V1 研究结果 | `reports/ml_selection_full_20261004_v3` |
| 下一轮配置 | `config/ml_selection_next.yaml` |
| 本轮成功的 8 币 pilot | `reports/ml_selection_next_pilot2_20261005` |
| 最终证据与修正后桥接汇总目录 | `reports/ml_selection_next_full6_20261005` |
| 封存的完整历史矩阵 | `reports/ml_selection_next_full5_20261005` |
| 已取得的一次公共输入采集 | `reports/ml_selection_public_forward_full_20261005/public_binance_20261004T181036479390` |
| 既有注册历史输入 | `reports/multicoin_100k_20261004/registration.json` |
| 既有注册账户基线 | `reports/smart_capital_100k_20261004/registration.json` |

启动新实验时，`prepare()` 验证注册输入字节、行情帧身份及账户配置，冻结代码、配置、成员证据和注册数据，并生成成熟标签数据集。输出目录必须尚未存在。`run_ml_selection_next.py` 当前是新建运行入口；重复传入已有目录会在准备阶段失败，不能将它当作整轮恢复命令。

V1 结果保留原身份。新轮通过 `audit_existing_run()` 把只读复核写入自己的 `v1_diagnostic`，不会把 V1 的已观察测试期重新登记为独立最终样本。旧归档没有新增候选链路时，诊断只能使用原来已记录的字段，缺失证据保留限制说明。

改变源码、标签、奖励、币池、模型参数、成员证据或更新预算都需要新实验目录。恢复时 `validate_run()` 会拒绝冻结源文件清单、文件哈希、注册输入或归档产物发生变化的运行；不能修改旧协议中的哈希来绕过验证。本指南作为 Markdown 文档不属于训练源码身份清单。新运行在 `frozen_source/` 保存协议中每个源文件的完整原始字节；归档与当前源码分别核对哈希，历史源码不随工作区后续修改而消失。

本次最终证据汇总目录为 `reports/ml_selection_next_full6_20261005`，协议 ID 为 `b79dc942e38c70c40c00fa6f8bedcdeb63511cb05051899d972cd9f6896d6eb6`。封存 source5 的完整历史矩阵协议为 `6f41249b58f54e3ed2719637b003d59dbd509a599231d2fbc64163e309db05df`。第三目录因 WF1 部分平仓后约 4.26e-14 的浮点持仓残余与已关闭 lot 不一致中断；第四目录修复该边界后完成主窗口和 WF1，又因 WF2 的低参与率情形无法在真实流动性内完成期末强制平仓而中断。两次原错误、对应源码、原成交和输入均封存，旧协议和原哈希不改写。

核心修复只在 lot 明细全部关闭、账户残余低于既定 lot 容差时归零；现金仍按真实成交数量计入。source5 修复研究汇总流程：精确识别原引擎的期末流动性错误，保存真实成交、现金、未平仓 lot 和订单，最终净收益、奖励、会计与终止风险裁决保持未知，然后继续其余预登记压力情形。它不强制制造成交、不改原终止规则；其他错误和普通测试账户错误仍向外报告。失败情形的完成门槛为 false，完整矩阵表示每个登记项均尝试并报告，不表示每项通过。

正式矩阵实际完成 **124 次更新**：source3 主窗口种子 42/43/44 为 20/23/20，source3 WF4 种子 43/44 各 20，source4 WF4 种子 42 为 21。source5 和 source6 均不新增训练更新。9 单元资源 pilot 的 18 次更新对正式预算贡献仍为 0。更新、水位和 checkpoint 均从真实训练历史核对，恢复调用、winner 汇总和辅助副本不重复计数。

新协议预先登记继承完整预算、固定权重和已成功的评价：主窗口十个 test arm、两组成本、三组执行压力以及 WF1 的评价来自 source4；WF2、WF3、WF4 的最终评价及 RL 状态桥接由 source5 执行。学习曲线、退出探针、同批分配和 pilot 来自封存 source3。各范围以 `recovery_provenance.json` 的来源记录为准，不能把复制的旧计算写成 source5 新训练或全部新评价。

source5 完成四个窗口、37 个普通 test 账户及全部 20 项窗口费用/执行压力尝试；WF2/WF3 的低参与率退出失败保持 `null` 最终指标。其桥接选股与评分在 144/144 时点一致，但完整审计只有 136/144 一致：风险预算阻断时，桥接错误改写了 30 条原本已被政策拒绝的记录原因，并加入多余的 false 字段。原 source5 结果保持不变。

source6 仅修正 `forward_bridge.py`：已被政策拒绝的审计记录原样保留；只有原来入选的候选被风控撤销时才添加相应记录。新协议继承全部历史矩阵，重新回放固定政策与当前时钟诊断，完整审计和动作 **144/144 一致、验证错误 0**。与 source5 相比，逐日权益、真实成交、逐日奖励、成交账本和闭合持仓五个文件字节完全相同，账户关键指标精确一致。新训练更新、新门槛搜索及新增财务候选均为 0。证据见 [最终回执](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)、[144 个状态桥接](../reports/ml_selection_next_full6_20261005/rl_bridge_verification.json)、[经济路径一致性](../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)。历史评价来源仍是主窗口/WF1 source4、WF2–WF4 source5；source6 是最终归档与修正后桥接，不是全量重训或历史重新评价。

source6 的 v3 来源登记保留一个来自旧 v2 的粗粒度 `inherited_evaluation_source_protocol_id` 字段，它只适用于主窗口与 WF1。全部窗口以更具体的 `evaluation_origins` 和最终回执字段为准；[来源范围澄清](../reports/ml_selection_next_full6_verification_20261005/recovery_scope_clarification.json)核对这两个口径，原冻结协议不改写。

逐回合训练记录按 `recovery_provenance.inherited_cells[*].source_artifact_root / summary.artifact_directory` 解析；五个旧 cell 的根在 source3，最后一个 WF4/42 的根在 source4。根目录学习曲线和诊断按登记的 source3 根解析。父目录须共同归档，不能只复制最终小回执而丢弃原始逐日权益、订单、成交和 checkpoint。

恢复登记的兼容性绝对论证在 `recovery_registration_erratum.json` 单独勘误：成功回合也可能在最后保护成交 bar 或终止估值时留下微小残余，因此不声称已证明原 103 次训练与修后重新训练位级等价。5 个继承最佳策略按原固定权重及已选门槛在 source4 重放，逐日权益、成交摘要和奖励均完全相同；这些固定验证保存在 `repair_compatibility/`，新增更新与阈值搜索均为 0，原 winner 未改变。勘误、原声明及迁移证据一起归档。

## 2. 先运行 pilot，再执行正式预算

下面是从不存在的目标目录启动新实验的示例。目录已经有结果时，保留原目录，选择新的实验名称；不要清空旧结果后重复使用原身份。

```powershell
.\.venv\Scripts\python.exe scripts/run_ml_selection_next.py `
  --config config/ml_selection_next.yaml `
  --run-dir reports/ml_selection_next_pilot_new_20261005 `
  --previous-run reports/ml_selection_full_20261004_v3 `
  --pilot-only --max-symbols 8
```

`--pilot-only` 会完成旧结果诊断、退出标签 probe、学习曲线、监督模型验证、同批资金分配诊断和奖励资源 pilot。它在正式 RL、最终候选冻结、历史测试及滚动评价之前返回。pilot 不是只跑两个极短回合：退出 probe、九组学习曲线拟合和原引擎验证也需要资源。

确认 pilot 的墙钟时间、候选量、RAM、奖励账本和失败情况后，正式新建运行使用独立目录：

```powershell
.\.venv\Scripts\python.exe scripts/run_ml_selection_next.py `
  --config config/ml_selection_next.yaml `
  --run-dir reports/ml_selection_next_full_new_20261005 `
  --previous-run reports/ml_selection_full_20261004_v3 `
  --forward-data-dir reports/ml_selection_public_forward_full_20261005/public_binance_20261004T181036479390
```

这一正式入口也会生成自己的奖励 pilot，随后执行主窗口正式 RL、冻结候选、历史测试、成本和执行压力、全部四个滚动窗口、RL 状态桥接验证和当前时钟影子记录。公共输入是取得时的不可变采集；未来运行仍需新采集，旧目录不会自动变成当天的新鲜行情。

`--max-symbols` 只适合在第一次冻结之前登记小范围试运行。缩小币种后的实验身份和覆盖不同，不能把其结果当作完整注册币池结果。已有运行不接受修改回合数或币种上限的恢复覆盖。

## 3. 标签、排序与收益门槛分别回答什么

当前基准训练目标仍为固定 20 日窗口、冻结 ATR 止损及固定代理成本下的独立代理交易收益。`label_contract.json` 同时登记原策略退出 probe：从训练期记录中选取最多 12 个原策略候选，在固定空仓现金研究账户加入一个候选，名义金额上限为 1,000，风险预算比例为 2%，检查最多 60 根日线，仍接受原策略、原风控及真实撮合约束。

probe 的退出收益来自实际原引擎成交。没有复现原信号、未成交、未完成退出、风险终止或会计未核对时，目标保持缺失；没有买入不能写成零收益。输出 `exit_label_probe_rows.csv` 和 `exit_label_probes.json`。probe 是目标定义诊断，当前整轮不会用这 12 个 probe 替换基准训练标签；单候选独立干预也不能证明组合中加入或替换该候选的边际价值。

监督对照包括 Ridge、LightGBM 回归和真正使用 `lambdarank` 目标的排序模型。排序学习按同一 `as_of` 决策时点分组，把组内收益次序映射成预登记的五档相关性标签；缺失或未成熟成员不会被删除后单独美化剩余组。

LambdaRank 的预测是组内排序分数，没有净收益单位。其独立 Ridge 收益门槛只用训练期原始特征和训练收益拟合，`predict_net_return()` 提供收益尺度，`min_expected_return=0` 用于是否值得进入候选门槛；排序分数用于合格候选之间的资金分配次序。RL 输出则是动作入选概率，不是盈利概率。

`supervised_validation.json` 同时提供同日排名相关、收益分组及收益校准；`validation_comparison.json` 给出完整原引擎组合结果。排名指标改善、收益校准改善和真实组合表现改善必须分别阅读。

## 4. 12／24／36 个月学习曲线与 PIT 边界

`next_research.learning_months=[12,24,36]` 对三个监督模型分别拟合九个模型。每个长度只使用对应训练期内已经成熟的完整决策组；缩放器在各自训练子集上拟合。验证区间、目标、特征、种子和模型参数预算固定，并通过原引擎在相同验证区间进行组合评价。

新建完整运行的结果位于 `learning_curves.json`、`learning_models` 和相应 `episodes/learning_*`；本次从 source3 继承的九条曲线继承封存的 full3 证据，逐回合 `episodes/learning_*` 仍在原父目录，不能把新目录中不存在的路径当作新计算。检查请求起点、实际覆盖起点、成熟行数、决策日期数、20 日时间块数、缺失及未成熟行数。`observed_month_coverage` 是 0/1 的请求起点覆盖标志，月数读取 `training_months`，实际覆盖范围读取 `actual_train_start`／`actual_train_end`。36 个月请求不保证输入真的覆盖 36 个月，覆盖不足必须保留报告。日线代理标签重叠、同日币种相关、相同市场阶段重复出现；更多数据行、时间块或随机种子不会自动增加等量独立样本。

`membership_evidence.json` 记录成员证据，当前静态币池和今天的交易所现货资格不能补齐历史上市、退市及消息可得时间。没有可追溯历史 PIT 成员材料时，历史结果继续标记覆盖限制，不宣称已经纳入全部消失资产或修复幸存者偏差。

## 5. RL 实际更新预算、门槛与资源读法

正式主窗口使用 `2020-01-01` 起的训练材料，训练截止 `2023-01-01`，验证截止 `2024-07-01`。额外 RL 窗口为第四滚动窗口，训练截止 `2025-01-01`，验证截止 `2025-07-01`，历史评价截止 `2026-01-01`。

| 正式预算字段 | 当前登记 |
|---|---|
| 每个窗口的种子 | 42、43、44 |
| RL 窗口数 | 主窗口 + 滚动窗口 4，共 2 个 |
| 每个种子的最低实际更新 | 20 |
| 每个种子的最高回合尝试数 | 30 |
| 早停最早启动水位 | 20 次实际更新 |
| 验证停滞容忍 | 3 |
| 正式最低总更新预算 | 2 × 3 × 20 = 120 |
| 正式最高回合尝试数 | 2 × 3 × 30 = 180 |
| 正式主奖励 | 净权益对数变化，回撤／换手额外权重均为 0 |
| 验证概率门槛 | 0.49、0.50、0.51；默认 0.50 |

每个完整训练回合有合法原策略候选轨迹时，调用一次 `policy.update()`，按 `policy.update_count` 计一更新。没有候选轨迹的回合只消耗回合尝试，不消耗更新水位；采样到全部拒绝但仍有合法动作轨迹，与完全没有可行动候选不同。最低更新和延迟启动条件均满足后，验证停滞才允许早停。达到 30 回合却没有 20 次更新，报告预算不足，不补造更新。

训练动作随机采样；确定性评估使用 `p >= threshold`。每个 checkpoint 只在验证材料中完整比较三条预登记门槛，并把选择门槛写入 policy metadata，进入 checkpoint 身份。收益相同时保留默认门槛。测试期读取冻结门槛，不通过测试结果调节门槛。

奖励 pilot 使用主窗口、权重 `0／0.25／0.5`、三个种子和每个实验单元两个回合，共九个实验单元，最多 18 次更新。它冻结独立注册矩阵，不改正式协议，结果标明 `formal_budget_contribution=0`，不能把 pilot 更新加进 120 次正式预算。现金保持合法，原引擎真实费用已进入净权益，不加固定现金惩罚迫使交易。

主窗口和额外窗口下，每个 `rl_seeds/<seed>` 保存：

- `rl_training.json`：已完成回合、候选动作、baseline、梯度／熵、概率分布、全部验证门槛结果和 checkpoint 身份。
- `models/policy_NNN.json`：各已完成回合模型；`policy_latest.json`、`policy_best.json` 是便捷别名。
- `rl_budget_receipt.json`：`actual_updates`、`minimum_budget_met`、`stop_reason`、已完成 checkpoint 更新水位及早停状态。
- `rl_resources.json`：实测训练、验证和整回合墙钟时间，候选量及 RAM。

RAM 通过 Windows 进程工作集或系统 `getrusage` 测量，峰值范围明确为 `process_lifetime`，不是该回合独占内存峰值。资源报告保留测量缺失；不能把未知 RAM 写成零，也不根据回合上限硬估总训练时间。`rl_seed_comparison.json` 和根目录预算回执汇总各种子，正式声明还应检查两个窗口六个种子的全部回执。最佳模型文件可能来自较早回合，其自身更新水位不能替代最终训练预算水位。

当前使用完整历史回合；短回合／分段抽样、账户起点和持仓路径恢复没有新增实现。

## 6. 同批资金分配、滚动与执行压力

`fixed_batch_allocation_comparison.json` 在验证期最多记录 12 个竞争批次，对同一批合格原策略候选比较原评分、动量、父模型评分和固定随机评分。每个对照复制原引擎当前候选、账户、broker、风控和 allocator 的完整对象图，保留对象间引用关系，在副本中调用原资金分配器。实际运行仍使用原选择器。

这项诊断记录预算约束、批准集合、批准数量和拒绝原因，检查评分变化是否真的改变入选或数量。副本只进行分配和订单批准，不运行成交撮合；其 `matching_executed=false`、`portfolio_return_difference=null`。因此批准数量差异不能直接称为收益差异。无法复制或验证状态的批次保留 `unmeasured`。

四个评分滚动窗口都报告；只有登记的第四窗口额外训练 RL。每个窗口独立初始化研究账户，不把不同窗口的权益曲线拼成连续账户收益。评价包含原规则、合格候选原规则、动量、随机种子、三个监督模型及已训练 RL，并保留负收益、风控终止和会计条件。

冻结候选后进行 1.5／2 倍成本重放，放大原执行佣金、费率表、滑点、价差及冲击相关成本参数，保存 `cost_stress.json`。另保存 `execution_stress.json` 的三项登记情景：

- 开仓延迟 1 根日线，原退出和风险行为仍由原引擎处理。
- 参与率 `0.00001`，开仓订单 TTL 为 3 根日线，检查部分成交和过期等结果。
- 初始现金 10,000，检验资金预算受限时的批准与实际执行。

这些情景属于冻结的研究压力账户。它们不关闭生产风控或策略健康，也不能证明参数之外所有执行风险都已覆盖。收益集中度同时保留固定入场时间块诊断和实际持仓区间重叠事件归并；区间事件、固定时间块及随机种子都没有被证明统计独立。事件数量不足或去掉盈利组后证据不全时保留未判定。

## 7. 取消、恢复与已完成水位

恢复之前确认当前源码、配置、注册输入和冻结产物没有改变。`rl_training.json` 是已完成水位，恢复逐条校验回合编号连续、编号 checkpoint 存在、父模型身份、模型身份、累计更新数和历史计数一致。训练中断可能使 `policy_latest`／`policy_best` 别名领先于历史；恢复从已提交编号 checkpoint 重建别名，未提交回合重新运行。未完整保存的回合不能计入完成更新预算。

主窗口已有 `protocol.json`、`dataset.csv`、监督模型、`parent_model.json` 和 `validation_comparison.json` 时，可使用现有阶段入口恢复其 RL：

```powershell
.\.venv\Scripts\python.exe scripts/train_selector.py `
  --stage rl --run-dir reports/ml_selection_resume_example
```

示例目录需要替换为同一源码版本下真实尚未完成的运行，不对本次已封存的 full3/full4/full5 或最终 full6 结果重复训练。该命令先验证冻结身份，再恢复三个种子的已完成水位，随后按验证结果冻结候选。不能同时传入 `--rl-episodes` 或 `--max-symbols` 改预算。候选已经冻结后若计算结果改变，程序会拒绝覆盖旧候选；应检查身份或创建新的实验。

主窗口 RL 完成且候选已冻结后，现有 `--stage evaluate` 可执行主窗口冻结评价。这个阶段入口不会补做退出 probe、学习曲线、桥接或整轮总结；完成主窗口 RL 不等于完整下一轮已经恢复完成。

额外窗口的训练状态保存在 `walk_forward/window_04/window_protocol.json`、其监督模型和各 `rl_seeds` 下。当前没有直接恢复单个额外窗口或整轮 `all` 的专用 CLI；子窗口的 `window_protocol.json` 也不是主运行 `protocol.json`。恢复额外窗口需要先校验父协议和窗口设置，使用对应窗口的监督模型与现有训练子函数。不要把主窗口协议传到额外窗口继续训练，也不要杜撰 `--resume` 或 `--window 4` 命令。

`--stage walk-forward` 是既有整个滚动阶段的入口，不是独立窗口恢复选项。整轮中断后仍需明确哪些阶段已完成、哪些摘要尚未生成；在专用恢复入口实现前，整轮继续需要按这些真实阶段能力处理。恢复行为与成功条件以当时实际源码为准。

## 8. 公共采集、冻结 RL 桥接与真实前向观察

公共行情采集不需要交易凭证，不提交订单。每次采集新增时间戳子目录，保存已收盘现货日线、字节与帧身份、接收时间，以及交易所当前现货资格原始回复和身份。

```powershell
.\.venv\Scripts\python.exe scripts/collect_ml_selection_forward.py `
  --output reports/ml_selection_public_forward_20261005 `
  --symbols BTC/USDT ETH/USDT --history-days 90
```

命令默认范围是 BTC、ETH 两个现货交易对；要覆盖完整研究币池，应明确传入全部注册交易对，并查看采集失败和覆盖。本轮还按 60 个注册交易对采集了 `public_binance_20261004T181036479390`：55 币各 90 根已收盘日线且当前允许现货，23 币未达到冻结流动性门槛，32 币合格；MATIC、EOS、XMR、FTM、MKR 的当前取数及资格缺口保留。55/60 是部分覆盖，不能称为完整历史或前向币池。它证明接收时的公共行情和当前资格，不证明过去的完整上市／退市成员表。采集到目录存在也不保证数据全部新鲜或资格全部通过，使用前仍检查 `collection.json`。

`RecordingSelector` 在原引擎候选 hook 捕获现金、持仓、挂单、价格、权益高水位、回撤、风险预算、策略健康及完整原候选，以协议、父模型、政策和 snapshot 身份关联。`rl_hook_states.json`、`rl_bridge_verification.json` 报告冻结政策动作重放是否与原 hook 一致，验证错误及未取得快照都保留。历史桥接重放标记 `forward_evidence=false`，不冒充新数据观察。

本轮完整 60 币研究范围的 source6 历史桥接实际取得 144 个快照且全部一致。该结果验证固定政策决策与记录恢复；它不代替真实当前账户、模拟成交与净权益证据。当前公共数据只产生父评分诊断，合格未来日期和成熟标签数仍为 0。

对于冻结监督候选，现有影子命令记录新输入及分数。对于冻结 RL 候选，还需要当时原引擎生成的完整状态 JSON：

```powershell
.\.venv\Scripts\python.exe scripts/train_selector.py `
  --stage shadow --run-dir reports/ml_selection_next_full6_20261005 `
  --market-data-dir <本次不可变公共采集子目录>

# 冻结候选为 RL 时，再提供当时完整原引擎 hook 状态：
.\.venv\Scripts\python.exe scripts/train_selector.py `
  --stage shadow --run-dir reports/ml_selection_next_full6_20261005 `
  --market-data-dir <本次不可变公共采集子目录> `
  --account-state <当时完整RL状态JSON>
```

尖括号表示必须替换的真实路径，不是可以原样执行的 PowerShell 参数。不要把历史 replay 的状态用于当前观察。RL 缺少当时账户与策略状态时，只能记录父评分诊断并保留 `requires_account_and_strategy_state_for_frozen_RL_candidate`；父模型评分不能替代冻结 RL 决策。决策桥接不恢复完整账户，也不产生模拟成交或权益结果。

影子观察使用实际时钟和可得信息截点。`--as-of` 可以缩小读取截点，但不能放到未来；历史截点重放仍标记 retrospective。观察内容写入 `shadow/observation_*.json`，原始预测不回写。真实 UTC 日内观察的代理入场最早从观察之后下一可执行日线开盘开始，ATR 固定在观察时。

未来代理标签成熟后，以当时新采集目录追加结果：

```powershell
.\.venv\Scripts\python.exe scripts/train_selector.py `
  --stage resolve-shadow --run-dir reports/ml_selection_next_full6_20261005 `
  --market-data-dir <成熟时的新公共采集子目录>
```

该命令校验原观察身份、已记录特征与预测没有被修订，再追加 `shadow/outcomes_*.json`。未成熟结果保留 pending。独立代理收益、RL 入选动作和原引擎模拟账户实际成交收益分别保存；不能用计划仓位或代理收益填补实际模拟成交证据。

`research.ml_selection.forward_evidence.evaluate_forward_store(folder, protocol, as_of=None)` 是只读汇总函数，校验协议、冻结候选、观察与成熟结果身份，报告合格日期、缺失、陈旧、资格覆盖、入选概率及特征漂移。它返回报告，不修改观察或标签；当前没有对应独立 CLI。需要保存报告时，由调用方把返回值写到独立汇总文件，不能修改原观察。

当前配置登记真实前向起点为 `2026-10-06T00:00:00Z`，新加坡时间为 **2026 年 10 月 6 日 08:00**；计划最终样本截止为 `2027-01-01T00:00:00Z`，即新加坡时间 **2027 年 1 月 1 日 08:00**。冻结候选数上限为 1，最低要求 30 个合格前向决策日期，代理成熟期为 20 根日线。一天多次采集不增加独立日期数。

起点前的工程采集不计入这 30 天。协议未在登记起点前冻结、缺新鲜行情、成员资格未验证、缺 RL 状态、没有合格输入或标签未成熟时，前向证据仍 pending。协议与候选 `frozen_at` 必须都严格早于登记起点；缺少时间、迟冻或边界相等都不计合格日期，不补造旧冻结时间。只读评价当前还明确保留 `original_engine_simulated_fill_and_equity_evidence_pending`；即使取得 30 个日期和成熟代理标签，也不能因此声称模拟账户成交与权益证据齐全。若未在起点前完成实际冻结，不能事后倒填时间，应重新登记未来观察区间。

## 9. 结果核对与停止声明条件

本次报告的完成数与结果来自已生成的真实回执；后续新实验仍须**以 `next_execution_receipt.json` 实际值为准**。回执必须列全六个预期种子／窗口及缺失单元，存在的五个回执全部达标也不能代表六个完成。正式预算前若奖励 pilot 有失败或未完成单元，停止扩大预算并保存失败回执。如果该文件尚未生成，说明不能凭计划或部分日志声称整轮已完成。

本轮 source6 的最终本地测试为 3,623 项通过、1 项跳过、111 个子测试通过，覆盖率 88.33%，达到 55% 门槛。完整套件实际耗时 630.71 秒；质量检查、124 次更新身份、26 个模型、144 个完整桥接状态和旧结果归档均核对通过。[质量回执](../reports/ml_selection_next_full6_verification_20261005/quality_receipt.json)与[最终验收记录](research/ml_selection_next_acceptance_20261005.json)保存具体证据。真实历史 PIT、未来合格日期、成熟模拟执行账户及独立 final 继续 pending，不与工程通过合并判定。

| 核对项 | 真实证据 |
|---|---|
| 正式实际更新及所有最低预算 | 六个种子／窗口的 `rl_budget_receipt.json`，整轮 `next_execution_receipt.json` |
| 奖励 pilot 的实际更新和资源 | `reward_pilot/pilot_registration.json`、`pilot_results.json`、`pilot_resource_report.json` |
| 候选链路、低敞口与奖励核对 | `v1_diagnostic`、各 episode 的诊断账本和原引擎审计 |
| 真实退出 probe 和目标缺失 | `label_contract.json`、`exit_label_probe_rows.csv`、`exit_label_probes.json` |
| 排名、校准及组合表现 | `supervised_validation.json`、`validation_comparison.json`、`learning_curves.json` |
| 排序是否改变同批批准和数量 | `fixed_batch_allocation_comparison.json` |
| 全部滚动、成本、执行与风险结果 | `walk_forward.json`、各窗口及主窗口的比较、压力和裁决文件 |
| 冻结 RL 状态桥接一致性 | `rl_hook_states.json`、`rl_bridge_verification.json` |
| 当前真实前向资格与成熟进度 | `shadow` 追加记录、`forward_evidence.json` 或只读汇总返回值 |

配置范围内的负收益、无成交、现金胜出、预算不足、采集失败及无效窗口都必须报告。收益、风险、会计、历史成员证据、独立最终样本和真实前向证据分别裁决。历史测试已观察、历史 PIT 不完整、集中度证据不足、最低更新未完成、没有真实未来时间经过或缺模拟成交账户路径时，停止相应效力与正式准入声明；源码存在和 checkpoint 保存不会解除这些条件。
