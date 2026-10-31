# PR25 主线移植与补充开发（2026-10-28）

本次把旧分支 PR25 的 22 项非条件工程范围移植到当前 main，并增加下列三项明确的研究接口。原始 [Roadmap](paper_research_roadmap_20261025.md)、[开发与测试合同](paper_research_development_plan_20261025.md)及[23项专项登记](paper_research_task_registry_20261025.json)继续提供范围和依赖；它们不改变全项目 41 项任务的状态。

当前提交的工程与 CI 结论统一由[主线验收回执](paper_research_main_acceptance_20261028.json)记录。#120 最终提交 `8ce579ce` 与合并提交 `2d9710cc` 的远端 CI 已全部通过（quality 5404 passed、覆盖率 90.97%），数字与链接由该回执的 `final_remote_ci` 记录；独立复核仍待进行。不能将之前工作树的通过数量当作当前提交的结果。

## 来源与兼容边界

2026-10-27 的工程回执属于 `unpublished b82 worktree local evidence`：未发布工作树的源码、目录、依赖及局部诊断与当前 main 不同。旧工程验收和 implementation 机器回执保留在原工作树，本次不复制为 main 的验收。迁入的[文献原映射](paper_research_literature_map_20261027.md)和[补充阅读记录](paper_research_literature_followup_20261027.md)仅保留来源与实际阅读层级；本轮未重新取得 Excel 正文。main #119 已登记的 [288 条工作簿记录](paper_literature_registry_20261026.json)及 [B1 执行历史](paper_research_excel_integration_plan_20261026.md)一并保留：L02 对应表内 #12、L04 对应 #252，其余六篇属于原始来源层；登记的阅读层级仍以检索摘要/元数据为限。

移植保留 main 的分层包路径、数据 adoption 规则、版本及字节身份、selector 合同和快照完整性，以及原有架构、质量棘轮、AUD 和发布状态。研究数据与标签继续使用当时可得的版本；新增严格时间入口具有单独身份，不替换回顾性入口。旧默认、原策略与风控门槛继续适用。

## 合入 main B1 后的规划与执行口径

本轮纳入 main `b52f45d6`（#119）的 B1 文献登记、基线采集、搜索范围/源码闭包 API 与对应测试。20261025 Roadmap、详细计划和 23 项 JSON 保留 main 的原始规划快照，`execution_started=false` 描述该规划文件本身；先前 PR120 逐项执行维度迁入[主线机器回执](paper_research_main_acceptance_20261028.json)的 `main_b1_integration.previous_task_execution_projection`，本轮作用域仍按本文及当前提交 Checks 核对。

实际执行使用当前 `python scripts/dev.py check`：runtime 覆盖范围为 `core`、`ops`、`signal_meta`、`backtest`、`live_trading`，最低覆盖率 **90%**，并执行质量棘轮、分层、架构、文档、数据 adoption 和四平台验证。原始计划 §7.6 的 55% 与旧测试命令属于 20261025 规划快照，运行本轮验证时按上述当前统一入口执行。协方差实现位置为 `core/planning/covariance_estimators.py`。

## 本轮三项补充功能

| 范围 | 当前接口 | 行为与边界 |
| --- | --- | --- |
| 可信本地成本模型发布与消费 | [成本发布收据](../../analysis/execution_cost_publication.py)、[成本 provider](../../core/planning/execution_cost_provider.py) | 发布与特征观察收据绑定实际本地时间、内容及支持域，strict/local 消费须核对可得性；strict/published 的外部发布时间证据仍为 unsupported，不由本地收据自动升格。 |
| 原策略决策与 intent 自动捕获 | [前向策略捕获](../../research/ml_selection/forward_policy_capture.py)、[执行绑定](../../research/ml_selection/forward_execution_binding.py) | 自动保留原 Router、Strategy、RiskManager、Broker 的决策与 intent 身份，接入原 store v2 binding；不修改原事件 ID。初始持仓、margin、funding 范围未扩展，不支持的输入 fail closed。 |
| 有类型的数据 CLI 到实际 worker | [类型化输入](../../analysis/paper_research_typed_inputs.py)、[研究 CLI](../../scripts/run_paper_research_study.py) | 通过 `--data-manifest` 与 `job.input_bindings` 绑定受核验的类型化输入并交给真实 bounded worker；输入、实际传输及运行身份须一致，不能只记录元数据而继续使用演示材料。 |

其余移植范围包括研究身份/完整尝试日志、时序/成熟标签/成员区间、受控统计与最终样本访问、执行成本桥、协方差与单期组合对照、支持诊断、前向账户及有限编排。主线路径已同步到当前 `core/{foundation,market,planning,trading,analytics}`、`signal_meta` 和 `ops` 分层。

## 当前验收范围与待证据

- 工程：远端 CI 已通过（见回执 `final_remote_ci`），独立复核待进行。正反例、失败恢复、兼容、实际 worker 传输和原引擎账户链由本次验收回执逐项记录。
- 研究：本次提交不据旧诊断宣称新候选得到支持。历史搜索不完整、样本不足和未改善的结论不被工程完成覆盖。
- 外部证据：真实执行与费用、历史成员/市值、可证明发布时间、真实前向接收与成熟观察均按实际输入判断；缺失继续 pending。
- PR25-15：条件未触发，未启动多期配置。PR25-20：工具可移植，但不强造或提升候选。PR25-21：未来观察与到期裁决继续 pending。
- 准入：未消费既有真实最终样本，不授予真实资金权限，不解除原策略、健康或账户锁定。

## 全项目治理衔接

[开发计划](../development_plan.md)、[开发详情](../development_details.md)与[全项目任务登记](../development_task_registry.json)仅追加 `PR25-MAIN-PORT-20261028` 补充 scope。既有 41 项任务、阶段与 AUD/架构/发布记录保持原样；当前工作通过 [专项登记](paper_research_task_registry_20261025.json)和本次验收回执说明范围，不创建平行的全项目完成结论。
