# QuantTrading 文档导航

本文档目录区分“当前有效文档”和“历史审计资料”。开发、验收和运行决策应以当前有效文档为准；归档文件只用于追溯旧任务编号、历史判断和迁移来源。

## 从哪里开始

按当前任务选择入口，无需先读完全部历史材料。

| 阅读目的 | 首选文档 | 补充参考 |
| --- | --- | --- |
| 首次安装、回测或打开网页 | [项目 README](../README.md)、[本地研究工作台](../dashboard/README.md) | [依赖与环境](dependency_management.md)、[前端维护](../dashboard/FRONTEND.md) |
| 理解代码职责和调用关系 | [代码模块说明](modules/README.md) | [架构边界审查](architecture_review.md)、[词汇表](glossary.md) |
| 查看阶段、依赖与放行条件 | [统一 Roadmap v3](unified_roadmap.md) | [统一开发计划](development_plan.md)、[结构化任务登记](development_task_registry.json) |
| 实施任务、核对交付和验收 | [开发详情](development_details.md) | [2026-09-20 后续实施记录](followup_completion_20260920.md) |
| 核对回测假设和指标公式 | [当前回测行为](backtest_assumptions.md) | [指标公式与专项标准](backtest_metrics_detailed_development_plan.md) |
| 查找研究、采集和验证入口 | [脚本索引](../scripts/README.md) | 下方[最新行为变更](#最新行为变更)中的专项契约与实施记录 |
| 部署、排障和周期性运行 | [部署与运维](deployment.md)、[账户事实操作说明](account_fact_operations.md) | [自动化回测操作方案](automated_backtest_plan.md) |
| 整理工程结构与生成物 | [工程结构优化路线图](engineering_structure_roadmap.md) | [文件保留与生成物规则](file_retention.md) |
| 追溯旧任务和历史结果 | [历史需求与邮件追溯](roadmap_traceability.md)、[固定历史基线](baselines/batch0_fixed_baseline.md) | [2026-09-20 审计](codex_mail_roadmap_audit_20260920.md)、[原文与邮件归档](archive/2026-09-roadmap-rebaseline/README.md) |

## 权威顺序

用户明确要求和已批准契约 → 统一Roadmap（阶段与门槛） → 开发计划/登记表（排期与状态） → 开发详情（任务契约与验收） → 领域公式/行为参考 → 历史归档。

项目阶段只在Roadmap维护；任务状态在开发计划与结构化登记同步维护；开发详情保留基线及技术要求，关闭时追加验收索引。原实盘、策略、仓位和回测优化计划保留为领域参考，其旧排期与完成标记不再独立维护。已批准的研究、风控、止损、健康等契约继续有效，冲突处理见新Roadmap。

当前行为文档不把计划中的能力写成已实现。工程验收、研究结论、连续运行和真实资金准入分别记录。
工程结构优化路线图是实施建议与检查清单，不改写上述 Roadmap 阶段或任务登记状态。

## 最新行为变更

以下按近期主题提供入口。带日期的实施记录说明对应批次的工作和证据，文件日期较新并不自动替代已批准契约或更新项目放行状态。

- [2026-10-05 机器学习选币 V1 实施总结](ml_selection_v1_review_20261005.md)：CPU 评分与完整回合 REINFORCE、原引擎可选接口、本地历史训练结果、数据包前提与有效性未通过的边界；操作入口见[训练指南](ml_selection_roadmap.md)。
- [2026-10-04 剩余论文任务](research/paper_remaining_tasks_20261004.md)：执行成本与盘口证据、历史因子输入、前瞻采集和严格标签的实现与缺口；[收益改进复核](research/paper_return_followup_20261004.md)记录固定方案的历史比较，保留主引擎与独立现货账户的口径差异。
- [论文统计验证契约](research/paper_validation_contract_20261003.md)：PBO、purged/embargo 切分、White Reality Check 与 DSR 的输入、计算和证据不足行为；应用入口见[论文应用清单](research/paper_application_list_20261003.md)和[工程完成记录](research/paper_engineering_completion_20261003.md)。
- [论文路线图实施](research/paper_roadmap_implementation_20261003.md)、[续办记录](research/paper_continuation_20261003.md)：因果标签、数据版本、融资与账户关联的实施边界及产物索引。
- [架构与性能路线图](architecture_performance_roadmap_20261002.md)、[验证记录](architecture_performance_validation_20261002.md)、[后续实施](architecture_performance_followup_20261002.md)：行情客户端、请求预算、有限并发、补帧、保护调度与状态导出的工程演进。
- [网页工作台](../dashboard/README.md)：账户监控、离线回测、实验档案、数据质量与 Walk-forward；模块、API 和验证命令见[前端工程说明](../dashboard/FRONTEND.md)。

- [TrendPortfolioV3 全市场日线研究](trend_portfolio_v3.md)：历史归档及因果成员证据、所有达标资产周调仓、动量/突破两版、两种融资口径、真实组合撮合与固定88次比较；默认关闭，保留V2对照。

- [TrendPortfolioV2 研究策略](trend_portfolio_v2.md)：20/60/120 趋势分数、状态风险倍率、波动率目标仓位、ATR 退出及双交易所五组固定对照；工程验证和策略准入分开记录。

- [Roadmap 第7节验收记录](section7_acceptance_20260920.md)：优先级和依赖阻断、七项完成定义、24项历史本地验收的显式证据适配，以及完整证据检查与CI结构检查的区别；[完成定义契约](roadmap_completion_contract.md)。

- [Roadmap 第5、6节执行记录](section56_acceptance_20260920.md)：九规则验证入口、连续观察与前瞻协议校验、S2/S3隔离能力、新候选登记及未完成边界；[选币契约](s2_selection_contract.md)、[仓位能力契约](s3_position_capabilities.md)。

- [`p1_signal_meta_layer.md`](p1_signal_meta_layer.md)：2026-09-18 P1 条件 EV、时间衰减、收缩、有效样本与冻结滚动研究；默认关闭，不影响正式账户或准入。
- [`p23_signal_meta_layer.md`](p23_signal_meta_layer.md)：P2 因果软状态与动态轴归因、P3 三组有限资本影子账户；默认关闭，包含复现及论文实现差异。

- [`p0_signal_observation.md`](p0_signal_observation.md)：2026-09-18 P0 原始候选、因果标签、实际成交关联与 Ghost 诊断；默认关闭，不改变策略准入。

- [`baselines/main_20260906/README.md`](baselines/main_20260906/README.md)：合并提交 `8a89116` 的主分支 CI、本地覆盖率、三次固定历史回测与本地证据归档索引。

- [`strategy_health_lock_investigation.md`](strategy_health_lock_investigation.md)：人工锁定根因、旧止损残留修复与更新回测；后续仓位模块草案见 [`position_management_plan.md`](position_management_plan.md)。

- [`p0_drawdown_recovery.md`](p0_drawdown_recovery.md)：2026-09-05 组合 BLOCK_NEW 冷静期恢复契约、重启兼容性与两组历史 A/B 回测；不代表策略重新准入或实盘放行。

## 历史资料

- 本次完整重整资料位于 [2026年9月Roadmap与邮件联合归档](archive/2026-09-roadmap-rebaseline/README.md)，包含旧主文档、领域计划、历史归档链、策略契约和项目Codex邮件。
- 已停止独立排期的旧路线图、架构审计和旧基线位于 [`archive/2026-08-roadmap-consolidation/`](archive/2026-08-roadmap-consolidation/README.md)，其中也包含旧任务编号到 R0–R8、BM0–BM8 的映射表。
- `unified_roadmap.md` 生效后的几次一次性代码审查/问题清单快照位于 [`archive/2026-08-technical-reviews/`](archive/2026-08-technical-reviews/README.md)。

历史文件不得继续维护项目完成状态。

## 文档与注释维护约定

- README 负责入口与常见操作，模块文档负责职责、调用关系和边界，专项契约负责精确定义；重复内容优先链接到对应来源。
- 当前行为按代码、配置与相关测试核对。示例写明工作目录、输入、输出和必要前提，避免把计划中的能力写成已支持。
- 代码注释优先解释处理顺序、时间可用性、单位、失败路径和兼容原因；docstring 说明调用方需要遵守的契约，不逐行复述实现。
- 日期、测试数量、研究结果和验收结论属于相应批次的证据。后续变化追加有日期的记录，保留历史基线和归档原文。
- 增加或移动文档时同步更新导航；提交前核对相对链接和代码路径，避免引用仅在个人机器存在的绝对路径。
