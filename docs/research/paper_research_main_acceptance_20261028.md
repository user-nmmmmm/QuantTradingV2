# PR25 主线验收与交付（2026-10-28）

本轮将 PR25 隔离研究工程移植到 main `b47f66cc`，新增可信本地成本发布、原策略全 bar 签名捕获和安全 typed CLI/worker。当前工程专项已经完成；**交付 PR #120 最终提交及其合并提交的远端 CI 已全部通过**（2026-10-31 回读记录，见下节）。独立复核、研究评价和外部证据不由 CI 通过替代。

[机器回执](paper_research_main_acceptance_20261028.json) · [开发与兼容说明](paper_research_main_development_20261028.md) · [原始开发计划](paper_research_development_plan_20261025.md)

## 实现与验证

- 原主线架构、数据 adoption、冻结样本、风险与资金准入规则保留，41 个全局任务及 counts 与 base 一致；本轮只追加补充范围。
- 成本模型必须由实际本地冻结训练、内部拟合与出版收据认证。严格消费另验证实际特征观察收据和当前请求，独立 verifier 不从预测元数据自动取信任。外部 published/真实venue证据仍不由本地签名替代。
- 前向捕获在原 BacktestEngine 的 timing→market→risk→bookkeeping→terminal 区间记录真实原事件和账户，不改原signal/intent ID。默认关闭；原尾部或范围外活动被检测则阻断旧成功前缀，不自动重发或启动实盘。
- typed CLI 用版本化安全 JSON/CSV、wire SHA 与 exact/v2 身份及登记 input_bindings，将真实 DataFrame/Series 送入有界 worker；不自动 unpickle，删改输入/transport/runtime来源均拒恢复。
- 专项组有重叠，不相加作为全仓数量。完整测试、覆盖率、ML、四平台安装/回归、离线自动化及 CodeQL 以同一提交的 GitHub 运行记录为准。90% 覆盖率、质量棘轮和分层约束未降低。

首轮分支 CI（`a90f9aa`）中 8 项任务通过，quality 记录 5287 passed / 11 failed / 15 skipped，覆盖率 90.95%。后续修复脚本登记/导入、报告合同、严格类型模块登记、校准设计错误顺序和跨进程代码指纹。该失败记录保留在机器回执；修复后的远端结论仍以实际最终 Checks 为准。

留出集的执行绑定使用 `python-executable-fields/v1`，避免 `marshal` 的进程内引用状态影响代码身份。源码/默认值/全局状态及嵌套代码仍受核验；旧绑定可读取，消费时拒绝自动升级，新方案须明确新版本登记。

## 远端 CI 结果（2026-10-31 回读）

以下数字取自 GitHub check-runs 与 quality 作业日志，写入[机器回执](paper_research_main_acceptance_20261028.json)的 `final_remote_ci`：

| 提交 | 触发 | 全部 check runs | quality（Linux, Python 3.11） | 运行 |
| --- | --- | --- | --- | --- |
| `8ce579ce`（#120 最终 head） | pull_request | 成功，0 失败 | 5404 passed / 0 failed / 15 skipped，覆盖率 90.97%（门槛 90%） | [tests](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/40640792859) |
| `2d9710cc`（#120 合并提交） | push | 成功，0 失败 | 5404 passed / 0 failed / 15 skipped，覆盖率 90.97% | [tests](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/40642640561) |

check runs 包括 quality、ml-research、四个 install-smoke（Ubuntu/Windows × 3.11/3.13）、synthetic-smoke-and-regression 和 CodeQL（python、javascript-typescript）。首轮 `a90f9aa` 的 11 项失败记录继续保留在 `remote_ci_attempts`。各工作包的 reviewer 仍为 `pending_current_commit_review`。

## 未被工程通过替代的条件

main #119 已保留 288 条 Excel 元数据登记及 L02=#12/L04=#252 的题名计算映射，本轮未重新读取原工作簿；未核验的全文阅读、真实执行/费用/报价、完整历史成员/市值/细粒度来源、同机会同参与率账户经济对照、真实前向自然日与标签/费用成熟仍待证据。成本 v2 仅认证实际本地发布，不认证外部公开发布时间；前向 v2 只覆盖 flat spot，初始库存/margin/funding 未扩展。

PR25-15 条件未触发；PR25-20 不强造候选；PR25-21 未到期不裁决；PR25-22 未消费真实最终窗口。2026-10-27 b82 未发布工作树回执保留为原目录的历史，不作为当前 main 的通过依据。

## 纳入 main B1（#119）

已保留 main `b52f45d6` 的 B1 文献、基线及审计能力与历史记录。原始 23 项规划 JSON 保持 `planning_only`，本轮执行投影保留在机器回执的 `main_b1_integration` 下；41 项正式任务状态不变。当前合并后源码仍须取得自己的 CI 结果，之前提交的通过记录仅对应各自原始源码。当前运行入口及 90% 门槛见[主线开发记录](paper_research_main_development_20261028.md)。

## PR #120 审查修复

生命周期字段 `entry_blocked` / `scheduled_exit` 随 OHLCV 修订保存，并按决策 cutoff 投影；未知值阻断，后续价格修订遗漏已知 guard 明确拒绝，合格显式 False 才能解禁。旧冻结身份缺生命周期证据不能丢弃限制；纯 OHLCV 默认记录与列合同保留。

已合入 main #119 的两套审计接口与全部双方测试，并保留已登记文献事实。合并后本地关联回归 281 项通过，13 项静态/数据/前端门禁通过；最终 CI 以本 PR 最新提交的实际 Checks 为准。
