# PR25 主线验收与交付（2026-10-28）

本轮将 PR25 隔离研究工程移植到 main `b47f66cc`，新增可信本地成本发布、原策略全 bar 签名捕获和安全 typed CLI/worker。当前工程专项已经完成；**同一提交的远端 CI 尚待取得，本文件不宣称已全绿**。最终结果以交付 PR 的实际 Checks 和交付回执为准。

[机器回执](paper_research_main_acceptance_20261028.json) · [开发与兼容说明](paper_research_main_development_20261028.md) · [原始开发计划](paper_research_development_plan_20261025.md)

## 实现与验证

- 原主线架构、数据 adoption、冻结样本、风险与资金准入规则保留，41 个全局任务及 counts 与 base 一致；本轮只追加补充范围。
- 成本模型必须由实际本地冻结训练、内部拟合与出版收据认证。严格消费另验证实际特征观察收据和当前请求，独立 verifier 不从预测元数据自动取信任。外部 published/真实venue证据仍不由本地签名替代。
- 前向捕获在原 BacktestEngine 的 timing→market→risk→bookkeeping→terminal 区间记录真实原事件和账户，不改原signal/intent ID。默认关闭；原尾部或范围外活动被检测则阻断旧成功前缀，不自动重发或启动实盘。
- typed CLI 用版本化安全 JSON/CSV、wire SHA 与 exact/v2 身份及登记 input_bindings，将真实 DataFrame/Series 送入有界 worker；不自动 unpickle，删改输入/transport/runtime来源均拒恢复。
- 专项组有重叠，不相加作为全仓数量。完整测试、覆盖率、ML、四平台安装/回归、离线自动化及 CodeQL 以同一提交的 GitHub 运行记录为准。90% 覆盖率、质量棘轮和分层约束未降低。

首轮分支 CI（`a90f9aa`）中 8 项任务通过，quality 记录 5287 passed / 11 failed / 15 skipped，覆盖率 90.95%。后续修复脚本登记/导入、报告合同、严格类型模块登记、校准设计错误顺序和跨进程代码指纹。该失败记录保留在机器回执；修复后的远端结论仍以实际最终 Checks 为准。

留出集的执行绑定使用 `python-executable-fields/v1`，避免 `marshal` 的进程内引用状态影响代码身份。源码/默认值/全局状态及嵌套代码仍受核验；旧绑定可读取，消费时拒绝自动升级，新方案须明确新版本登记。

## 未被工程通过替代的条件

Excel 原始映射、真实执行/费用/报价、完整历史成员/市值/细粒度来源、同机会同参与率账户经济对照、真实前向自然日与标签/费用成熟仍待证据。成本 v2 仅认证实际本地发布，不认证外部公开发布时间；前向 v2 只覆盖 flat spot，初始库存/margin/funding 未扩展。

PR25-15 条件未触发；PR25-20 不强造候选；PR25-21 未到期不裁决；PR25-22 未消费真实最终窗口。2026-10-27 b82 未发布工作树回执保留为原目录的历史，不作为当前 main 的通过依据。
