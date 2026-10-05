# ML 选币工程整改：先完成代码，暂停训练

本批基于 `main` 的 `373cb6ae11c2ce11a38bfa59be6ea94b4d7d8fef`（PR #50）。执行范围为前次审查的工程缺陷、研究接口和验证工具。没有启动模型拟合、RL 更新、阈值搜索、行情下载或历史研究账户重跑。冻结权重和默认门槛保留作为后续对照。

## 问题与代码交付

| 阶段 / 问题 | 本批代码交付 | 后续需要的真实证据 |
|---|---|---|
| R0 / 近乎全拒绝、低活动难解释 | 候选、数据资格、实际评分、有效 gate 与拒绝原因的漏斗；概率明确为动作概率；端到端及准备 / 引擎 / 报告阶段计时 | 原 60 币输入与逐账户账本，同配置经济复验 |
| R0 / 原输入包缺失 | 只读 readiness 审计登记、文件与模型身份、训练 / 标签 / 行情水位、账户、PIT 和 final 状态 | 恢复登记绑定的原始 CSV、protocol 与账本；缺失保持 pending |
| R1 / V3 静默绕过 selector | CLI、网页准入、Engine 与共享 EventProcessor 拒绝未支持的组合 | 将来实现 V3 候选与目标权重桥接后再开放 |
| R1 / 多次上市接口不一致 | membership 审计与 dataset 共用非重叠区间和信息可得时间判定 | 完整、可核验的历史成员资料 |
| R1 / 数据来源和版本不清楚 | UTC 水位、原 frame hash、真实报价额 / 代理来源、可选严格资格要求 | 新数据版本与来源，不能覆盖旧登记 |
| R1 / 推理重复准备 | feature-only builder，BTC benchmark 特征单次构建；不计算未来标签 | 真实全量运行端到端性能测量 |
| R2 / 过滤、排序与资金权重混在一起 | 四项 selector contract，排名分数与资金 score 分离；只生成冻结消融计划或比较已有配对账本 | 同数据、账户、费用、退出与风控的独立账户重放 |
| R3 / 全币日代理标签偏离策略域 | 从权威 decision / order / fill / lot-close 构建策略候选数据；实际退出、代理、配对边际目标分开；未知与同日 cohort 保留 | 真实成熟候选、融资成本归属、足够完整竞争组 |
| R3 / 账户迁移、训练水位旧 | 显式训练 / 部署账户契约，近期滚动 train_start，独立早停 / 校准 / 开发检验范围，试验预算与样本不足记录 | 数据恢复后使用新协议；本批不训练 |
| R4 / RL gate 与奖励语义不清楚 | legacy policy-only 默认与 opt-in gate 交集分开；采样动作、实际有效 gate、账户奖励归属分开记录；可选策略 / 风险 / 竞争上下文 | 训练后验证增量价值和信用分配方案，不能把账户奖励当单候选利润 |
| R5 / 漂移、零成交和准入边界 | 只读缺失率、陈旧水位、候选覆盖、概率分布、拒绝原因与实际账户零成交日监测；复用现有前向证据合同 | 真实前向观察、标签成熟、独立模拟成交账户与执行压力 |

代码支持并不证明收益改善。模型效力、数据补齐和正式放行仍须对应证据；`formal_admission` 保持 false。终端流动性失败、未平库存或缺账本的最终收益保持未知。

## 无训练的验证入口

从仓库根目录执行，使用已安装正式 / 开发 / 可选 ML 依赖的 Python：

```bash
python scripts/check_ml_code.py --list-tests
python scripts/check_ml_code.py --pytest-arg=-q
```

这是显式审核过的工程测试清单，涵盖纯数据、模拟训练编排、原引擎与冻结模型推理。guard 在收集测试前阻断真实 fit、scaler 拟合、RL update、LightGBM 和 sklearn 训练调用；意外调用会使测试失败，不会跳过断言。它只保护当前测试进程，不是任意子进程的沙箱。

完整 `tests/test_ml_selection_*.py` 包含真实合成模型训练，暂停训练期间不要使用该通配入口。模型训练正确性的完整验收需留待用户重新授权训练。

## 只读检查与消融计划

```bash
python scripts/audit_ml_readiness.py --account-mode spot_margin \
  --output /tmp/ml-readiness.json
python scripts/plan_selector_comparison.py --account-mode spot_margin \
  --output /tmp/ml-comparison-plan.json
```

两条命令不训练、不下载、不运行回测。没有原始登记时照样给出模型身份和明确的 pending 清单。恢复原协议后用 `--protocol` 提供 hash anchor；消融计划可用 `--controls` 提供完整账户条件。已有完整账户回执可通过 `--left-ledger` / `--right-ledger` 比较；合同、完整日历、会计或终端执行不匹配时净收益差为 null。

原条件对照依次包括 off、仅资格、仅排名、仅收益 gate、仅 RL gate、组合，以及动量 / 随机排名基线。排名方案另固定 original_score / selector_score 资金来源。`selector_enabled`、构造参数和四个布尔 flags 在计划中独立记录，后续执行不得将计划当已运行结果。

漂移审计可增加 `--reference`、`--observations`、`--activity`。前两者是已有特征记录，activity 是独立实际账户的逐日成交回执。selector 全拒绝与账户零成交分别统计；缺 activity 不能推断零成交。`--forward-store` 只验证已有前向证据。

