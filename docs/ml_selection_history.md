# 机器学习选币:V1 复核与本轮研究记录(合并)

> 合并自 V1 复核、本轮执行、冻结回测、候选流诊断与稳健性解读。均为带日期的证据记录,不维护当前状态;现状见 [ml_selection.md](ml_selection.md)。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/ml_selection_v1_review_20261005.md -->
## 机器学习选币 V1 实施与研究复核

记录日期：2026-10-05（Asia/Singapore）。本文件记录 V1 已实现的行为、本地运行证据和本轮研究结论，作为代码提交前的复核说明。

**V1 的离线训练与历史对照流程已经运行完成，本轮选币有效性未通过。** 工程测试、旧基线保持和模型收益是三个不同结论。当前 `retrospective_checks_passed=false`、`formal_admission=false`、`production_enabled=false`，没有实盘启用或真实订单。

### 实现范围与原系统关系

V1 使用日线、现金现货、多头与现金。原策略先生成交易候选，研究选币器再决定是否入选并修改排序；资金分配、仓位限制、策略健康、退出、保护止损、订单撮合和成本仍由原系统处理。

`BacktestEngine` 和共享 `EventProcessor` 新增可选的 `candidate_selector` 接口，默认值为 `None`。未启用研究选币器时沿用原路径；研究选择发生在候选收集后、组合分配前，不替换执行引擎。接口默认关闭，也没有把研究模型接入正式实盘。

主要实现为：

| 模块 | 已实现行为 |
|---|---|
| `research/ml_selection/protocol.py` | 配置校验、冻结协议、代码与输入身份、产物校验 |
| `research/ml_selection/dataset.py` | 因果特征、资格排除、独立代理标签、成熟时间与跨界清除、前向标签 |
| `research/ml_selection/models.py` | NumPy Ridge、CPU LightGBM、模型 JSON 与小型 Bernoulli 策略 |
| `research/ml_selection/selector.py` | 无标签的推理输入、候选门槛与排序、账户特征、决策审计 |
| `research/ml_selection/environment.py` | 完整原引擎回合、权益奖励、实际成交换手、风险终止与末尾处理 |
| `research/ml_selection/pipeline.py` | 评分训练、原引擎对照、多种子 RL、候选冻结、压力与滚动评价、影子日志 |
| `scripts/train_selector.py` | 统一离线 CLI 和分阶段运行 |
| `scripts/verify_ml_selector_baseline.py` | 使用旧登记及旧账户语义复现历史 smart 基线 |

运行细节见 [训练入口与现有工程说明](ml_selection.md)。本文不扩展下一阶段安排或学习清单。

### 数据、标签和训练算法

默认登记名单包含 60 个币，历史范围为 2020-01-01 至 2026-09-18。所有输入先按登记哈希核对。本次共生成 129,423 条候选记录，其中 95,032 条符合资格；成熟训练分区包含 41,980 条记录，覆盖 **55 个币、1,077 个决策日期**。60 是输入币池数量，55 是本次实际成熟训练样本覆盖的币数，不能混用。

评分模型使用 21 个行情、波动、流动性及 BTC 相对表现特征。特征只使用决策时已经收盘且可得的历史，推理表不包含未来标签。缺失日线、不足历史、低流动性及生命周期限制保留明确排除原因。

标签采用小额、独立、假设完整成交的代理多头交易：下一根可执行开盘入场，在固定窗口或冻结 ATR 止损条件下退出，并扣除登记的成本。默认窗口为 20 根日线。这是训练目标，**不能将各币代理收益相加作为组合收益**；真实组合结果由原引擎重新执行。

训练期截至 2022-12-31，验证期为 2023-01-01 至 2024-06-30，历史测试期为 2024-07-01 至 2026-09-18。分区按时间切分、完整同日分组，并清除未成熟和跨界标签；预处理仅在训练样本拟合。已有历史测试区间属于回顾性研究材料，未获得独立最终样本身份。

评分阶段训练 NumPy Ridge 和 CPU LightGBM。奖励学习的准确名称是 **完整回合 Bernoulli REINFORCE 策略梯度**：先运行完整原引擎训练历史，再根据动作的后续累计奖励更新参数。当前实现不是 PPO，没有 Gym 的逐步 `reset/step` 接口，也没有另建简化交易执行器。

三个 RL 种子为 42、43、44。本轮每个训练回合记录 244—460 个候选动作；三个种子分别完成 **4、5、4 次回合更新**后早停。这些动作来自原策略可交易候选，不能把 129,423 条输入记录都视作 RL 已执行动作。

奖励采用净权益对数变化，减去回撤加深惩罚及可选的实际成交换手惩罚。默认回撤权重为 0.5、额外换手权重为 0。净权益已经包含费用与未实现盈亏，不重复扣费；未成交目标不计换手，风险终止后的冻结展示尾部不继续奖励，末尾实际清算费用保留。

### 候选冻结与本轮结果

父评分模型先根据验证组合奖励选择。随后在验证区间比较评分模型及 RL，按预登记的正净收益、回撤、会计一致性条件筛选，再按奖励冻结最终研究候选。没有模型通过时仍保存研究候选，但明确标记资格未通过；历史测试不参与选择。

本轮验证结果：Ridge 净收益 −2.79%，LightGBM −0.74%，原规则约 −0.36%。三个 RL 种子的最佳验证策略均保持现金：0 收益、0 成交、0 敞口和 0 奖励。最终研究候选为 RL，`validation_qualification_passed=false`。

保持现金是合法选择，零收益不满足默认正收益要求。本轮最佳 RL 验证的候选概率略低于确定性入选门槛 0.5，因此全部拒绝；不能将这个结果解释为模型高度确信所有币都应该回避。回撤加深惩罚按路径累计，也可能让正收益交易策略的奖励低于现金。

| 主历史测试方案 | 净收益 | 最大回撤 | 实际成交次数 |
|---|---:|---:|---:|
| 原规则 `native` | −4.10% | 7.63% | 108 |
| Ridge | +3.33% | 6.43% | 88 |
| LightGBM | −2.99% | 6.64% | 102 |
| 冻结 RL 候选 | 0.00% | 0.00% | 0 |

Ridge 的测试盈利是研究线索，但其验证期未通过，测试前未被选为最终候选。看过测试之后改选 Ridge，再声称 V1 成功，会破坏验收口径。主要交易方案的平均敞口仅约 1.96%—1.97%，策略健康暂停和资金风控共同影响结果，不能把全部收益变化归于模型排序。

四个预登记评分滚动窗口已完成，各窗口单独重训、验证选模型，再测试；其中三个相对原规则改善，但三个窗口的候选净收益仍为负。没有在四个窗口分别重训 RL，也没有将四段拼接成连续账户收益。

历史评价还包含 1.5、2 倍成本压力，以及重建持仓净盈亏后按固定 5 日入场组检查去掉最大 5/10 组的集中度。本轮冻结 RL 候选没有成交，集中度证据为不足；时间分组本身也不证明组间独立。

### 本地验证证据与分发范围

本地完整运行位于 `reports/ml_selection_full_20261004_v3/`，主线、多种子 RL 和四个滚动评分窗口共用时约 502 秒。本次记录的核对结果为：

| 本地证据 | 本次核对结果 |
|---|---|
| ML 专项测试 | 142 项通过 |
| 原路径回归 | 98 项通过，另有 6 个子测试通过 |
| 输出产物登记 | 586 个输出文件身份核对 |
| 源文件身份 | 198 个源文件哈希 |
| 完整引擎运行记录 | 77 份 episode 摘要 |
| 保存模型 | 30 个模型文件，可重新加载并核对身份 |
| 会计一致性 | 主线和滚动窗口均通过 |
| 旧 smart 基线 | 2,453 个日历日、664 次成交，每日权益最大绝对差 0；执行及资金分配摘要一致 |

