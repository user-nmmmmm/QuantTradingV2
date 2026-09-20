# QuantTrading 文档导航

> 公开发布视图：保留任务编号、状态、依赖及技术契约；私人邮件正文、逐条审查摘录和邮箱映射未发布。原始本地登记仍是权威资料。`reports/`、`outputs/`、`tmp/`、`docs/archive/` 及邮件审计文件均为仅本地引用，不随源码发布；公开 CI 的 structure-only 结果不代表这些历史证据已验证。

本文档目录区分“当前有效文档”和“历史审计资料”。开发、验收和运行决策应以当前有效文档为准；归档文件只用于追溯旧任务编号、历史判断和迁移来源。

## 从哪里开始

1. [统一 Roadmap v3](unified_roadmap.md)：项目当前状态、R0–R8阶段、依赖与放行条件。
2. [统一开发计划](development_plan.md)：41个任务（原始40项加自动化SYS-19）、B0–B8及扩展批次、优先级、串并行和负责角色。
3. [开发详情](development_details.md)：每个任务的修改范围、开发步骤、正反例验收、兼容迁移和交付物。
4. [历史需求追溯](roadmap_traceability.md)：旧R/BM/G/S/SR/PM/Phase编号到新任务的技术映射；逐条邮件映射仅保存在本地受限资料中。
5. 过去的Roadmap、计划与Codex邮件（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/README.md`，不随源码发布）：34份原文快照、45封项目邮件正文、来源与校验摘要。
6. [结构化任务登记](development_task_registry.json)：开发计划的机器可读副本；2026-09-20审计（仅本地引用：`docs/codex_mail_roadmap_audit_20260920.md`，不随源码发布）保存整合前完成情况与证据。
7. [今日回顾后续实施](followup_completion_20260920.md)：账户/指标接线、自动化运行、调度部署、验证结果及仍需真实证据的边界。
7. [指标公式与专项标准](backtest_metrics_detailed_development_plan.md)、[当前回测行为](backtest_assumptions.md)、[部署与运维](deployment.md)：精确定义与当前支持行为。
8. [固定历史基线](baselines/batch0_fixed_baseline.md)、[代码模块说明](modules/README.md)、[词汇表](glossary.md)：历史证据与开发参考。

## 权威顺序

用户明确要求和已批准契约 → 统一Roadmap（阶段与门槛） → 开发计划/登记表（排期与状态） → 开发详情（任务契约与验收） → 领域公式/行为参考 → 历史归档。

项目阶段只在Roadmap维护；任务状态在开发计划与结构化登记同步维护；开发详情保留基线及技术要求，关闭时追加验收索引。原实盘、策略、仓位和回测优化计划保留为领域参考，其旧排期与完成标记不再独立维护。已批准的研究、风控、止损、健康等契约继续有效，冲突处理见新Roadmap。

当前行为文档不把计划中的能力写成已实现。工程验收、研究结论、连续运行和真实资金准入分别记录。

## 最新行为变更

- [TrendPortfolioV3 全市场日线组合研究](trend_portfolio_v3.md)：动态全资格选币、两套规则、共享组合执行与融资证据；默认关闭，工程验收和研究裁决分别报告。

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

- 本次完整重整资料位于 2026年9月Roadmap与邮件联合归档（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/README.md`，不随源码发布），包含旧主文档、领域计划、历史归档链、策略契约和项目Codex邮件。
- 已停止独立排期的旧路线图、架构审计和旧基线位于 `archive/2026-08-roadmap-consolidation/`（仅本地引用：`docs/archive/2026-08-roadmap-consolidation/README.md`，不随源码发布），其中也包含旧任务编号到 R0–R8、BM0–BM8 的映射表。
- `unified_roadmap.md` 生效后的几次一次性代码审查/问题清单快照位于 `archive/2026-08-technical-reviews/`（仅本地引用：`docs/archive/2026-08-technical-reviews/README.md`，不随源码发布）。

历史文件不得继续维护项目完成状态。
