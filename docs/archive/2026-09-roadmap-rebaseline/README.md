# 历史 Roadmap、开发计划与 Codex 邮件联合归档

**归档日期：2026-09-20；审计基点：`ab15bc2+dirty`。** 本目录将改版前的路线图、开发计划、Phase/T 记录、后续约束合同和项目 Codex 邮件放在一起，作为新路线图的可追溯输入。它保留历史原文，历史勾选、优先级或 Running 记录不自动成为当前完成结论。

初始 30 份文档在 `2026-09-20T02:13:33.627501+00:00`（UTC）冻结；新增合同及 Phase 0 基线说明的独立捕获时间记录在清单中。共保存 **34 份原始文档快照、45 封项目邮件正文、77 条邮件意见**。保留旧文件原处，本目录保存其副本。

## 从这里阅读

- [当前总 Roadmap](../../unified_roadmap.md) 与 [当前开发计划](../../development_plan.md)：本次改版后的开发入口。
- [邮件索引](emails/README.md)：按时间定位 45 封邮件、PR 和意见数量。
- [邮件正文合集](emails/codex_project_emails.md)：审查正文、代码片段、原始链接和消息标识。
- [历史审计报告](sources/docs/codex_mail_roadmap_audit_20260920.md) 与 [77 条结构化审计意见](emails/codex_mail_findings_20260920.json)：状态为 24 已修复、7 部分修复、45 仍存在、1 证据不足。
- [完整来源及 SHA-256 清单](manifest.json)：源路径、快照路径、字节数、捕获时间、哈希以及派生归档文件。

## 来源文档索引

快照按 `sources/<原始项目路径>` 保存。所有原始文件使用逐字节复制，未修正文内链接；原文件里的相对链接可能仍指向旧目录结构，因此请从下面的来源索引导航。原文内部的历史任务 ID 全部保留。

### 2026-09 改版前的 Roadmap、开发计划与审计

| 原始路径 | 冻结快照 | 用途 |
| --- | --- | --- |
| `docs/unified_roadmap.md` | [unified_roadmap.md](sources/docs/unified_roadmap.md) | 旧项目总 Roadmap（R0–R8） |
| `docs/development_plan.md` | [development_plan.md](sources/docs/development_plan.md) | 旧批次开发计划（Batch 0–11） |
| `docs/live_trading_remediation_plan.md` | [live_trading_remediation_plan.md](sources/docs/live_trading_remediation_plan.md) | 实盘整改与执行链路计划 |
| `docs/backtest_metrics_detailed_development_plan.md` | [backtest_metrics_detailed_development_plan.md](sources/docs/backtest_metrics_detailed_development_plan.md) | 回测指标详细开发计划（BM 系列） |
| `docs/strategy_development_roadmap.md` | [strategy_development_roadmap.md](sources/docs/strategy_development_roadmap.md) | 策略能力路线图（S0–S4） |
| `docs/current_strategy_remediation_roadmap.md` | [current_strategy_remediation_roadmap.md](sources/docs/current_strategy_remediation_roadmap.md) | 当前策略修复路线图（SR 系列） |
| `docs/position_management_plan.md` | [position_management_plan.md](sources/docs/position_management_plan.md) | 仓位与保证金计划（PM 系列） |
| `docs/backtest_optimization_roadmap.md` | [backtest_optimization_roadmap.md](sources/docs/backtest_optimization_roadmap.md) | 回测性能与优化路线图 |
| `docs/codex_mail_roadmap_audit_20260920.md` | [codex_mail_roadmap_audit_20260920.md](sources/docs/codex_mail_roadmap_audit_20260920.md) | 上一轮完成情况和邮件审计正文 |
| `docs/codex_mail_findings_20260920.json` | [codex_mail_findings_20260920.json](sources/docs/codex_mail_findings_20260920.json) | 上一轮 77 条邮件意见结构化审计 |

### 历史 2026-08 Roadmap 合并资料