旧基线使用原 `spot_margin` 账户登记，最终权益为 172,821.7372157314。新 ML `native` 是适配为现金现货的研究对照，两者分别保留，不能直接认作同一账户回测。旧基线复现证明可选研究接口未改变该历史基线，不证明新模型有效。

**上述输入、完整结果、模型、77 份 episode 和 586 个输出属于本地研究证据，不随本次代码仓库提交分发。** 本地证据入口包括 `execution_receipt.json`、`protocol.json`、`artifacts.json`、`dataset_summary.json`、`rl_seed_comparison.json` 和 `report.md`。文档保存证据数值和结论；远端仓库读者不能仅靠克隆代码直接取得这些本地产物。

测试数量和哈希是本批次的记录，不能替代其他机器重新运行测试或新实验的身份检查。代码、数据或配置发生变化时应创建新冻结实验，而不是沿用旧结论。

### 提交前恢复一致性修复

2026-10-05 审查并修复了两项中断恢复问题，追加 12 项回归测试：

- RL 以已经提交的回合历史及对应编号模型检查点作为恢复依据，检查连续回合、冻结预算、父模型及模型身份。可能先于历史写入的 latest/best 便利别名不作为恢复水位；恢复会根据已完成历史纠正它们。
- 重跑回合会保留原目录并写入 `_attempt_N`。集中度评价依据候选摘要中的实际产物目录读取完整持仓记录，检查路径仍位于本次实验目录内，避免读取旧的半成品。

V3 的 198 个冻结源文件已按原哈希保存在本地 `reports/ml_selection_v3_source_archive_20261005/`，旧结果未改写。修复后的代码与 V3 冻结代码不同，后续训练使用新实验；这里没有把恢复修复当作重新训练或新的收益证明。GitHub 增加单独的 ML 检查，安装可选 CPU 依赖并使用合成行情测试，不依赖个人机器的历史报告。

修复后 ML 专项共 154 项测试；V3 表中的 142 项保留为该运行批次的历史记录。提交前已通过依赖与锁文件完整性、历史归档、仓库文件卫生、Roadmap 结构、全仓库 Ruff 和关键运行接口 Mypy 检查。最终远端检查与合并身份以 GitHub PR 记录为准。

### 代码合并与追加复核记录

