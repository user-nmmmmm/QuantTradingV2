# QuantTrading 统一开发 Roadmap

> 文档状态：Active v2.1（2026-09-20 证据复核）
> 生效日期：2026-08-02  
> 权威范围：项目级优先级、阶段顺序、放行门槛和跨领域依赖  
> 回测执行计划：[`development_plan.md`](development_plan.md)  
> 实盘执行计划：[`live_trading_remediation_plan.md`](live_trading_remediation_plan.md)  
> 回测指标计划：[`backtest_metrics_detailed_development_plan.md`](backtest_metrics_detailed_development_plan.md)
> 策略能力计划：[`strategy_development_roadmap.md`](strategy_development_roadmap.md)（S0–S4：缺陷清理、选币、保证金、合约策略）
> 当前策略专项：[`current_strategy_remediation_roadmap.md`](current_strategy_remediation_roadmap.md)（健康生命周期、保护性止损、组合风险与重新准入）

## 1. 文档治理

本文件是项目唯一总路线图。`development_plan.md` 负责回测领域（R0/R1）批次、文件和验收；`live_trading_remediation_plan.md` 负责实盘领域（R4–R6）的 G0–G10 任务分解；回测指标详细计划负责公式、字段和边界。发生冲突时按“统一 Roadmap → 领域执行计划 → 领域详细计划”处理。

以下旧 roadmap 已移入 [`archive/2026-08-roadmap-consolidation/`](archive/2026-08-roadmap-consolidation/README.md)，停止独立排期，只保留需求、旧任务 ID 和审计记录：`roadmap.md`、`roadmap_detailed.md`、`current_system_remediation_roadmap.md`、`backtest_metrics_development_roadmap.md`、`formula_monitoring_roadmap.md`。

历史任务 ID 保留；项目阶段统一使用 `R0–R8`，不再使用含义冲突的 Phase 编号。完成状态以代码、测试和验收产物为准。

## 2. 目标与边界

目标是把研究原型演进为回测可信、语义一致、事实可审计、故障可恢复、风险可控制的系统：结果可重复和对账；回测/回放/实盘共享语义；交易事实可恢复；故障时停止新增风险；Metric/Alert/Policy/Action 解耦；通过固定回归、sandbox 和 paper trading 后逐级放行。

R7 完成前不使用真实资金；R8 只允许小额、少标的、单交易所灰度。

首轮不做未经样本外验证的 Alpha 扩张、账本稳定前的 ML 交易、无真实数据的微观结构指标，以及未经复核的自动扩大风险。

## 3. 当前基线与缺口

复核日期：2026-09-20。代码基点为 `ab15bc2`，另有尚未提交的 P2/P3 元层和策略复核改动。详细依据及 Gmail 中 45 封 Codex 邮件的 77 条审查意见见 [本次完成情况与邮件审计](codex_mail_roadmap_audit_20260920.md)。审计文档是本次证据快照，项目阶段状态仍以本文件为入口。

| 阶段 | 当前状态 | 已有证据及剩余边界 |
| --- | --- | --- |
| R0 | 历史基线已验收；当前版本待重新冻结 | 2026-09-06 精确提交、固定三次回测和工程验收已归档；当前工作区不等于该提交，旧 Phase 0 归档仍有跨平台 hash/冻结问题。 |
| R1 | 部分完成 | MetricResult、成本、lot/组合会计检查已存在；常规 metrics.json、完整指标状态契约、报告 FIFO 与主账本事实统一仍有缺口。独立 research audit ledger 不等于已接入交易主链。 |
| R2 | 主要分析能力已实现；未全阶段验收 | 回撤、交易质量、暴露、漏斗与成本敏感性已有代码和测试；现金成本、漏斗和部分图表口径仍有邮件问题未闭环。 |
| R3 | 工具已实现；研究准入未通过 | 归因、基准、walk-forward、Bootstrap/Monte Carlo 等已有；样本外隔离、统计门槛和冻结配置仍需修复。最新默认策略研究结果为 fail。 |
| R4 | 订单安全基础已实现；部分完成 | 幂等、状态机、重启恢复与交易所规则已有测试；完整账户对账和部分风险执行边界仍未闭环。 |
| R5 | 共享执行核心已实现；事实链部分完成 | 回测/实盘使用共享 EventProcessor；旧事件类型兼容及存量事件幂等迁移仍有缺口。 |
| R6 | 运维模块已实现；验收判断仍需修复 | 监控、告警、快照、备份、Dashboard 已存在；Phase 6 对健康、对账字段、长跑连续性的判断仍可能误通过。 |
| R7 | 未找到可核验的连续运行完成证据 | 离线故障测试和门控工具不能代替 2–4 周 sandbox/paper；更严格的 Phase 6 证据要求也未因此满足。 |
| R8 | 未放行 | 尚无前置门槛、人工批准与真实小额运行的完整验收链。 |

策略专项：S0/S1/S2/S3 均为部分完成，S4 未形成准入结论；SR0–SR3 已有核心实现，SR4 数据研究仍有缺口，SR5 回顾性运行已产出但默认策略失败，SR6 未放行。P0/P1 信号研究已合入；P2/P3 有本地实现及隔离验收记录，仍属工作区研究交付，不代表正式账户准入。

2026-09-19 的策略复核批次已完成 15 个基线、572 个矩阵、39 个验证运行；`completion.json` 仍为 `engineering_status=pending`、`evidence_pipeline_complete=false`，元层分层报告及完整交付回执未闭环。正式策略保持 `paused_revalidation`。不能把已跑完的矩阵、通过的单元测试或合并的 PR 当作整个项目完成。

