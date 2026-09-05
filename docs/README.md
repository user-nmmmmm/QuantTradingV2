# QuantTrading 文档导航

本文档目录区分“当前有效文档”和“历史审计资料”。开发、验收和运行决策应以当前有效文档为准；归档文件只用于追溯旧任务编号、历史判断和迁移来源。

## 从哪里开始

1. [`unified_roadmap.md`](unified_roadmap.md)：唯一项目总路线图，维护 R0–R8、优先级、依赖和放行门槛。
2. [`development_plan.md`](development_plan.md)：回测领域当前开发批次、任务顺序、交付物和验收清单（对应 R0/R1）。
3. [`live_trading_remediation_plan.md`](live_trading_remediation_plan.md)：实盘交易领域执行计划，G0–G10 任务分解（对应 R4–R6）。
4. [`backtest_metrics_detailed_development_plan.md`](backtest_metrics_detailed_development_plan.md)：回测指标公式、输入、边界、测试和专项完成标准。
5. [`backtest_assumptions.md`](backtest_assumptions.md)：当前回测引擎已经实现的执行、成本、数据和统计口径。
6. [`deployment.md`](deployment.md)：当前允许的安装、回测、sandbox 和运维方式。
7. [`baselines/batch0_fixed_baseline.md`](baselines/batch0_fixed_baseline.md)：固定回归基线及其验收证据。
8. [`modules/README.md`](modules/README.md)：逐包代码说明（core/backtest/live_trading/router/strategies/其余小包），回答"这段代码是做什么的"，不涉及项目阶段状态。
9. [`glossary.md`](glossary.md)：专业词汇表（执行/风控/绩效指标/交易质量/归因/稳健性验证/合约术语），统一中英文术语与计算口径，供后续选币、仓位管理和合约模块开发复用。

## 权威顺序

发生冲突时，按以下顺序处理：

```text
unified_roadmap.md
  → development_plan.md（回测领域）/ live_trading_remediation_plan.md（实盘领域）
  → 领域详细计划
  → 当前行为/运维文档
  → 历史归档
```

项目阶段状态只在 `unified_roadmap.md` 维护；当前批次勾选只在对应领域计划维护；指标公式和边界只在回测详细计划维护。`backtest_assumptions.md` 和 `deployment.md` 只描述当前代码已经支持的行为，不把未来计划写成现有能力。

## 最新行为变更

- [`strategy_health_lock_investigation.md`](strategy_health_lock_investigation.md)：人工锁定根因、旧止损残留修复与更新回测；后续仓位模块草案见 [`position_management_plan.md`](position_management_plan.md)。

- [`p0_drawdown_recovery.md`](p0_drawdown_recovery.md)：2026-09-05 组合 BLOCK_NEW 冷静期恢复契约、重启兼容性与两组历史 A/B 回测；不代表策略重新准入或实盘放行。

## 历史资料

- 已停止独立排期的旧路线图、架构审计和旧基线位于 [`archive/2026-08-roadmap-consolidation/`](archive/2026-08-roadmap-consolidation/README.md)，其中也包含旧任务编号到 R0–R8、BM0–BM8 的映射表。
- `unified_roadmap.md` 生效后的几次一次性代码审查/问题清单快照位于 [`archive/2026-08-technical-reviews/`](archive/2026-08-technical-reviews/README.md)。

历史文件不得继续维护项目完成状态。