本实现已通过 [PR #48](https://github.com/user-nmmmmm/QuantTradingV2/pull/48) 合并到 `main`，合并提交为 `77f9ca4198f5cd52d5c1cc32c2d56629f1fad2fc`，时间为 2026-10-05 00:37:05（Asia/Singapore）。[远端 CI](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/37217022354) 的 `quality`、`ml-research`，以及离线回测 `synthetic-smoke-and-regression` 三项检查全部成功。完整质量测试为 3,487 项通过、111 个子测试通过、2 项跳过，核心/回测/实盘模块合计覆盖率 88.32%；ML 依赖专项另行执行全部 154 项测试。

合并前使用修复后的源码再次核对旧 smart 基线，结果仍为 2,453 天、664 次成交、逐日权益差 0，执行、资金分配和会计检查均通过，证据位于本地 `reports/ml_selector_baseline_verification_20261005_merge/validation.json`。Windows 受限沙箱的全量测试曾卡在创建 asyncio 事件循环，不能记为通过；远端完整测试提供独立的工程验证。

随后在 Windows 普通主机权限下完整测试通过：3,488 项通过、111 个子测试通过、1 项跳过，覆盖率同为 88.32%，耗时约 449 秒。该本地环境包含可选 ML 依赖，远端 `quality` 不包含这些可选依赖，测试计数因此有差异；远端另设 `ml-research` 检查。记录位于本地 `outputs/ml_merge_full_host_20261005.log` 及 `outputs/test_runtime/ml_merge_full_host_20261005.xml`。

合并后的研究推进见[下一阶段 Roadmap](ml_selection.md)，相关概念和验证练习见[学习清单](ml_selection_learning_checklist_20261005.md)。合并确认代码进入主分支，没有改变本轮模型未通过有效性验收的结论。

### CLI 与克隆后的输入限制

以下命令从仓库根目录运行，使用仓库相对路径。Windows 示例依赖已准备好的 `.venv`；其他环境可将解释器替换为本机 Python：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-ml.txt
& .\.venv\Scripts\python.exe scripts/train_selector.py --config config/ml_selection.yaml --max-symbols 8 --rl-episodes 2
```

默认三个种子各最多 2 个 RL 回合，以上是最多 6 个训练回合的小规模完整流程。去掉两个规模参数，使用默认 60 个币及每种子最多 6 回合：

```powershell
& .\.venv\Scripts\python.exe scripts/train_selector.py --config config/ml_selection.yaml
```

当前默认配置需要本地历史登记与数据：

- `reports/multicoin_100k_20261004/registration.json` 及该登记目录下的 `input/engine/*.csv`。
- `reports/smart_capital_100k_20261004/registration.json` 中的历史账户、策略和引擎选项。

这些文件不随本次代码提交分发。**新克隆若没有这两个登记及匹配 CSV，默认训练和旧基线验证无法直接启动。** CLI 不会自动下载数据、伪造登记或忽略哈希。需要按实际持有的数据准备符合接口的登记并创建新配置；这会形成新实验，不等于复现本地 V1。

旧基线验证还依赖原归档的权益、执行摘要和资金分配摘要。具备完整原归档时，可另用尚不存在的输出目录执行：

```powershell
& .\.venv\Scripts\python.exe scripts/verify_ml_selector_baseline.py --output reports/ml_selector_baseline_check
```

运行使用 CPU；LightGBM 依赖位于可选 `requirements-ml.txt`，Ridge 与 REINFORCE 使用 NumPy。当前实现无需 GPU、CUDA 或 PyTorch。

### 工程与前向证据边界

已实现的离线范围包括数据与代理标签、评分模型、多种子奖励学习、完整原引擎回合、验证冻结、历史对照、滚动评分、成本压力和身份核对。这个范围已通过本轮工程检查，但没有放行模型有效性。

历史币池仍为静态名单和 `observed_history_only`，缺少完整当时可交易成员、上市、退市及信息可得时间的 PIT 证明。历史 `test` 不是独立最终样本；集中度分组独立性和多重试验证据尚未成立。

影子日志区分真实 `observed_at` 与信息截点，记录内容身份、特征和模型；成熟结果追加到新文件，禁止改写原预测。真实日内观察的前向代理收益最早从观察之后的下一可执行日线开盘开始，使用观察时冻结 ATR，排除观察前已经过去的开盘与行情；历史重放继续标记为 `retrospective`。

当前本地输入只到 2026-09-18，实际影子检查没有新鲜合格样本。冻结候选为 RL，其前向决策仍需要账户和原策略候选状态桥接，父评分模型诊断不能替代该 RL 候选的前向表现。新行情、标签成熟和完整前向收益证据尚未取得。


---

<!-- 合并自 docs/ml_selection_next_execution_20261005.md -->
## ML 选币下一轮工程运行与恢复指南（2026-10-05）

本指南对应 [下一轮路线图](ml_selection.md)、`config/ml_selection_next.yaml` 和 `scripts/run_ml_selection_next.py`。它说明已经存在的运行入口、结果证据和恢复边界；实验实际完成情况以 `next_execution_receipt.json` 的实际值为准。配置中的 120 次最低更新与 180 回合上限属于预登记预算，不能直接写成已完成结果。

全部实验使用离线日线现货研究账户。原引擎负责策略、仓位、资金分配、风控、策略健康、订单、撮合和终止计价。合并代码、取得历史研究结果、取得真实前向证据、正式准入是不同状态；本轮不自动启用生产交易。

### 1. 目录、运行环境与冻结条件

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

### 2. 先运行 pilot，再执行正式预算

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

### 3. 标签、排序与收益门槛分别回答什么

当前基准训练目标仍为固定 20 日窗口、冻结 ATR 止损及固定代理成本下的独立代理交易收益。`label_contract.json` 同时登记原策略退出 probe：从训练期记录中选取最多 12 个原策略候选，在固定空仓现金研究账户加入一个候选，名义金额上限为 1,000，风险预算比例为 2%，检查最多 60 根日线，仍接受原策略、原风控及真实撮合约束。

probe 的退出收益来自实际原引擎成交。没有复现原信号、未成交、未完成退出、风险终止或会计未核对时，目标保持缺失；没有买入不能写成零收益。输出 `exit_label_probe_rows.csv` 和 `exit_label_probes.json`。probe 是目标定义诊断，当前整轮不会用这 12 个 probe 替换基准训练标签；单候选独立干预也不能证明组合中加入或替换该候选的边际价值。

监督对照包括 Ridge、LightGBM 回归和真正使用 `lambdarank` 目标的排序模型。排序学习按同一 `as_of` 决策时点分组，把组内收益次序映射成预登记的五档相关性标签；缺失或未成熟成员不会被删除后单独美化剩余组。

LambdaRank 的预测是组内排序分数，没有净收益单位。其独立 Ridge 收益门槛只用训练期原始特征和训练收益拟合，`predict_net_return()` 提供收益尺度，`min_expected_return=0` 用于是否值得进入候选门槛；排序分数用于合格候选之间的资金分配次序。RL 输出则是动作入选概率，不是盈利概率。

`supervised_validation.json` 同时提供同日排名相关、收益分组及收益校准；`validation_comparison.json` 给出完整原引擎组合结果。排名指标改善、收益校准改善和真实组合表现改善必须分别阅读。

### 4. 12／24／36 个月学习曲线与 PIT 边界

`next_research.learning_months=[12,24,36]` 对三个监督模型分别拟合九个模型。每个长度只使用对应训练期内已经成熟的完整决策组；缩放器在各自训练子集上拟合。验证区间、目标、特征、种子和模型参数预算固定，并通过原引擎在相同验证区间进行组合评价。

新建完整运行的结果位于 `learning_curves.json`、`learning_models` 和相应 `episodes/learning_*`；本次从 source3 继承的九条曲线继承封存的 full3 证据，逐回合 `episodes/learning_*` 仍在原父目录，不能把新目录中不存在的路径当作新计算。检查请求起点、实际覆盖起点、成熟行数、决策日期数、20 日时间块数、缺失及未成熟行数。`observed_month_coverage` 是 0/1 的请求起点覆盖标志，月数读取 `training_months`，实际覆盖范围读取 `actual_train_start`／`actual_train_end`。36 个月请求不保证输入真的覆盖 36 个月，覆盖不足必须保留报告。日线代理标签重叠、同日币种相关、相同市场阶段重复出现；更多数据行、时间块或随机种子不会自动增加等量独立样本。

`membership_evidence.json` 记录成员证据，当前静态币池和今天的交易所现货资格不能补齐历史上市、退市及消息可得时间。没有可追溯历史 PIT 成员材料时，历史结果继续标记覆盖限制，不宣称已经纳入全部消失资产或修复幸存者偏差。

### 5. RL 实际更新预算、门槛与资源读法

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

### 6. 同批资金分配、滚动与执行压力

`fixed_batch_allocation_comparison.json` 在验证期最多记录 12 个竞争批次，对同一批合格原策略候选比较原评分、动量、父模型评分和固定随机评分。每个对照复制原引擎当前候选、账户、broker、风控和 allocator 的完整对象图，保留对象间引用关系，在副本中调用原资金分配器。实际运行仍使用原选择器。

这项诊断记录预算约束、批准集合、批准数量和拒绝原因，检查评分变化是否真的改变入选或数量。副本只进行分配和订单批准，不运行成交撮合；其 `matching_executed=false`、`portfolio_return_difference=null`。因此批准数量差异不能直接称为收益差异。无法复制或验证状态的批次保留 `unmeasured`。

四个评分滚动窗口都报告；只有登记的第四窗口额外训练 RL。每个窗口独立初始化研究账户，不把不同窗口的权益曲线拼成连续账户收益。评价包含原规则、合格候选原规则、动量、随机种子、三个监督模型及已训练 RL，并保留负收益、风控终止和会计条件。

冻结候选后进行 1.5／2 倍成本重放，放大原执行佣金、费率表、滑点、价差及冲击相关成本参数，保存 `cost_stress.json`。另保存 `execution_stress.json` 的三项登记情景：

- 开仓延迟 1 根日线，原退出和风险行为仍由原引擎处理。
- 参与率 `0.00001`，开仓订单 TTL 为 3 根日线，检查部分成交和过期等结果。
- 初始现金 10,000，检验资金预算受限时的批准与实际执行。

这些情景属于冻结的研究压力账户。它们不关闭生产风控或策略健康，也不能证明参数之外所有执行风险都已覆盖。收益集中度同时保留固定入场时间块诊断和实际持仓区间重叠事件归并；区间事件、固定时间块及随机种子都没有被证明统计独立。事件数量不足或去掉盈利组后证据不全时保留未判定。

### 7. 取消、恢复与已完成水位

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

### 8. 公共采集、冻结 RL 桥接与真实前向观察

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

### 9. 结果核对与停止声明条件

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


---

<!-- 合并自 docs/research/ml_selection_frozen_backtest_20261005.md -->
## 冻结新选币器的最新历史回测（2026-10-05）

本次在新目录重新运行原引擎，固定既有主候选 RL 和三个监督模型的权重及门槛，未训练、未搜索新参数、未按本次收益改选候选。日线已延长至 **2026-10-03**，只使用已经收盘的数据。两组账户均以 **100,000 USDT** 开始，原策略、策略健康、风险、资金分配、手续费、滑点、撮合及真实终端平仓规则保持一致。

完整评价账户从 2024-07-01 连续运行，2026 对照账户从 2026-01-01 独立初始化。连续账户逐年归因另外报告，不把独立账户结果拼接为连续收益。主候选保持源6冻结 RL，父模型为 LightGBM，概率门槛为 **0.51**。

### 2024-07-01 至 2026-10-03：连续账户

| 方案 | 期末净资产（USDT） | 净利润（USDT） | 净收益 | 年化收益 | 最大回撤 | 平均总敞口 | 成交行数 | 佣金（USDT） | 会计 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 原规则 | 96,587.78 | -3,412.22 | -3.4122% | -1.5253% | 7.6314% | 2.1010% | 116 | 190.83 | 通过 |
| 相同资格的原规则 | 97,359.05 | -2,640.95 | -2.6410% | -1.1779% | 7.0368% | 2.0897% | 110 | 185.28 | 通过 |
| LightGBM | 97,777.21 | -2,222.79 | -2.2228% | -0.9903% | 6.6376% | 2.1011% | 110 | 186.07 | 通过 |
| Ridge | 104,062.05 | +4,062.05 | +4.0620% | +1.7784% | 6.4339% | 2.0949% | 98 | 170.96 | 通过 |
| LambdaRank + 收益门槛 | 103,945.71 | +3,945.71 | +3.9457% | +1.7281% | 6.5386% | 2.0774% | 98 | 170.40 | 通过 |
| 冻结 RL 选币器 | 102,805.16 | +2,805.16 | +2.8052% | +1.2324% | 2.5961% | 0.7271% | 4 | 35.60 | 通过 |

### 2026-01-01 至 2026-10-03：独立账户

| 方案 | 期末净资产（USDT） | 净利润（USDT） | 净收益 | 年化收益 | 最大回撤 | 平均总敞口 | 成交行数 | 佣金（USDT） | 会计 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 原规则 | 97,817.12 | -2,182.88 | -2.1829% | -2.8785% | 6.2222% | 6.4607% | 44 | 134.74 | 通过 |
| 相同资格的原规则 | 101,610.92 | +1,610.92 | +1.6109% | +2.1374% | 5.0400% | 7.4323% | 54 | 141.83 | 通过 |
| LightGBM | 101,610.92 | +1,610.92 | +1.6109% | +2.1374% | 5.0400% | 7.4323% | 54 | 141.83 | 通过 |
| Ridge | 98,945.09 | -1,054.91 | -1.0549% | -1.3936% | 5.4848% | 6.3379% | 48 | 146.54 | 通过 |
| LambdaRank + 收益门槛 | 98,945.09 | -1,054.91 | -1.0549% | -1.3936% | 5.4848% | 6.3379% | 48 | 146.54 | 通过 |
| 冻结 RL 选币器 | 100,000.00 | +0.00 | +0.0000% | +0.0000% | 0.0000% | 0.0000% | 0 | 0.00 | 通过 |

### 连续账户的逐年收益

| 方案 | 2024（7–12 月） | 2025 | 2026（1 月至 10 月 3 日） |
|---|---:|---:|---:|
| 原规则 | -3.2389% | -1.1933% | +1.0265% |
| 相同资格的原规则 | -3.2389% | -1.0386% | +1.6740% |
| LightGBM | -2.8233% | -1.0386% | +1.6740% |
| Ridge | -2.9818% | +5.9787% | +1.2093% |
| LambdaRank + 收益门槛 | -3.0903% | +5.9787% | +1.2093% |
| 冻结 RL 选币器 | +2.8052% | +0.0000% | +0.0000% |

每年收益按上年最后账户净权益为分母，2024 首段以初始资金为分母；没有追加资金或重置连续账户。年化使用各区间实际日历天数和 365.25 日折算，属于短历史表现的数学折算，不是未来收益预期。净收益已包含全部实际执行成本；佣金单列用于解释费用，不再从净收益重复扣除。成交行数包括买卖和部分成交，不代表独立交易数量。

### RL 行为与结果边界

完整连续账户：收益 +2.8052%，最大回撤 2.5961%，平均敞口 0.7271%，实际成交 4 行，政策接受 2 个候选。已记录概率 361 条，概率范围 0.489229–0.510482，低于冻结门槛 359 条、达到门槛 2 条。期末实际待处理/活动订单数为 0/0；未成交订单不计为利润。

2026 独立账户：收益 +0.0000%，最大回撤 0.0000%，平均敞口 0.0000%，实际成交 0 行，政策接受 0 个候选。已记录概率 94 条，概率范围 0.489229–0.508700，低于冻结门槛 94 条、达到门槛 0 条。期末实际待处理/活动订单数为 0/0；未成交订单不计为利润。

各方案的持仓事件集中度和风险状态保留在 JSON 与逐账户证据中。现金账户的零收益、零回撤是合法执行结果，不证明模型具有盈利能力；历史收益为正也不证明独立泛化。该回测继续属于已观察历史的开发评价，完整历史 PIT 未验证，未打开 2026-10-06 开始的独立未来窗口，生产启用保持 false。

### 数据与可复核证据

保留原注册 60 币历史，给仍有真实报价的 55 币各追加 15 根日线，共 825 根。重叠 OHLCV 和真实成交额核对一致，原历史特征与资格逐项精确不变；新增数据的 478 行满足冻结输入资格。报价额直接使用交易所原始 kline 第 7 字段，原始回复、CSV、接收时间和文件身份分别保存。五个已终止资产保留原历史及退出标记，没有补造价格或成交。当前交易资格与观察到的历史不是完整历史成员 PIT。

准备阶段发现新旧可得时间序列化格式不同，修复前新增行全部误判不可用；该目录在任何回测前停止，并保存原协议、原产物和原驱动字节。随后在新目录统一时间表示、验证新增资格及所有原历史特征，才运行本次十二个实际账户。模型权重和收益门槛没有改变。

![两组账户净权益曲线](research/ml_selection_frozen_backtest_equity_20261005.png)

- [本次摘要](../reports/ml_selection_fresh_backtest_20261005/summary.json)
- [独立复核](../reports/ml_selection_fresh_backtest_20261005/independent_review.json)
- [模型与区间登记](../reports/ml_selection_fresh_backtest_20261005/run2/fixed_model_registration.json)
- [连续账户实际收据](../reports/ml_selection_fresh_backtest_20261005/windows/post_validation/receipt.json)
- [2026 独立账户实际收据](../reports/ml_selection_fresh_backtest_20261005/windows/year_2026/receipt.json)
- [注册行情及数据身份](../reports/ml_selection_fresh_backtest_20261005/input_bundle3/registration.json)
- [准备失败保留记录](../reports/ml_selection_fresh_backtest_20261005/preparation_failure_receipt.json)


---

<!-- 合并自 docs/research/ml_selection_next_diagnostics_20261005.md -->
## ML 选币本轮候选流、敞口与奖励诊断

本报告依据 `reports/ml_selection_next_full3_20261005` 的真实运行产物编写。冻结协议为 `ee4fb26d17e74a277047cdadd8bf6afb1f05dd3906e1b2e5bc1131c9366554f1`，覆盖登记的 60 个币。训练账户区间为 2020-01-01 至 2022-12-31，开发验证账户区间为 2023-01-01 至 2024-06-30。本轮属于回顾性研究，历史成员资格仍未完整证明。

低敞口首先来自原引擎的市场状态、策略信号、持仓、冷却和分配约束。验证原规则只有 51 个候选日期、21 个竞争日期；它们不能由完整特征表的行数替代。概率接近 0.5 的 RL 在不同门槛下确实能形成交易、少量交易或全现金账户，但“接受动作的概率”不是盈利概率。新回合的费用已进入原引擎净权益，本轮正式奖励为净权益的对数变化，没有另加重复费用或强制交易奖励。

本文件的候选流、原始训练诊断、退出/分配和学习证据来自 source3 已完成部分；该整轮后来在 WF1 部分成交处中断，不能称为完整成功运行。source4 修复账户浮点残余后重放主窗口十个 test、两组费用和三组执行情形，各项净收益、回撤、敞口、实际成交、奖励与账务值同 source3 完全一致；source4 又在 WF2 低参与率期末无法完成退出时中断。原失败记录和原数据均保留。

正式训练实际合计为 **124 次更新**：source3 主窗口三个种子 20/23/20，加 WF4 种子 43/44 各 20，再加 source4 WF4 种子 42 的 21；资源 pilot 的 18 次更新不计入正式预算。最终历史矩阵入口为 [full5 回执](../reports/ml_selection_next_full5_20261005/next_execution_receipt.json)，主窗口及 WF1 评价明确继承 source4，其余窗口和失败情形由 source5 完整汇总；不把复制结果或零更新恢复冒充新增训练。

最终证据汇总为 [source6 回执](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)，历史矩阵来源继续保持上述范围。source6 修正桥接在风险阻断时误改原本已拒绝记录的审计原因；固定政策的 144 个原引擎状态、完整审计和动作全部重放一致，与 source5 的逐日权益、成交和奖励文件字节完全相同。source5 原完整审计 136/144 一致的结果已封存，不回填为通过；source6 新训练、门槛搜索和新增财务候选均为 0。[桥接修正经济路径核对](../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)保留两版比较。

继承策略的固定验证在 source4 原引擎重放为 5/5 完全一致，仍不声称原训练已证明等价于修后重训；来源、路径解析和登记勘误见 [固定策略验证](../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)、[恢复勘误](../reports/ml_selection_next_full4_20261005/recovery_registration_erratum.json)、[执行与归档说明](ml_selection_history.md)。本文保留原始 source3 证据链接，所有窗口结果详见 [滚动与执行压力解读](ml_selection_history.md)。

**记录口径与证据。** 新运行显式启用完整研究审计，保留决策前现金、权益、已持仓币种、实际访问的门槛事实、原始策略信号、资本批准、仓位限制、风险预算和订单身份。训练原规则的 51,877 条 observation 全部具有 `gate_facts`，其中 380 条实际产生 `original_signal`；验证原规则的 31,952 条 observation 全部具有 `gate_facts`，其中 109 条产生原始候选。

这里的拒绝数来自 observation 顶层最终 `reason`，每条 observation 只计一次。内部 `gate_facts` 是访问过程的状态记录，不能简单按其中 `reason` 的文字累计失败。例如真实成功单的事实序列也有 `allocation_rejected` 标签，同时其资本批准数量为正；后面的 `zero_sizing` 标签也可以伴随正的 `sized_qty`。判断该阶段结果必须结合批准数量、资本分配原因、`allowed`、仓位限制和最终订单结果。

某币先被 `market_state_cash` 拦住时，本轮没有观察到它在下游策略、模型、分配和执行阶段会发生什么。以下是实际访问路径的描述，不能把未访问阶段的信号或盈利能力当作反事实结论。

证据：[训练原规则 summary](../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/summary.json)、[完整 observation](../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/entry_observations.json)、[训练诊断](../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/diagnostics.json)、[验证原规则 observation](../reports/ml_selection_next_full3_20261005/episodes/validation_native/entry_observations.json)。

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

证据：[全部监督验证比较](../reports/ml_selection_next_full3_20261005/validation_comparison.json)、[监督模型选择](../reports/ml_selection_next_full3_20261005/parent_model.json)、[Ridge 诊断](../reports/ml_selection_next_full3_20261005/episodes/validation_ridge/diagnostics.json)、[LambdaRank 诊断](../reports/ml_selection_next_full3_20261005/episodes/validation_lambdarank/diagnostics.json)。

**真实竞争与排序影响。** 原规则训练期有 163 个候选日期、73 个至少两个候选的竞争日期；验证期只有 51/21。相同候选批次的分配对照实际记录 12 批，11 批遇到资金或持仓容量限制，1 批改变了获批候选或数量。总体遇到 21 个竞争批次，但对照预登记上限为 12；其余批次不能据此推断。

2023-02-05 的真实账户状态下，固定原候选为 XTZ/USDT 与 BAT/USDT。原评分及固定随机评分分别批准 BAT 数量 13,284.015259；模型及动量评分分别批准 XTZ 数量 3,542.186156。当时已有七个币的持仓，被拒绝的候选显示 `position_slots_exhausted`。这说明排序在真实持仓容量竞争下确实可能改变入选，而非所有批次都只改一个无关的排序列表。

此对照复制当时原引擎的 allocator 对象图并调用原分配链，未执行撮合，各方案新增成交数均为零，`portfolio_return_difference` 明确为 null。因此 1/12 是“同批次审批或数量改变”的测量，不能写成 1/12 的盈利改善或选币收益提升。

证据：[同批次原引擎分配对照](../reports/ml_selection_next_full3_20261005/fixed_batch_allocation_comparison.json)。

**成交与退出的联接。** 训练原规则的 90 个开仓 decision 均有真实 order ID、开仓成交及退出联接；唯一开仓 fill ID 为 90 个，唯一退出 fill ID 为 420 个，两者并集恰好等于实际 fill ledger 的 510 行。验证原规则为 43 个开仓、89 个唯一退出 fill ID，并集等于 132 行实际成交。两个回合均没有未识别 decision、未联接实际成交或未联接开仓 order ID。

完整训练 decision ledger 仍有 51,787 行 `execution_linkage=unknown`，验证有 31,909 行；这些是没有开仓订单的路径记录，不能把它们写成同数量的丢失成交。`individual_equity_contribution` 保持未知：订单、lot 和退出的联接没有把组合净权益虚构分摊给每个候选。

证据：[训练 decision ledger](../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/decision_ledger.csv)、[训练 fill ledger](../reports/ml_selection_next_full3_20261005/episodes/training_native_diagnostic/fill_ledger.csv)、[验证 decision ledger](../reports/ml_selection_next_full3_20261005/episodes/validation_native/decision_ledger.csv)、[验证 fill ledger](../reports/ml_selection_next_full3_20261005/episodes/validation_native/fill_ledger.csv)。

**旧 77/22 的准确含义与新奖励。** V1 旧运行的 77 个已存回合全部完成权益、成本、奖励与终止对账，其中 22 个回合净收益为正但总奖励为负。77 和 22 都是回合数量，不是收益率。旧 seed 42 的训练回合 2，净权益对数收益为 0.3457220741，累计回撤加深惩罚为 0.5759756949，换手惩罚为零，奖励为 -0.2302536208。惩罚项确实改变了优化目标；对账最大逐项误差为零，不能把该现象直接归因为账务漏费或重复扣费。

旧 seed 42 训练回合 1 的净权益对数收益为 0.1187704382、回撤惩罚为 0.2354599787，总奖励 -0.1166895405。新 full3 main42 首回合保留同一净权益对数收益 0.1187704382，但正式预登记的回撤和换手系数都为零，奖励因此等于 0.1187704382。新 main42 首两回合的训练及三个门槛验证共八套实际账本已逐行核对 `reward=log(equity/previous_equity)`，最大误差为零、账务 discrepancy 均为零、无重复扣费。

这没有将手续费、滑点或融资从奖励里删除：它们已经进入原引擎的净权益；当前正式奖励没有再重复减去这些费用。独立 9-cell reward/resource pilot 在 0、0.25、0.5 三个回撤系数、三个种子下全部成功并提交 18 次更新，但不计入正式最低更新数，也未用于打开历史 test。

旧报告仍缺少上游原策略 observation 和完整 opening order 联接；旧历史 test 已被观察。旧 77 个回合只能按其真实已存账本解释，不能重建其全部门槛反事实，不能升级成独立样本，也不能覆盖成新的 V1 有效裁决。

证据：[V1 只读对账](../reports/ml_selection_next_full3_20261005/v1_diagnostic/audit.json)、[main42 首两回合语义核对](../reports/ml_selection_next_verification_20261005/main42_first_two_semantic_check.json)、[完整 reward/resource pilot](../reports/ml_selection_next_full3_20261005/pilot_resource_report.json)。

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

证据：[main44 完整训练历史](../reports/ml_selection_next_full3_20261005/rl_seeds/44/rl_training.json)、[0.49 验证 selection](../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_49/selection.csv)、[0.50 验证诊断](../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_5/diagnostics.json)、[0.51 验证诊断](../reports/ml_selection_next_full3_20261005/rl_seeds/44/episodes/rl_validation_015_threshold_0_51/diagnostics.json)、[独立执行原始资源及 SHA256 收据](../reports/ml_selection_next_verification_20261005/main_seed44_auxiliary_completion_receipt.json)。

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

证据：[候选冻结记录](../reports/ml_selection_next_full3_20261005/candidate.json)、[实际主窗口预算](../reports/ml_selection_next_full3_20261005/rl_budget_receipt.json)、[冻结 policy 及门槛](../reports/ml_selection_next_full3_20261005/models/policy_best.json)、[十个历史 test 比较](../reports/ml_selection_next_full3_20261005/test_comparison.json)、[冻结 RL test summary](../reports/ml_selection_next_full3_20261005/episodes/test_rl/summary.json)、[RL 概率/漏斗/对账](../reports/ml_selection_next_full3_20261005/episodes/test_rl/diagnostics.json)、[真实 RL 成交账本](../reports/ml_selection_next_full3_20261005/episodes/test_rl/fill_ledger.csv)、[费用压力](../reports/ml_selection_next_full3_20261005/cost_stress.json)、[执行压力](../reports/ml_selection_next_full3_20261005/execution_stress.json)、[实际最终裁决](../reports/ml_selection_next_full3_20261005/adjudication.json)。

即使后续历史 test、压力和所有正式更新成功完成，本轮仍保留回顾性身份、历史成员资格证据限制，以及至少 30 个真实前向 decision 日期等未满足条件。未来样本不由本轮历史重放或多个种子提供。最终验收还须以实际总执行收据及 [运行恢复指南](ml_selection_history.md) 记录的能力边界为准。


---

<!-- 合并自 docs/research/ml_selection_next_robustness_20261005.md -->
## ML 选币本轮滚动窗口与执行压力解读

本文以 `reports/ml_selection_next_full6_20261005` 为当前最终汇总入口，冻结协议为 `b79dc942e38c70c40c00fa6f8bedcdeb63511cb05051899d972cd9f6896d6eb6`。下文历史矩阵及原证据保留在 source5（协议 `6f41249b58f54e3ed2719637b003d59dbd509a599231d2fbc64163e309db05df`）：主窗口的十个账户、费用和执行压力，以及 WF1 实际来源于 source4；WF2/WF3/WF4 的登记尝试实际来源于 source5。source6 继承完整历史矩阵，只修复并重放固定 RL 的桥接审计，144/144 个动作与完整 audit 一致；新增训练 0、新门槛搜索 0，财务候选、权重、门槛与经济路径不变。source3/source4 的中断、source5 的原桥接不一致、WF2/WF3 低参与率真实失败及未知最终指标均保留，完整汇总不能写成全部压力通过或全部 source6 新评估。

这里分开回答四个问题：账户是否赚钱、风险是否在限制内、账务是否能对上、证据是否足以支持有效性和准入。正收益、较低回撤、账务通过可以分别成立，而最终准入仍不成立。历史 test 已进入开发材料，本轮保持回顾性研究身份，不把重放窗口、随机种子或参数更新视作新的独立最终样本。

**source3/source4 中断与 source5 恢复的科学口径。** source3 协议为 `ee4fb26d17e74a277047cdadd8bf6afb1f05dd3906e1b2e5bc1131c9366554f1`，其 run 保留 `complete=false`、`all_minimum_budgets_met=false`。它在 WF1 的 `low_participation` 情形因 `protective_position_identity_missing:UNI/USDT` 中断：权威 lot 账本已经没有 open lot，但汇总数量还剩 `4.263256414560601e-14`。不能把这次中断改写为 source3 已完成四窗口。source4 修复协议为 `9f9b3f8f18397b7a6219483b293895e5a484b5f1dcf857f4680d3e90f5ef51b9`。

source4 唯一变更的运行源码为 `core/portfolio.py`：只在权威 lot 账本为空、汇总残余不大于已有 lot quantity epsilon 时，把投影持仓归为精确零；真实成交现金不变。修复的定向重放真实经过 2022-08-26 UNI/USDT 的残余归零，WF1 低参与率账户账务通过，84 项针对性检查通过。该修复验证产生 0 次训练更新，未写回父 run 产物。

source4 的第二次中断保留为执行失败事实：WF2 `low_participation` 在窗口尾部关闭持仓时抛出 `End-window exit cannot fill within actual liquidity`。低参与率下，真实流动性不足以完成终端退出是合法的压力边界。该情形没有已完成终端账户，最终净收益、最终 reward 和最终 accounting 均未知；不能用零收益、最后已落盘的中途 equity 或假定成交补齐。WF2 已落盘的普通 test 与费用压力不代表其全部压力已完成；source4 的完整 `walk_forward.json` 只有一个已完成 item（WF1），最终四窗口收据不存在。

source5 只变更 `research/ml_selection/pipeline.py` 的研究汇总路径：将上述精确预期的流动性退出异常记录为失败的执行证据，继续其余登记压力；其他异常仍传播，原引擎成交与终端策略不变。source5 来源登记明确继承 source4 的 `main_validation_test_cost_execution` 和 `walk_forward_window_01` 评估范围；WF2/WF3/WF4 与 RL state bridge 是 source5 的继续评估范围。六个训练 cell 全部继承，source3 更新 103、source4 更新 21、source5 新训练更新 0，`training_equivalence_proven=false`。完整 matrix 指全部登记尝试及失败得到报告，不等于每个压力情形都成功。

source5 已实际把 WF2 的同一压力边界记录为 `failed_end_window_liquidity`、`execution_completed=false`。其原引擎部分证据有 95 行已记录成交：期末 AVAX/USDT 卖单请求数量 `73.62268620310697`，只完成 `22.081817700000002`，剩余 `51.540868503106964`，与真实 open lot 数量一致，`terminal_contract_satisfied=false`。这是真实未平持仓，不是 source3 的极小数值残余。失败时现金 `100996.77171029814` 只是部分账户状态，不能作为最终净值；最终净收益、回撤、reward、accounting 及是否风险终止均保存为 `null`。source5 在记录失败后实际继续完成 WF2 的开仓延迟与现金预算限制情形，不能将“继续汇总成功”写成“失败压力已成功退出”。

source4 导入主窗口 42/43/44 和 WF4 43/44 已在 source3 完成的 103 次参数更新与固定权重；这些更新的训练引擎、最佳 checkpoint 和门槛选择、继承训练/验证指标来源仍是 source3。source4 重新执行账户评估，WF4 seed42 是 source4 的新训练 cell。注册勘误明确撤回“成功完成即能证明没有经过相关路径”的绝对说法：最后一根 bar 的保护成交可能发生在当根身份核对之后，成功完成本身不能证明旧训练没有受该路径影响。因此 `training_equivalence_status=not_proven`，不能把 103 次导入更新称为 source4 重训或宣称重训位级等价。

source4 的五个继承摘要中的相对 `artifact_directory` 按映射解析到封存的 source3 原目录：cell 摘要使用 `OLD/cell_relative/summary.artifact_directory`；根学习曲线使用 `OLD/summary.artifact_directory`；根 winner history 的 artifact root 为 `OLD/rl_seeds/43`。解析报告保存 `all_recorded_summary_paths_resolved_and_hash_verified=true`，保留原始字节和 hash。source5 则逐 cell 使用 `recovery_provenance.inherited_cells.source_artifact_root / summary.artifact_directory`，其中五个 root 指向 source3，WF4 seed42 指向 source4；学习曲线、pilot 和原诊断的来源仍为 source3。不能误链接到后续 run 的空训练回合目录。

证据：[source3 中断收据](../reports/ml_selection_next_full3_20261005/interruption_receipt.json)、[保护持仓归零修复验证](../reports/ml_selection_next_full3_verification_20261005/protective_flat_fix_verification.json)、[source4 冻结协议](../reports/ml_selection_next_full4_20261005/protocol.json)、[source4 中断及完整训练收据](../reports/ml_selection_next_full4_20261005/interruption_receipt.json)、[source4 恢复来源](../reports/ml_selection_next_full4_20261005/recovery_provenance.json)、[注册勘误](../reports/ml_selection_next_full4_20261005/recovery_registration_erratum.json)、[继承回合解析与 hash 核对](../reports/ml_selection_next_full4_20261005/inheritance_episode_resolution.json)、[source5 冻结协议](../reports/ml_selection_next_full5_20261005/protocol.json)、[source5 混合来源登记](../reports/ml_selection_next_full5_20261005/recovery_provenance.json)、[WF2 低参与率失败摘要](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/summary.json)、[WF2 原引擎未完成账户证据](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)。

**固定策略迁移验证。** 五个导入 best policy 各使用 source3 已选门槛，在 source4 下重放一次开发验证账户。以下 5/5 的每日 equity 形状、索引、列及数值精确相同，最大绝对差为 0，确定性 episode digest 相同，scalar differences 均为空。该补充验证产生 0 次新训练更新、搜索 0 个新门槛，`changed_original_selection=false`；它没有重新寻找 winner，也未使用 test 选择。

| 导入 cell | 原最佳回合 | 原冻结门槛 | 每日 equity 行数（旧 / 新） | 固定验证精确相同 | 训练等价证明 |
| --- | ---: | ---: | ---: | --- | --- |
| main42 | 13 | 0.50 | 547 / 547 | 是 | 未证明 |
| main43 | 20 | 0.51 | 547 / 547 | 是 | 未证明 |
| main44 | 15 | 0.49 | 547 / 547 | 是 | 未证明 |
| WF4 seed43 | 1 | 0.51 | 181 / 181 | 是 | 未证明 |
| WF4 seed44 | 1 | 0.51 | 181 / 181 | 是 | 未证明 |

固定验证的精确相同支持这五份已选权重与门槛的 source4 迁移；它没有检查所有旧训练轨迹在 source4 下的参数更新，也不改变 `training_equivalence_proven=false`。

证据：[五份固定策略验证报告](../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)。

**账户与窗口口径。** 每个 arm、每次训练/验证回合及每个滚动窗口都重新建立原引擎账户；窗口协议保存 `account_initialization=separate_fresh_account_no_equity_stitching`。持仓、现金、订单、退出、健康及风险控制路径各自演化。主窗口和不同窗口的净收益不能直接相加，四个窗口也不是一条接续投资曲线。

主窗口 test 为 2024-07-01 至 2026-09-18。滚动窗口登记边界为：

| 窗口 | 训练截止边界（不含） | 验证区间 | test 区间 | 登记 RL |
| --- | --- | --- | --- | --- |
| WF1 | 2022-01-01 | 2022-01-01 至 2022-06-30 | 2022-07-01 至 2022-12-31 | 否 |
| WF2 | 2023-01-01 | 2023-01-01 至 2023-06-30 | 2023-07-01 至 2023-12-31 | 否 |
| WF3 | 2024-01-01 | 2024-01-01 至 2024-06-30 | 2024-07-01 至 2024-12-31 | 否 |
| WF4 | 2025-01-01 | 2025-01-01 至 2025-06-30 | 2025-07-01 至 2025-12-31 | 三种子，分别至少 20 次实际更新 |

前三个窗口各登记九个完整 arm：native、qualified_native、momentum、LightGBM、Ridge、LambdaRank 和 random 42/43/44。WF4 加入 RL，登记十个 arm。每个窗口在其开发验证账户上选择并冻结自己的候选，再运行其 test 和压力；窗口候选不替换主窗口已冻结模型。即使验证资格未过、test 为零或负、账户全现金，窗口仍须完整报告，不能从稳健性统计中删掉。

证据：[最终冻结协议](../reports/ml_selection_next_full5_20261005/protocol.json)、[完整四窗口汇总](../reports/ml_selection_next_full5_20261005/walk_forward.json)、[source4 已完成 WF1 汇总](../reports/ml_selection_next_full4_20261005/walk_forward.json)。WF1 来源于 source4，WF2/WF3/WF4 来源于 source5。

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

证据：[主窗口候选](../reports/ml_selection_next_full5_20261005/candidate.json)、[继承 source4 的主窗口 test](../reports/ml_selection_next_full5_20261005/test_comparison.json)、[继承 source4 的费用压力](../reports/ml_selection_next_full5_20261005/cost_stress.json)、[继承 source4 的执行压力](../reports/ml_selection_next_full5_20261005/execution_stress.json)、[继承 source4 的主窗口裁决](../reports/ml_selection_next_full5_20261005/adjudication.json)、[候选流与奖励诊断](ml_selection_history.md)。

**主窗口随机种子差异。** 三个种子的最佳 checkpoint 和门槛均由 source3 开发验证选择；以下指标保留 source3 来源，source4 的五份固定策略报告已核实其选定验证账户精确一致。表格不能解释成 source4 三次重训，也不能解释成三个独立历史 test。

| 种子 | 最佳 checkpoint 回合 / 门槛 | 完成的实际更新 | 验证净收益 | 验证奖励 | 验证最大回撤 | 平均总敞口 | selector 接受 / 成交行数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 42 | 13 / 0.50 | 20 | 0.0403% | 0.0004028174 | 2.6105% | 1.4846% | 2 / 4 |
| 43 | 20 / 0.51 | 23 | 5.5041% | 0.0535797875 | 5.0123% | 2.0560% | 4 / 32 |
| 44 | 15 / 0.49 | 20 | 2.9679% | 0.0292470112 | 6.5577% | 5.3505% | 125 / 149 |

三个最佳验证账户都为正、账务通过且未因风险终止，但收益幅度、门槛、接受数和敞口差异很大。main42 接近零收益，main44 接受更多候选而回撤也更大。重复随机训练改变策略和账户路径，没有提供三个新市场；不能仅用“3/3 验证为正”证明种子稳健性。多个 checkpoint、种子和门槛之间的最佳值比较也没有消除验证选择的乐观偏差。

证据：[原主窗口三种子比较](../reports/ml_selection_next_full3_20261005/rl_seed_comparison.json)、[source4 保留的三种子比较](../reports/ml_selection_next_full4_20261005/rl_seed_comparison.json)、[固定迁移验证](../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)。

**WF4 三种子的实际开发验证差异。** 这三个 cell 的训练均已完成，以下 best checkpoint 只使用各自开发验证选择；seed42 来源为 source4，seed43/44 来源为 source3。source4 的五份固定策略迁移验证覆盖两个导入 WF4 best policy，不能把它们记作 source4 重训。

| WF4 种子 / 训练来源 | 最佳 checkpoint 回合 / 门槛 | 实际更新 | 验证净收益 | 验证奖励 | 验证最大回撤 | 平均总敞口 | 实际 selected_count / 成交行数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 42 / source4 | 18 / 0.49 | 21 | 0.5042% | 0.0050288539 | 4.4708% | 3.6658% | 3 / 6 |
| 43 / source3 | 1 / 0.51 | 20 | 0.0000% | 0.0000000000 | 0.0000% | 0.0000% | 0 / 0 |
| 44 / source3 | 1 / 0.51 | 20 | 0.0000% | 0.0000000000 | 0.0000% | 0.0000% | 0 / 0 |

三份 best 验证账户账务通过、没有风险终止，但只有 seed42 为正收益；seed43/44 是合法现金账户，其零收益和零风险不能计为正盈利效力。全部实际更新均属于各协议的重复历史训练，种子差异没有形成三个独立市场样本。这张开发验证表也不代替 WF4 的最终 test、费用和执行压力。

证据：[WF4 seed42 source4 原训练记录](../reports/ml_selection_next_full4_20261005/walk_forward/window_04/rl_seeds/42/rl_training.json)、[WF4 seed42 source4 完成收据](../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_completion.json)、[WF4 seed43 source3 原训练记录](../reports/ml_selection_next_full3_20261005/walk_forward/window_04/rl_seeds/43/rl_training.json)、[WF4 seed44 source3 原训练记录](../reports/ml_selection_next_full3_20261005/walk_forward/window_04/rl_seeds/44/rl_training.json)。

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

证据：[完整四窗口与全部 arm](../reports/ml_selection_next_full5_20261005/walk_forward.json)、[WF1 候选与资格](../reports/ml_selection_next_full5_20261005/walk_forward/window_01/candidate.json)、[WF2 候选与资格](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/candidate.json)、[WF3 候选与资格](../reports/ml_selection_next_full5_20261005/walk_forward/window_03/candidate.json)、[WF4 候选与资格](../reports/ml_selection_next_full5_20261005/walk_forward/window_04/candidate.json)。

费用与执行证据：[WF1 费用](../reports/ml_selection_next_full5_20261005/walk_forward/window_01/cost_stress.json) / [执行](../reports/ml_selection_next_full5_20261005/walk_forward/window_01/execution_stress.json) / [裁决](../reports/ml_selection_next_full5_20261005/walk_forward/window_01/adjudication.json)；[WF2 费用](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/cost_stress.json) / [执行](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/execution_stress.json) / [裁决](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/adjudication.json)；[WF3 费用](../reports/ml_selection_next_full5_20261005/walk_forward/window_03/cost_stress.json) / [执行](../reports/ml_selection_next_full5_20261005/walk_forward/window_03/execution_stress.json) / [裁决](../reports/ml_selection_next_full5_20261005/walk_forward/window_03/adjudication.json)；[WF4 费用](../reports/ml_selection_next_full5_20261005/walk_forward/window_04/cost_stress.json) / [执行](../reports/ml_selection_next_full5_20261005/walk_forward/window_04/execution_stress.json) / [裁决](../reports/ml_selection_next_full5_20261005/walk_forward/window_04/adjudication.json)。

未完成账户证据：[WF2 低参与率原状态](../reports/ml_selection_next_full5_20261005/walk_forward/window_02/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)、[WF3 低参与率原状态](../reports/ml_selection_next_full5_20261005/walk_forward/window_03/episodes/execution_stress_lightgbm_low_participation/partial_evidence.json)。

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

证据：[source5 最终执行收据](../reports/ml_selection_next_full5_20261005/next_execution_receipt.json)、[source3 中断时的五 cell 预算](../reports/ml_selection_next_full3_20261005/interruption_receipt.json)、[source4 六 cell 完整训练预算](../reports/ml_selection_next_full4_20261005/interruption_receipt.json)、[WF4 seed42 source4 完成收据](../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_completion.json)、[WF4 seed42 原预算](../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_original_rl_budget_receipt.json)、[WF4 seed42 原资源](../reports/ml_selection_next_full4_verification_20261005/wf4_seed42_original_rl_resources.json)、[main44 原辅助完成资源](../reports/ml_selection_next_verification_20261005/main_seed44_auxiliary_completion_receipt.json)、[main44 原预算](../reports/ml_selection_next_verification_20261005/main_seed44_rl_budget_receipt.json)、[main44 原资源](../reports/ml_selection_next_verification_20261005/main_seed44_rl_resources.json)、[WF4 两 helper 完成汇总](../reports/ml_selection_next_verification_20261005/wf4_auxiliary_completion.json)、[WF4 seed43 原预训练收据](../reports/ml_selection_next_verification_20261005/wf4_seed43_pretrain_receipt.json)、[WF4 seed44 原预训练收据](../reports/ml_selection_next_verification_20261005/wf4_pretrain_receipt.json)、[WF4 seed43 原预算](../reports/ml_selection_next_verification_20261005/wf4_seed43_rl_budget_receipt.json)、[WF4 seed43 原资源](../reports/ml_selection_next_verification_20261005/wf4_seed43_rl_resources.json)、[WF4 seed44 原预算](../reports/ml_selection_next_verification_20261005/wf4_seed44_rl_budget_receipt.json)、[WF4 seed44 原资源](../reports/ml_selection_next_verification_20261005/wf4_seed44_rl_resources.json)。

**主窗口与最终准入裁决。** 主窗口收益层：净收益为正、超过原规则、费用 1.5/2.0 倍仍为正。风险层：回撤在登记限制内，主账户和执行压力没有风险终止。会计层：主账户与所有压力检查通过。证据层：历史 PIT、独立最终样本、独立事件组和前向成熟度仍未满足。四窗口的分开裁决见上表，不能由主窗口的正收益覆盖。

主窗口裁决的集中度使用固定 5 日开仓块，只有两个 cohort，`status=insufficient`，未证明独立性；移除最高五/十组后剩余净 PnL 都为零，`positive_after_top_5=null`。因此 `retrospective_checks_passed=false`，不能写成完整稳健性门槛通过。后续窗口的 `event_concentration` 使用实际持仓区间连通重叠分组，与固定 5 日块不同；即使事件数足够，该计算仍保存 `independent_events_proven=false`，不自动证明事件独立。

现金账户的零收益与零风险属于合法执行结果；它们可以通过会计检查，不能算作正盈利效力。`insufficient` 集中度或验证资格未通过也必须保留，不能改写为成功窗口。

source5 的原固定策略历史桥接报告继续保存 `all_decisions_identical=false`：144/144 个快照的 selected-ID 比较一致，136/144 个完整 decision-facts / audit 比较一致，`verification_errors=[]`。source6 只修复 `research/ml_selection/forward_bridge.py` 的风险审计装饰：已被策略拒绝的行保留原审计事实，只对原选中后被风险预算撤销的行添加风险拒绝字段。真实 source6 原引擎重放已取得 144/144 个动作与完整 audit 一致、`all_decisions_identical=true`、0 验证错误，不覆盖 source5 原报告。

source6 桥接与 source5 的 equity、trades、rewards、fill_ledger、closed_positions 五份经济文件 SHA 精确相同；净收益、最大回撤、敞口、换手、reward、fill_count、accounting 七个标量相同，`all_exact=true`。新训练更新和新门槛搜索均为 0，`new_financial_candidate=false`，训练等价仍未证明。这证明此次固定策略桥接审计修复的经济路径相同，没有重评主窗口和四个历史窗口，也不提供独立市场样本；该历史重放仍保存 `forward_evidence=false`。

真实前向证据当前有 1 条 recorded observation，但 0 个核验通过的前向 decision 日期、0 个成熟 proxy 标签，模拟账户收益仍为 `null`。登记开始为 2026-10-06，协议与候选在开始前冻结；实际状态仍为 `pending_forward_evidence`，缺少冻结 RL 的真实账户和策略状态，决策桥接不能提供原引擎 fills/equity。独立最终样本登记为 `preregistered_future_sample_not_opened`，没有打开。

最终执行收据保存 `formal_admission=false`、`production_enabled=false`、`historical_pit_universe_verified=false`。至少 30 个真实前向 decision 日期、成熟标签与未打开的独立最终样本仍需将来真实证据，历史回放、多个种子或最低更新数不能替代这些条件。

证据：[source5 原桥接不一致报告](../reports/ml_selection_next_full5_20261005/rl_bridge_verification.json)、[source6 实际完整桥接](../reports/ml_selection_next_full6_20261005/rl_bridge_verification.json)、[source6 桥接经济路径精确对照](../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)、[source6 混合来源登记](../reports/ml_selection_next_full6_20261005/recovery_provenance.json)、[当前前向证据](../reports/ml_selection_next_full6_20261005/forward_evidence.json)、[当前最终执行收据与准入身份](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)。
