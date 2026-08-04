# 2026-08 Roadmap 整合归档

> 归档日期：2026-08-02  
> 状态：Historical / Read-only

本目录保存统一路线图生效前的路线图、整改审计、架构分析和 Phase 0 基线。文件内容用于追溯历史判断和旧任务 ID，不再代表当前排期、完成状态、测试数量或生产放行结论。

当前有效文档请从 [`../../README.md`](../../README.md) 开始阅读。旧编号迁移关系见 [`../../roadmap_migration.md`](../../roadmap_migration.md)。

## 文件说明

| 文件 | 历史用途 | 当前替代文档 |
| --- | --- | --- |
| `roadmap.md` | 早期粗粒度重构阶段 | `unified_roadmap.md` |
| `roadmap_detailed.md` | 早期工程分解和测试矩阵 | `unified_roadmap.md`、`development_plan.md` |
| `current_system_remediation_roadmap.md` | LIVE/RISK/ORD/BT 等整改审计和当时状态 | `unified_roadmap.md`、`development_plan.md` |
| `backtest_metrics_development_roadmap.md` | 旧 BM0–BM7 指标候选与审查 | `backtest_metrics_detailed_development_plan.md` |
| `formula_monitoring_roadmap.md` | 公式、监控、告警和旧 FM 阶段设计 | R1–R3、R6 相关当前计划 |
| `project_file_and_architecture_analysis.md` | 2026-08-01 左右的架构审计快照 | 当前代码和未来的稳定架构说明 |
| `phase0_baseline.md` | 2026-07-22 测试基线记录 | `baselines/batch0_fixed_baseline.md` |
| `phase0_baseline_results.md` | 动态日期 synthetic 人工基线 | 固定 fixture 回归基线 |

不要在这些文件中继续勾选任务或更新当前状态。如其中仍有有效要求，应先迁移到当前权威文档并通过代码、测试和验收产物确认。
