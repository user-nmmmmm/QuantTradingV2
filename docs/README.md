# QuantTrading 文档导航

本文档目录区分“当前有效文档”和“历史审计资料”。开发、验收和运行决策应以当前有效文档为准；归档文件只用于追溯旧任务编号、历史判断和迁移来源。

## 从哪里开始

按当前任务选择入口，无需先读完全部历史材料。

| 阅读目的 | 首选文档 | 补充参考 |
| --- | --- | --- |
| 首次安装、回测或打开网页 | [项目 README](../README.md)、[本地研究工作台](../dashboard/README.md) | [依赖与环境](dependency_management.md)、[前端维护](../dashboard/README.md) |
| 理解代码职责和调用关系 | [代码模块说明](modules/README.md) | [架构边界审查](architecture_review.md)、[词汇表](glossary.md) |
| 查看阶段、依赖与放行条件 | [统一 Roadmap v3](unified_roadmap.md) | [统一开发计划](development_plan.md)、[结构化任务登记](development_task_registry.json) |
| 实施任务、核对交付和验收 | [开发详情](development_details.md) | [2026-09-20 后续实施记录](roadmap_acceptance_log.md) |
| 核对回测假设和指标公式 | [当前回测行为](backtest_assumptions.md) | [指标公式与专项标准](archive/2026-10-doc-consolidation/backtest_metrics_detailed_development_plan.md) |
| 查找研究、采集和验证入口 | [脚本索引](../scripts/README.md) | 下方[最新行为变更](#最新行为变更)中的专项契约与实施记录 |
| 部署、排障和周期性运行 | [部署与运维](deployment.md)、[账户事实操作说明](account_fact_operations.md) | [自动化回测操作方案](archive/2026-10-doc-consolidation/automated_backtest_plan.md) |
| 整理工程结构与生成物 | [工程结构优化路线图](engineering_structure_roadmap.md) | [文件保留与生成物规则](file_retention.md) |
| 追溯旧任务和历史结果 | [历史需求与邮件追溯](roadmap_traceability.md)、[固定历史基线](baselines/batch0_fixed_baseline.md) | [2026-09-20 审计](archive/2026-10-doc-consolidation/codex_mail_roadmap_audit_20260920.md)、[原文与邮件归档](archive/2026-09-roadmap-rebaseline/README.md) |

## 权威顺序

用户明确要求和已批准契约 → 统一Roadmap（阶段与门槛） → 开发计划/登记表（排期与状态） → 开发详情（任务契约与验收） → 领域公式/行为参考 → 历史归档。

项目阶段只在Roadmap维护；任务状态在开发计划与结构化登记同步维护；开发详情保留基线及技术要求，关闭时追加验收索引。原实盘、策略、仓位和回测优化计划保留为领域参考，其旧排期与完成标记不再独立维护。已批准的研究、风控、止损、健康等契约继续有效，冲突处理见新Roadmap。

当前行为文档不把计划中的能力写成已实现。工程验收、研究结论、连续运行和真实资金准入分别记录。
工程结构优化路线图是实施建议与检查清单，不改写上述 Roadmap 阶段或任务登记状态。

## 最新行为变更

以下按近期主题提供入口。带日期的实施记录说明对应批次的工作和证据，文件日期较新并不自动替代已批准契约或更新项目放行状态。

- [机器学习选币:现状与历史记录](ml_selection.md)：操作说明、工程整改、回测开关与下一阶段 Roadmap 见 [ml_selection.md](ml_selection.md)；V1 复核、本轮执行、冻结回测、诊断与稳健性解读见 [ml_selection_history.md](ml_selection_history.md)；[学习清单](ml_selection_learning_checklist_20261005.md)。均为研究/工程记录，`formal_admission=false` 保持不变。
- [2026-10-04 剩余论文任务](research/paper_remaining_tasks_20261004.md)：执行成本与盘口证据、历史因子输入、前瞻采集和严格标签的实现与缺口；[收益改进复核](research/paper_applications.md)记录固定方案的历史比较，保留主引擎与独立现货账户的口径差异。
- [论文统计验证契约](research/paper_validation_contract_20261003.md)：PBO、purged/embargo 切分、White Reality Check 与 DSR 的输入、计算和证据不足行为；应用入口见[论文应用清单](research/paper_applications.md)和[工程完成记录](research/paper_applications.md)。
- [论文应用](research/paper_applications.md)：32 篇清单、路线图、十项实现、工程收口、续办与收益复核的时间线合集；[剩余任务](research/paper_remaining_tasks_20261004.md)与[统计验证契约](research/paper_validation_contract_20261003.md)保持独立。
- [架构与性能](architecture_performance.md)：AP01–AP13 的路线图、验证记录与后续实施；[回测性能优化记录](backtest_performance.md)。
- [V4 架构设计(草案)](v4_architecture_design.md)：分层包结构、统一特征管线、选币协议、策略插件接口、两层回测与实施路线；[文档整理审计](doc_consolidation_audit_20261010.md)记录了本轮文档合并。尚未登记进统一 Roadmap。
- [网页工作台](../dashboard/README.md)：账户监控、离线回测、实验档案、数据质量与 Walk-forward；模块、API 和验证命令见[前端工程说明](../dashboard/README.md)。

- [TrendPortfolio V2/V3 研究策略](trend_portfolio.md)：趋势分数、状态风险倍率、波动率目标、全市场周调仓与固定对照；工程验证和策略准入分开记录。

- [Roadmap 验收记录](roadmap_acceptance_log.md)：R 系列、第 5/6 节、第 7 节、后续完成记录与 M-01–M-20 能力矩阵；机器验收入口见脚本索引。

- [信号元层 P0–P3](signal_meta_layer.md)：P0 候选观察、P1 条件 EV、P2/P3 因果软状态与影子账户；默认关闭，不影响正式账户或准入。

- [`baselines/main_20260906/README.md`](baselines/main_20260906/README.md)：合并提交 `8a89116` 的主分支 CI、本地覆盖率、三次固定历史回测与本地证据归档索引。

- [`strategy_health_lock_investigation.md`](strategy_health_lock_investigation.md)：人工锁定根因、旧止损残留修复与更新回测；后续仓位模块草案见 [`position_management_plan.md`](archive/2026-10-doc-consolidation/position_management_plan.md)。

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
