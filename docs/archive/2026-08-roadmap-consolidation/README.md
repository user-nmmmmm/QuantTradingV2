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

## 旧任务编号到统一阶段的映射

以下映射迁移自已废弃的 `roadmap_migration.md`，供追溯旧任务 ID 时参考；`Phase N`、`FMN`、旧 `BMN` 不代表当前执行阶段。引用历史任务时须同时注明统一阶段，例如 `BT-04 / R1`、`FM3 / R6`。

| 统一阶段 | 吸收的历史范围 |
| --- | --- |
| R0 | 旧 Phase 0、工程基线、文档治理、固定回归 |
| R1 | 旧可信回测、BM0、FM0/FM1 的指标正确性和内核 |
| R2 | BM1–BM4、旧 FM2 的回测可观测性 |
| R3 | BM5–BM8、RES-01、旧稳健性和路径质量任务 |
| R4 | 旧订单状态、恢复、交易所规则、EXCH/ORD |
| R5 | 旧架构重构、统一事件引擎、ARCH-01 |
| R6 | 旧 FM3–FM6、OBS、MON、ALERT、OPS、Dashboard |
| R7 | 旧 sandbox、故障注入、paper trading 和生产门槛 |
| R8 | 旧小额实盘阶段和灰度扩容 |

### 回测 BM 编号统一

- BM0：指标契约、权益/收益、成本、trade ledger、资金对账和报告迁移；
- BM1：回撤事件；
- BM2：交易质量和样本可信度；
- BM3：暴露、资金效率和信号漏斗；
- BM4：成本与执行敏感性；
- BM5：策略、标的、方向和退出归因；
- BM6：基准、滚动、分段和参数邻域分析；
- BM7：Initial Risk、R-Multiple、MAE/MFE、Exit Efficiency、SQN；
- BM8：样本内/外、walk-forward、Bootstrap、Monte Carlo 和多重测试。

旧 `backtest_metrics_development_roadmap.md` 中 BM6 的交易路径任务迁移到新 BM7；旧 BM7 的稳健性任务迁移到新 BM8。M-xxx 公式 ID 保持不变。
