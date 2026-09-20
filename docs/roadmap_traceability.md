# 历史Roadmap、邮件与新任务追溯矩阵

> 公开发布视图：保留任务编号、状态、依赖及技术契约；私人邮件正文、逐条审查摘录和邮箱映射未发布。原始本地登记仍是权威资料。`reports/`、`outputs/`、`tmp/`、`docs/archive/` 及邮件审计文件均为仅本地引用，不随源码发布；公开 CI 的 structure-only 结果不代表这些历史证据已验证。

> 2026-09-20。当前执行：[Roadmap](unified_roadmap.md) → [开发计划](development_plan.md) → [开发详情](development_details.md)。
> 联合资料库：34份文档快照与45封Codex项目邮件（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/README.md`，不随源码发布）。

第5、6节追加追溯：九项旧计划冲突使用 [POL-01–09](roadmap_policy_contract.md)登记“原文档路径+旧ID”和行为回归；S2-1–S2-7→SYS-12，S3-4–S3-6→SYS-13，PM1保护身份→SYS-07，信号P0–P3交付→SYS-10，新候选重新登记→SYS-11，S4前置检查→SYS-14。每项的新证据与未闭环部分见[追加执行记录](section56_acceptance_20260920.md)，原审计和历史完成标记保持原义。

## 1. 覆盖口径

- 34份历史文档/契约按原字节快照，附SHA-256；旧计划未删除，现有领域入口转为参考。
- 45封邮件覆盖30个PR：28封有具体审查意见，17封为状态摘要；邮件归档保留正文、时间、消息ID及Gmail/GitHub来源链接，非原始MIME。
- 77条意见保持审计时状态：24已修复、7部分修复、45仍存在、1证据不足。52条未闭环各进入一个FIX，1条进入VER，24条进入回归登记。
- 18个SYS承接旧计划能力；同一能力可以依赖多个FIX，邮件只分配一次以避免重复统计。
- “来源文档路径+旧编号”是映射键；数字相同的BT/G/Phase不能直接合并。下表按相关族/阶段聚合，原文细项继续作为验收输入，不以表格行数宣称每条旧需求已完成。

## 2. 旧计划到新任务

| 原文来源 | 旧编号/范围 | 新任务 | 承接方式 |
| --- | --- | --- | --- |
| unified_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） | R0 | [SYS-01](development_details.md#sys-01)、[FIX-03](development_details.md#fix-03) | 历史固定基线保留；当前工作区重新冻结 |
| unified_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） | R1/R2 | [SYS-02](development_details.md#sys-02)、[SYS-03](development_details.md#sys-03)、[FIX-10](development_details.md#fix-10)、[FIX-11](development_details.md#fix-11)、[FIX-12](development_details.md#fix-12)、[FIX-13](development_details.md#fix-13)、[FIX-16](development_details.md#fix-16)、[FIX-19](development_details.md#fix-19)、[FIX-20](development_details.md#fix-20) | 基础已实现，补齐事实/成本/指标验收 |
| unified_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） | R3 | [FIX-02](development_details.md#fix-02)、[FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11) | 工具完成与研究通过分别记录 |
| unified_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） | R4/R5/R6 | [SYS-05](development_details.md#sys-05)、[SYS-06](development_details.md#sys-06)、[SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[SYS-16](development_details.md#sys-16) | 账户、共享运行、风险与运维闭环 |
| unified_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） | R7/R8 | [SYS-17](development_details.md#sys-17)、[SYS-18](development_details.md#sys-18) | 连续运行与灰度待证据 |
| development_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/development_plan.md`，不随源码发布） | Batch 0–10 | [SYS-01](development_details.md#sys-01)、[SYS-02](development_details.md#sys-02)、[SYS-03](development_details.md#sys-03)、[SYS-06](development_details.md#sys-06) | 旧回测批次迁入跨领域批次；技术公式仍参考指标详情 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G0/G9、ENG-01 | [SYS-01](development_details.md#sys-01)、[FIX-03](development_details.md#fix-03) | 基线/CI/仓库治理 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G1、SAFE-01 | [SYS-16](development_details.md#sys-16)、[SYS-18](development_details.md#sys-18) | 历史安全启动能力保留；在SYS-17前复验 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G2、ORD-01/02/03 | [SYS-06](development_details.md#sys-06)、[FIX-14](development_details.md#fix-14)、[FIX-17](development_details.md#fix-17)、[FIX-18](development_details.md#fix-18) | 历史订单能力保留；事务边界及当前版本回归 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G3、ACCT-01/02 | [SYS-05](development_details.md#sys-05) | 完整账户快照与六层对账 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G4、ARCH-01 | [SYS-06](development_details.md#sys-06)、[SYS-07](development_details.md#sys-07) | 共享事件/执行与持仓 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G5、OPS-01/TEST-01 | [SYS-16](development_details.md#sys-16)、[FIX-17](development_details.md#fix-17)、[FIX-18](development_details.md#fix-18) | 故障/告警/恢复演练 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G6、PAPER-01 | [SYS-17](development_details.md#sys-17) | 连续观察，不按首尾日期宣称满时长 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G7、BT-01/02 | [SYS-02](development_details.md#sys-02)、[SYS-03](development_details.md#sys-03) | 此处BT-01是指标JSON；BT-02是账本桥接 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G8、RES-01 | [SYS-11](development_details.md#sys-11)、[FIX-02](development_details.md#fix-02)、[FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09) | 独立研究准入 |
| live_trading_remediation_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） | G10 | [SYS-18](development_details.md#sys-18) | 人工评审真实资金 |
| backtest_metrics_detailed_development_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_metrics_detailed_development_plan.md`，不随源码发布） | BM0–BM8 | [SYS-03](development_details.md#sys-03)、[FIX-12](development_details.md#fix-12)、[FIX-19](development_details.md#fix-19)、[FIX-20](development_details.md#fix-20) | 全部公式、输入、边界与原细项保留为专项验收矩阵 |
| backtest_metrics_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md`，不随源码发布） | 旧BM6/旧BM7 | [SYS-03](development_details.md#sys-03)、[SYS-11](development_details.md#sys-11) | 旧交易路径BM6→现BM7；旧稳健性BM7→现BM8，不能按数字直接对应 |
| backtest_metrics_detailed_development_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_metrics_detailed_development_plan.md`，不随源码发布） | BM4 执行质量 | [SYS-03](development_details.md#sys-03)、[SYS-17](development_details.md#sys-17) | 成交/部分成交/拒绝/取消/过期、成交耗时、参与率、IS继续保留；不能只做成本 |
| strategy_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） | S0-1/S0-2/S0-3/S0-4 | [SYS-07](development_details.md#sys-07)、[FIX-08](development_details.md#fix-08)、[SYS-02](development_details.md#sys-02)、[SYS-06](development_details.md#sys-06) | 持仓上下文、真实候选路由、增量消费、pending释放 |
| strategy_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） | S1-1–S1-5 | [SYS-03](development_details.md#sys-03)、[SYS-11](development_details.md#sys-11)、[SYS-15](development_details.md#sys-15) | 当前缺陷先修，展示扩展BX；S1-3按OOS选参条款已替换 |
| strategy_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） | S2-1–S2-7 | [SYS-04](development_details.md#sys-04)、[SYS-12](development_details.md#sys-12)、[FIX-07](development_details.md#fix-07) | PIT基础进主线，完整动态选币/权重进BX |
| strategy_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） | S3-1–S3-6 | [SYS-02](development_details.md#sys-02)、[SYS-05](development_details.md#sys-05)、[SYS-08](development_details.md#sys-08)、[SYS-13](development_details.md#sys-13)、[SYS-14](development_details.md#sys-14) | 当前账户/成本/风险主线必做；新模式与仓位扩展分开 |
| strategy_development_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） | S4-1–S4-4、S4§9.1、G-S1–G-S10 | [SYS-11](development_details.md#sys-11)、[SYS-14](development_details.md#sys-14) | 研究门槛保留，候选策略后续扩展 |
| current_strategy_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/current_strategy_remediation_roadmap.md`，不随源码发布） | SR0 | [SYS-01](development_details.md#sys-01)、[SYS-10](development_details.md#sys-10)、[FIX-03](development_details.md#fix-03)、[FIX-08](development_details.md#fix-08) | 冻结与交付身份 |
| current_strategy_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/current_strategy_remediation_roadmap.md`，不随源码发布） | SR1/SR2/SR3 | [SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[FIX-10](development_details.md#fix-10)、[FIX-14](development_details.md#fix-14)、[FIX-15](development_details.md#fix-15)、[FIX-16](development_details.md#fix-16)、[FIX-17](development_details.md#fix-17) | 生命周期、健康、风险 |
| current_strategy_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/current_strategy_remediation_roadmap.md`，不随源码发布） | SR4/SR5 | [SYS-04](development_details.md#sys-04)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11)、[FIX-04](development_details.md#fix-04)、[FIX-05](development_details.md#fix-05)、[FIX-06](development_details.md#fix-06)、[FIX-07](development_details.md#fix-07)、[FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09) | 数据修复与重验，不用收益挑修复 |
| current_strategy_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/current_strategy_remediation_roadmap.md`，不随源码发布） | SR6-1–SR6-4 | [SYS-09](development_details.md#sys-09)、[SYS-17](development_details.md#sys-17)、[SYS-18](development_details.md#sys-18) | 隔离影子证据、连续运行与灰度 |
| position_management_plan.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/position_management_plan.md`，不随源码发布） | PM0–PM4 | [SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[SYS-13](development_details.md#sys-13)、[SYS-17](development_details.md#sys-17) | 共享持仓/预算/健康/重放，扩展目标仓位另排 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-01/B-10、A1 | [SYS-06](development_details.md#sys-06)、[SYS-08](development_details.md#sys-08) | TTL/TIF、pending和预算释放、持仓耗尽终态 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-02/B-03、A2 | [FIX-10](development_details.md#fix-10)、[FIX-13](development_details.md#fix-13)、[SYS-02](development_details.md#sys-02)、[SYS-05](development_details.md#sys-05) | 现金/杠杆/融资/资金费当前模型闭环 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-04/B-05/B-06、A3/A4/A5 | [SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[FIX-16](development_details.md#fix-16)、[VER-01](development_details.md#ver-01) | 止损和减仓按真实跳空/流动性；不采纳理想价无限强平 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-07/B-11、B1/B3 | [SYS-02](development_details.md#sys-02)、[SYS-03](development_details.md#sys-03)、[FIX-12](development_details.md#fix-12)、[FIX-19](development_details.md#fix-19)、[FIX-20](development_details.md#fix-20) | 权威交易聚合与指标接线 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-08/B-09、B2 | [SYS-03](development_details.md#sys-03)、[SYS-05](development_details.md#sys-05) | 陈旧估值明确标记；基准公共时间轴/上市前现金及不同再平衡政策分开 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-12/B-13/B-15、D1/D2 | [SYS-04](development_details.md#sys-04) | 多周期/缓存/合成压力模板/数据来源 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | B-14、B4/D5 | [FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09)、[SYS-11](development_details.md#sys-11) | 训练验证选参和最终样本独立；取消按最终OOS结果选择 |
| backtest_optimization_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） | C2/C3/C4、D3/D4 | [SYS-07](development_details.md#sys-07)、[SYS-12](development_details.md#sys-12)、[SYS-13](development_details.md#sys-13)、[SYS-15](development_details.md#sys-15) | 持仓基础与多周期、选币、图表扩展分别保留 |
| current_system_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md`，不随源码发布） | BT-01/02/03/04 | [SYS-02](development_details.md#sys-02)、[SYS-04](development_details.md#sys-04)、[SYS-06](development_details.md#sys-06)、[FIX-06](development_details.md#fix-06)、[FIX-16](development_details.md#fix-16) | 此处BT-01时序、BT-02时间轴、BT-03部分成交、BT-04成本，与实盘计划BT号不同 |
| current_system_remediation_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md`，不随源码发布） | ORD/EXCH/ARCH/DOC | [SYS-05](development_details.md#sys-05)、[SYS-06](development_details.md#sys-06)、[SYS-16](development_details.md#sys-16)、[DOC-01](development_details.md#doc-01) | 旧已完成能力保留回归，文档来源可查 |
| Phase0基线README（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/baseline/phase0/README.md`，不随源码发布） | T-0.1–T-0.5 | [SYS-01](development_details.md#sys-01)、[DOC-01](development_details.md#doc-01) | T-0.4提及原路线图工作簿未找到，登记source_missing |
| phase3_implementation.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase3_implementation.md`，不随源码发布） | T-3.1–T-3.12 | [SYS-08](development_details.md#sys-08)、[SYS-14](development_details.md#sys-14) | 当前融资/风险验收主线，新合约模式和研究候选BX |
| phase4_implementation.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase4_implementation.md`，不随源码发布） | T-4任务 | [SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[FIX-20](development_details.md#fix-20) | 路由组合/恢复/诊断 |
| README.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase5/README.md`，不随源码发布） | Phase5 | [SYS-03](development_details.md#sys-03)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11) | 指标、研究交付、准入分别判断 |
| phase6_operations.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase6_operations.md`，不随源码发布） | T-6.1–T-6.8 | [SYS-05](development_details.md#sys-05)、[SYS-16](development_details.md#sys-16)、[SYS-17](development_details.md#sys-17)、[SYS-18](development_details.md#sys-18) | 对账、运维、56日以上连续观察与灰度 |
| formula_monitoring_roadmap.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md`，不随源码发布） | FM1–FM7 | [SYS-03](development_details.md#sys-03)、[SYS-12](development_details.md#sys-12)、[SYS-13](development_details.md#sys-13)、[SYS-14](development_details.md#sys-14)、[SYS-15](development_details.md#sys-15)、[SYS-16](development_details.md#sys-16) | 公式/监控必要能力主线，扩展观察与新策略BX |
| p0_signal_observation.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p0_signal_observation.md`，不随源码发布） | 信号P0 | [SYS-09](development_details.md#sys-09)、[SYS-10](development_details.md#sys-10) | 已合入；隔离和完整交付证据复核 |
| p1_signal_meta_layer.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p1_signal_meta_layer.md`，不随源码发布） | 信号P1 | [SYS-09](development_details.md#sys-09)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11) | 已合入；有效性与正式准入未通过 |
| p23_signal_meta_layer.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p23_signal_meta_layer.md`，不随源码发布） | 信号P2/P3 | [SYS-09](development_details.md#sys-09)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11) | 本地代码保留；缺交付回执，不以弃权/等权回退作有效性 |
| p0_drawdown_recovery.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p0_drawdown_recovery.md`，不随源码发布） | 组合BLOCK_NEW恢复 | [SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09) | 继承已批准恢复/人工锁规则，禁止借整合调阈值 |
| strategy_remediation_contract_20260914.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/research/strategy_remediation_contract_20260914.md`，不随源码发布） | 9/14冻结研究契约 | [SYS-04](development_details.md#sys-04)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[SYS-10](development_details.md#sys-10)、[SYS-11](development_details.md#sys-11) | 本金/成本/风险/基准/恢复与独立验证契约优先 |
| strategy_health_contract.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_health_contract.md`，不随源码发布） | 健康状态与恢复 | [SYS-09](development_details.md#sys-09)、[FIX-15](development_details.md#fix-15)、[FIX-17](development_details.md#fix-17) | 人工锁和分级恢复迁移 |
| protective_stop_contract.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/protective_stop_contract.md`，不随源码发布） | 原仓位保护与批准风险 | [SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[FIX-10](development_details.md#fix-10)、[FIX-16](development_details.md#fix-16) | 批准预算不可增长、跳空真实成交 |
| portfolio_risk_contract.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/portfolio_risk_contract.md`，不随源码发布） | 组合预算与预留 | [SYS-08](development_details.md#sys-08)、[FIX-10](development_details.md#fix-10)、[FIX-16](development_details.md#fix-16) | 不可变预算、未知订单占用、目标减仓幂等 |
| 旧phase0_baseline.md（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/phase0_baseline.md`，不随源码发布） | 2026-07旧Phase 0 / R0 | [SYS-01](development_details.md#sys-01) | 早期基线历史参考，不对应8月T-0任务 |

## 3. 历史审查覆盖范围（仅本地映射）

历史基线共 77 条审查记录：24 条已修复回归、52 条 FIX 范围、1 条待核实。逐条邮件编号、评论标题、摘录和邮箱入口不包含在公开副本中。公开任务状态以任务登记、开发计划和详情卡为准；本节聚合数量不表示项目完成。

## 4. 全部历史来源索引

以下链接打开冻结前原文；内部相对路径保持原样，推荐通过本表或联合归档索引（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/README.md`，不随源码发布）导航。

| 原路径 | 冻结快照 |
| --- | --- |
| `docs/unified_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/unified_roadmap.md`，不随源码发布） |
| `docs/development_plan.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/development_plan.md`，不随源码发布） |
| `docs/live_trading_remediation_plan.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/live_trading_remediation_plan.md`，不随源码发布） |
| `docs/backtest_metrics_detailed_development_plan.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_metrics_detailed_development_plan.md`，不随源码发布） |
| `docs/strategy_development_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_development_roadmap.md`，不随源码发布） |
| `docs/current_strategy_remediation_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/current_strategy_remediation_roadmap.md`，不随源码发布） |
| `docs/position_management_plan.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/position_management_plan.md`，不随源码发布） |
| `docs/backtest_optimization_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/backtest_optimization_roadmap.md`，不随源码发布） |
| `docs/codex_mail_roadmap_audit_20260920.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/codex_mail_roadmap_audit_20260920.md`，不随源码发布） |
| `docs/codex_mail_findings_20260920.json` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/codex_mail_findings_20260920.json`，不随源码发布） |
| `docs/phase3_implementation.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase3_implementation.md`，不随源码发布） |
| `docs/phase4_implementation.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase4_implementation.md`，不随源码发布） |
| `docs/phase5/README.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase5/README.md`，不随源码发布） |
| `docs/phase6_operations.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/phase6_operations.md`，不随源码发布） |
| `docs/p0_signal_observation.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p0_signal_observation.md`，不随源码发布） |
| `docs/p1_signal_meta_layer.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p1_signal_meta_layer.md`，不随源码发布） |
| `docs/p23_signal_meta_layer.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p23_signal_meta_layer.md`，不随源码发布） |
| `docs/p0_drawdown_recovery.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/p0_drawdown_recovery.md`，不随源码发布） |
| `docs/research/strategy_remediation_contract_20260914.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/research/strategy_remediation_contract_20260914.md`，不随源码发布） |
| `docs/r7_sandbox_runbook.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/r7_sandbox_runbook.md`，不随源码发布） |
| `docs/baselines/batch0_fixed_baseline.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/baselines/batch0_fixed_baseline.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/backtest_metrics_development_roadmap.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/formula_monitoring_roadmap.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/phase0_baseline.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/phase0_baseline.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/phase0_baseline_results.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/phase0_baseline_results.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/project_file_and_architecture_analysis.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/README.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/README.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/roadmap.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/roadmap.md`，不随源码发布） |
| `docs/archive/2026-08-roadmap-consolidation/roadmap_detailed.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/archive/2026-08-roadmap-consolidation/roadmap_detailed.md`，不随源码发布） |
| `docs/strategy_health_contract.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/strategy_health_contract.md`，不随源码发布） |
| `docs/protective_stop_contract.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/protective_stop_contract.md`，不随源码发布） |
| `docs/portfolio_risk_contract.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/portfolio_risk_contract.md`，不随源码发布） |
| `docs/baseline/phase0/README.md` | 原文快照（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/sources/docs/baseline/phase0/README.md`，不随源码发布） |

邮件正文、逐条审计记录及邮件索引仅保存在本地受限归档中，不随源码发布。

## 5. 缺失、冲突及状态维护

- 原T-0.4提及的路线图工作簿未在本轮可访问资料中找到：source_missing。现有Markdown、合同和邮件已归集，不声称已导入该工作簿。
- 按最终OOS选参、理想止损价、无限流动性即时强平、2–4周即可正式放行等旧条款，以新Roadmap冲突表为准。
- 旧BM阶段编号变更保留映射；BM4执行质量细项不因并入成本/指标任务而丢失。
- 原9月19日研究候选前瞻协议保留独立身份；任何影响候选身份的修复按协议登记，不用新代码冒充旧冻结结果。
- 后续新增来源须保留日期与原文，再更新映射和任务；任务关闭须追加验收产物链接。归档的原始hash和审计基线不得为迁就当前状态而重写。