## 4. 优先级

| 优先级 | 定义 | 处理原则 |
| --- | --- | --- |
| P0 | 重复订单、未知仓位、失效风控或资金错误 | 阻止真实资金，立即处理 |
| P1 | 回测失真、账务不平或回测/实盘分叉 | sandbox 长跑前完成 |
| P2 | 审计、监控、运维和研究可信度 | 小额实盘前完成 |
| P3 | 策略、模型、性能和体验扩展 | 不抢占 P0/P1 |

## 5. 阶段总览

| 阶段 | 名称 | 核心目标 | 主要任务 |
| --- | --- | --- | --- |
| R0 | 基线与治理 | 建立唯一事实基线 | ENG、DOC、fixtures |
| R1 | 回测可信内核 | 指标、账本、成本、对账 | BT-04、BM0 |
| R2 | 风险与交易分析 | 回撤、交易质量、暴露、成本 | BM1–BM4 |
| R3 | 归因与稳健性 | 归因、基准、路径和样本外 | BM5–BM8、RES-01 |
| R4 | 交易所与订单闭环 | 合法下单、事实和恢复 | EXCH-01、ORD |
| R5 | 共享事件管线 | 收敛回测/回放/实盘 | ARCH-01 |
| R6 | 监控、告警与运维 | 快照、告警、对账、守护 | OBS、MON、ALERT、OPS |
| R7 | Sandbox/Paper | 故障注入和连续运行 | 全链路演练 |
| R8 | 小额实盘灰度 | 受控验证真实执行 | 最小权限、急停、回滚 |

## 6. 依赖关系

```text
R0
 ├─> R1 ─> R2 ─> R3
 └─> R4
      R1 + R4 ─> R5
      R2 + R5 ─> R6
      R3 + R6 ─> R7 ─> R8
```

R1 与 R4 可在 R0 后并行；R5 必须等待两侧契约稳定；R3 的结论只有在 R1/R2 稳定后有效。

## 7. 阶段范围和退出条件

### R0 基线与治理

固定无交易、可手算、固定历史/合成三类数据和 start/end/seed/config；保存 orders、fills、equity、metrics；统一文档和生成物规则。

退出：同一配置连续三次事实一致；基线不依赖当天；测试全绿或失败有责任任务；旧 roadmap 归档；任务状态唯一。

### R1 回测可信内核

顺序：MetricResult/JSON → 权益/收益/年化 → 现有指标 → 成本 → trade ledger → Portfolio/PnL 对账 → 报告迁移 → BM0 验收。

退出：无非标准 JSON；0 与不可计算可区分；年化来源明确；输入不被修改；closed trade 可追溯；现金、持仓、PnL、费用和权益对账；缺价不静默验收。

### R2 风险与交易分析

BM1 回撤事件；BM2 交易质量；BM3 暴露和信号漏斗；BM4 成本与执行敏感性。

退出：回撤可定位；边界样本无误导值；多标的暴露正确；固定订单下成本增加时净 PnL 不增；未建模成本明确。

### R3 归因与稳健性

BM5 归因；BM6 基准/滚动/分段；BM7 R-Multiple/MAE/MFE/SQN；BM8 train/test、walk-forward、Bootstrap、Monte Carlo 和多重测试。

退出：分组与总体对账；基准无未来数据；样本外不参与调参；展示稳定邻域；结果可复现。

### R4 交易所与订单闭环

加载 markets；统一 rounding、min qty/notional；完善 order/fill/reconciliation；恢复 unknown/timeout/partial；校验 reduce-only。

退出：非法订单预先拒绝；超时不重复；重启可恢复；订单/成交/持仓日终对账；事实陈旧时禁止新增风险。

### R5 共享事件管线

定义 MarketEvent、Signal、OrderIntent、Order、Fill、PortfolioSnapshot 和因果 ID；共享 signal → risk → intent，模式差异封装为 adapter。

退出：同一事件流在回测和回放产生相同 signal/intent；fill 可回溯；崩溃后不重复消费。

### R6 监控、告警与运维

按 `Metric → Alert → Policy → Action` 建设原子快照、遥测、告警滞回、启动检查、心跳、日终对账、备份回滚和稳定 schema Dashboard。

退出：状态不半写；告警可追溯；指标不直接平仓；告警可抑制；故障有演练。

### R7 Sandbox/Paper 与 R8 小额实盘

R7 覆盖超时、乱序、unknown、部分成交、拒单、精度、数据异常、崩溃、状态损坏和熔断恢复；paper trading 连续 2–4 周无未解释差异，P0/P1 全部关闭。

R8 从单交易所、单标的、最小订单开始，使用最小权限、禁用提现、人工急停和可验证回滚，每次扩容重新评审。

## 8. 完成定义与当前顺序

任务必须定义清晰、有相称测试、不伪装不可计算值、记录差异、符合 schema、通过对账/性质测试、更新文档并保存验收产物。收益变高不是完成标准。

当前顺序（2026-09-20 复核）：先关闭准入误通过、前视/数据来源、成本/生命周期及风险动作等已确认邮件缺口 → 补齐存量事件迁移与账本/报告契约 → 完成当前研究批次的证据导出与工程验收 → 在冻结配置上完成独立研究判定 → 连续 sandbox/paper → 再评估 R8。各批次的具体邮件 ID、验收条件和剩余事项见本次审计的后续 Roadmap；原阶段退出条件不降低。