## 候选数据与新协议

```bash
python scripts/build_selector_candidates.py \
  --run-dir reports/EXISTING_FROZEN_RUN --episode episodes/test_native \
  --labels-as-of 2026-10-04T00:00:00Z --target-type actual_exit \
  --output /tmp/NEW_CANDIDATE_ARTIFACT
```

源实验需要已有 protocol、dataset、decision、fill 与 close-event 账本。输出必须是新目录且位于源实验外。工具不以 symbol / 时间猜测订单联接，也不强制平仓补标签。实际成交目标是观察到的策略账户退出收益，存在成交选择条件；不是拒绝候选的反事实，也不是单候选组合边际贡献。非现货账户缺融资成本归属时目标未知。

候选数据通过 `load_candidate_training_rows()` 校验 contract、CSV、原协议与源账本哈希，只接受完整成熟的账户日组。没有完整组会明确报数据不足。新协议可登记下面的数据入口；拟合与早停读候选域，推理和账户评价仍使用独立的完整因果行情表，避免候选表缺失未来日期或重复策略行污染服务：

```yaml
training_data:
  candidate_dataset_directory: /ABSOLUTE/PATH/TO/CANDIDATE_ARTIFACT
  target_type: actual_exit
  data_identity: EXACT_VALUE_FROM_CONTRACT
```

冻结协议记录该外部产物的身份，恢复时再次校验。实际退出样本仍有成交选择条件，不因换了数据入口就成为独立结果或边际组合目标。

新实验可显式登记 `splits.train_start`、`splits.calibration_end` 和 `evaluation_protocol`：

```yaml
account_mode: spot_margin
splits:
  train_start: "2021-01-01"
  train_end: "2023-01-01"
  validation_end: "2024-01-01"
  calibration_end: "2024-07-01"
evaluation_protocol:
  deployment_account_modes: [spot_margin]
  maximum_validation_trials: 1000
  maximum_calibration_trials: 100
  minimum_event_groups: 6
```

这是现有注册期内的代码配置示例，不是建议训练窗口或统计样本门槛。日期字段应使用字符串；所有滚动窗口也必须登记校准区间，试验预算须覆盖已登记种子 / checkpoint / 窗口 / 阈值尝试。先在早停验证区选 seed / checkpoint，再仅对冻结 winner 校准，开发检验不参与选择。旧协议会明确报告 validation 复用，不能自动改写为独立证据。

新 final 必须在真实开始之前预登记，位于所有开发数据之后。已有单候选、未打开的 final 合同保持其身份；新候选不能借用已开始的历史窗口。以上任务完成后，下一步是恢复和登记数据、执行公平对照，再决定是否训练；本批不执行这些步骤。

新 policy 可通过 `rl.context_features` 登记 `native_candidate_score`、`stop_distance_fraction`、`batch_candidate_count`、`health_risk_multiplier`、`market_risk_multiplier`、`portfolio_risk_multiplier`、`requested_notional_fraction`、`candidate_is_short` 的所需子集。值来自 selector 调用时的真实候选和运行状态，缺失会拒绝该候选并说明未知项；不会读取之后的 allocation 审计。上下文的初始缩放是明确记录的零参考 / 单位尺度，不宣称任意币日有这些历史策略状态。冻结旧 policy 的 21 个行情与 5 个账户输入保持原顺序。

`selection.policy_gate_mode` 可显式设置 `policy_only` 或 `policy_and_return`，新政策元数据随模型冻结。修改 gate 或阈值属于新研究配置，需要完整账户评价；本批没有通过调整门槛来制造更高成交数。前向 bridge 捕获和校验决策时上下文，使新输入可以逐候选回放。

## 本批验收

- 显式无训练 guard 清单：20 个模块、400 项通过，无失败或跳过；真实模型 / scaler 拟合与 RL 更新被阻断。
- 原引擎、共享运行时、网页任务及 V3 等运行时回归：171 项通过。
- Ruff、CI 必需的 3 文件 Mypy、另 8 个新增 / 修改核心源文件 Mypy、数据与 loader 4 文件 Mypy、依赖锁、49 份受保护历史文件、仓库保留规则、Roadmap 结构、diff whitespace 校验通过。
- 数据对照：1,439 行旧 / 新 21 个特征、资格与代理标签精确一致；冻结模型合成原引擎对照的权益和交易账本一致。该场景为 6 个 selector 决策、零成交，覆盖范围有限，不是实际收益改善证据。
- 真实冻结包 readiness CLI：两个模型文件 hash 与内部身份通过，`failed_reasons=[]`，状态为 `pending_external_evidence`。12 个消融 arm 均为 `planned_not_executed`。

扩大检查既有 Engine 和研究模块的 Mypy 仍有与 `373cb6a` 原始快照相同的旧错误；它们不在 CI 必需范围，本批没有声明这些模块全量类型检查通过。暂停训练期间也没有执行含真正拟合的完整 ML 测试。

机器回执、Junit 与只读 CLI 报告保存在本次云环境的 `/workspace/scratch/quant-ml-code-verification/`，不作为历史金融研究产物提交。环境配置草稿已保存可选 CPU ML 依赖与默认无训练启动检查，尚未发布。