| 原始路径 | 冻结快照 | 用途 |
| --- | --- | --- |
| `docs/archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md` | [backtest_metrics_development_roadmap.md](sources/docs/archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md` | [current_system_remediation_roadmap.md](sources/docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md` | [formula_monitoring_roadmap.md](sources/docs/archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/phase0_baseline.md` | [phase0_baseline.md](sources/docs/archive/2026-08-roadmap-consolidation/phase0_baseline.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/phase0_baseline_results.md` | [phase0_baseline_results.md](sources/docs/archive/2026-08-roadmap-consolidation/phase0_baseline_results.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md` | [project_file_and_architecture_analysis.md](sources/docs/archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/README.md` | [README.md](sources/docs/archive/2026-08-roadmap-consolidation/README.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/roadmap.md` | [roadmap.md](sources/docs/archive/2026-08-roadmap-consolidation/roadmap.md) | 原历史文档；含当时规划、任务映射或验收记录 |
| `docs/archive/2026-08-roadmap-consolidation/roadmap_detailed.md` | [roadmap_detailed.md](sources/docs/archive/2026-08-roadmap-consolidation/roadmap_detailed.md) | 原历史文档；含当时规划、任务映射或验收记录 |

### Phase/T 实施来源、研究计划与后续合同

| 原始路径 | 冻结快照 | 用途 |
| --- | --- | --- |
| `docs/phase3_implementation.md` | [phase3_implementation.md](sources/docs/phase3_implementation.md) | Phase 3 实现与验收映射 |
| `docs/phase4_implementation.md` | [phase4_implementation.md](sources/docs/phase4_implementation.md) | Phase 4 / T-4.x 实现记录 |
| `docs/phase5/README.md` | [README.md](sources/docs/phase5/README.md) | Phase 5 研究与样本外验证入口 |
| `docs/phase6_operations.md` | [phase6_operations.md](sources/docs/phase6_operations.md) | Phase 6 / T-6.x 运维与准入契约 |
| `docs/p0_signal_observation.md` | [p0_signal_observation.md](sources/docs/p0_signal_observation.md) | 信号 P0 观察层计划与契约 |
| `docs/p1_signal_meta_layer.md` | [p1_signal_meta_layer.md](sources/docs/p1_signal_meta_layer.md) | 信号 P1 元层计划与契约 |
| `docs/p23_signal_meta_layer.md` | [p23_signal_meta_layer.md](sources/docs/p23_signal_meta_layer.md) | 信号 P2/P3 元层计划与契约 |
| `docs/p0_drawdown_recovery.md` | [p0_drawdown_recovery.md](sources/docs/p0_drawdown_recovery.md) | P0 回撤恢复任务与约束 |
| `docs/research/strategy_remediation_contract_20260914.md` | [strategy_remediation_contract_20260914.md](sources/docs/research/strategy_remediation_contract_20260914.md) | 2026-09-14 策略整改合同 |
| `docs/r7_sandbox_runbook.md` | [r7_sandbox_runbook.md](sources/docs/r7_sandbox_runbook.md) | R7 连续运行与证据收集手册 |
| `docs/baselines/batch0_fixed_baseline.md` | [batch0_fixed_baseline.md](sources/docs/baselines/batch0_fixed_baseline.md) | Batch 0 固定基线定义 |
| `docs/strategy_health_contract.md` | [strategy_health_contract.md](sources/docs/strategy_health_contract.md) | 策略健康状态契约 |
| `docs/protective_stop_contract.md` | [protective_stop_contract.md](sources/docs/protective_stop_contract.md) | 保护止损生命周期契约 |
| `docs/portfolio_risk_contract.md` | [portfolio_risk_contract.md](sources/docs/portfolio_risk_contract.md) | 组合风险预算契约 |
| `docs/baseline/phase0/README.md` | [README.md](sources/docs/baseline/phase0/README.md) | Phase 0 基线任务 T-0.1–T-0.5 与 T-0.4 工作簿来源 |

## 邮件来源与整理规则

邮件源为此前已从 `mnm799063@gmail.com` 检索并保存的 `tmp/roadmap_audit_20260920/source_emails.json`，包含且只包含本项目相关 Codex 通知。此归档步骤没有重新访问、发送、删除或修改邮箱。仅保留与项目有关的邮件内容；无其他邮件。

邮件按时间排序，保存主题、发送时间、Gmail 消息/线程 ID、GitHub/Gmail 来源链接与审查正文。重复的 Codex 使用说明折叠区和通知尾注已裁剪，正文换行规范化；每封邮件标明裁剪范围。没有复制原始 MIME/邮件头、退订信息或跟踪内容。结构化正文保存原 body 的 SHA-256，清单保留输入集合的 SHA-256 以便核验来源。

邮件日期范围为 **2026-08-09 至 2026-09-18（Asia/Singapore）**。30 个 PR 中可存在多封邮件、重复问题或只有审查状态摘要；45 封邮件和 77 条意见均不代表独立开发任务数。

## 冻结与验收

1. 34 份文档快照均以复制时的原始字节计算 SHA-256；改版后活跃文档允许变化，因此今后应验证快照与清单，而不是要求其继续与活跃文件一致。
2. 已核对 45 个唯一 Gmail 消息 ID、30 个 PR、77 个唯一意见 ID，并核对每条意见在对应邮件正文中仍有标题；28 封具体意见、17 封进度摘要的总数一致。
3. 邮件区结构化意见文件与原始审计快照完全相同；内容归档、索引和总入口的哈希也写入清单。清单不自哈希。
4. 这里保存的是 `ab15bc2` 加未提交工作区的审计证据。它不声明所有代码、研究、长跑或实盘已经验收通过。
