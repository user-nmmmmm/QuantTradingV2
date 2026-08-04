# Roadmap 整合与迁移索引

> 生效日期：2026-08-02  
> 状态：Active（过渡索引）

## 1. 权威文档

| 层级 | 文档 | 职责 |
| --- | --- | --- |
| 项目级 | [`unified_roadmap.md`](unified_roadmap.md) | 唯一总路线图、阶段、依赖和放行门 |
| 执行级 | [`development_plan.md`](development_plan.md) | 开发批次、任务顺序和验收产物 |
| 回测领域 | [`backtest_metrics_detailed_development_plan.md`](backtest_metrics_detailed_development_plan.md) | 指标公式、输入、边界、测试和专项 DoD |
| 假设契约 | [`backtest_assumptions.md`](backtest_assumptions.md) | 回测时间、费用、滑点、数据和指标口径 |

如有冲突，优先级为：统一总路线图 → 统一开发计划 → 领域详细计划 → 历史资料。

## 2. 已整合历史文档

以下文件自 2026-08-02 起停止独立排期和维护完成状态，并已移入 [`archive/2026-08-roadmap-consolidation/`](archive/2026-08-roadmap-consolidation/README.md)：

| 历史文档 | 保留内容 | 迁移目标 |
| --- | --- | --- |
| [`roadmap.md`](archive/2026-08-roadmap-consolidation/roadmap.md) | 早期重构阶段与目标 | R0–R8 |
| [`roadmap_detailed.md`](archive/2026-08-roadmap-consolidation/roadmap_detailed.md) | 工程分解、测试矩阵、旧决策门 | R0、R4–R8 |
| [`current_system_remediation_roadmap.md`](archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md) | LIVE/RISK/ORD/BT/ARCH/OBS/OPS 审计和历史状态 | R0、R1、R4–R6 |
| [`backtest_metrics_development_roadmap.md`](archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md) | 旧 BM0–BM7 指标候选和审查 | 新 BM0–BM8 |
| [`formula_monitoring_roadmap.md`](archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md) | M-xxx 公式目录、监控设计、旧 FM0–FM7 | R1–R3、R6 |

历史文档中的 `Phase N`、`FMN` 或旧 `BMN` 不再代表项目执行阶段。引用历史任务时必须同时注明统一阶段，例如：`BT-04 / R1`、`FM3 / R6`。

## 3. 统一编号映射

| 统一阶段 | 吸收的历史范围 |
| --- | --- |
| R0 | 旧 Phase 0、工程基线、文档治理、固定回归 |
| R1 | 旧可信回测、BM0、FM0/FＭ1 的指标正确性和内核 |
| R2 | BM1–BM4、旧 FM2 的回测可观测性 |
| R3 | BM5–BM8、RES-01、旧稳健性和路径质量任务 |
| R4 | 旧订单状态、恢复、交易所规则、EXCH/ORD |
| R5 | 旧架构重构、统一事件引擎、ARCH-01 |
| R6 | 旧 FM3–FM6、OBS、MON、ALERT、OPS、Dashboard |
| R7 | 旧 sandbox、故障注入、paper trading 和生产门槛 |
| R8 | 旧小额实盘阶段和灰度扩容 |

## 4. 回测 BM 编号统一

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

## 5. 状态维护规则

1. 项目阶段状态只在 `unified_roadmap.md` 维护；
2. 当前批次勾选只在 `development_plan.md` 维护；
3. 指标公式和边界只在回测详细计划维护；
4. 历史文档不得继续新增独立 Sprint 或改变统一开发顺序；
5. “已实现初版”不等于“已验收”；只有满足自动化测试、对账、差异说明和阶段产物后才能完成；
6. 历史测试数量不得用作当前状态，必须重新运行当前完整测试。
