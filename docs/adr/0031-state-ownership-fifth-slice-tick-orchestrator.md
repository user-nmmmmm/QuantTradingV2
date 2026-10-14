# 0031 状态所有权迁移的第五片：实盘 tick 循环自己的记账（B6）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B6，[ADR 0027](0027-state-ownership-first-slice-circuit-breaker.md)，[ADR 0030](0030-state-ownership-fourth-slice-live-engine-recovery-and-export.md)

## 背景
ADR 0027 盘点时 `TickOrchestrator` 写 16 个宿主属性，计划最后做。这一片先逐个核对"谁在写"。结论是它们分两类：

- **只有 `TickOrchestrator` 写**（引擎 `__init__` 里的初始值除外）：`_tick_count`、`_consecutive_tick_crashes`、`_next_retry_delay`、`_consecutive_strategy_failures`、`_last_strategy_error`、`_last_tick_seconds`、`_alerted_health_transitions`、`_live_fill_risk_audit`。外部只有读：引擎主循环读 `_next_retry_delay`，`StateExport` 读计数与错误并导出，若干测试读写。
- **引擎、`runtime_controls`、状态导出等也在写**：`_operational_state`（另有 8 处写入）、`_healthy`（3 处）、`data_map`（23 个测试文件、52 处引用）、`_snapshot`、`_last_account_sync_at`、`_protective_order_manager`。`TickOrchestrator` 只是它们的写入者之一，不是所有者。
- 另有两个已在前几片归属别处：`_reconciliation_status`、`_unresolved_unknown_cache`（`Recovery`，ADR 0030）。

## 决定
- **搬第一类的 8 个**：它们在 `TickOrchestrator.__init__` 里初始化（`0`、`0`、`0.0`、`0`、`None`、`None`、`set()`、`[]`），`tick_orchestrator.py` 里 18 处 `host.<状态>` 改为 `self.…`。`LiveTradingEngine` 用 `Delegated` 保留 8 个旧名字，引擎 `__init__` 里的初始化删除。
- **不搬第二类**：共享的引擎级状态留在引擎上。把它们搬进某个协作类只会让其他写入者变成"跨协作类写入"，没有减少耦合；它们属于引擎本身，路线图的退出条件（"`host.<属性>` 引用显著减少"）靠前几片和这一片已经达成大部分，剩下的不是协作类的状态。
- **测试替身 `tests/collaborator_host.py::TickOrchestratorHost` 同步声明同样的 8 个 `Delegated` 名字**：否则替身上直接设置 `_live_fill_risk_audit` 之类会写到替身自己的实例属性，协作类看不到（ADR 0018 的教训：替身必须暴露与引擎完全一致的名字）。
- **一处可观察差别**：`_alerted_health_transitions` 以前在第一次健康状态告警时才惰性创建，现在从构造起就是空集合；`_live_fill_risk_audit` 在替身上以前也是惰性创建。仓库里只有 `TickOrchestrator` 自己读写它们，金样本测试把"未创建"与"空"视为同一行。

## 备选方案
- 把 16 个全部搬进 `TickOrchestrator`：共享状态会变成"协作类持有、引擎和其他协作类通过描述符写入"，所有权反而更模糊，并且 `data_map` 的 52 处引用没有一个是 `TickOrchestrator` 独有的。
- 把第二类做成只读代理：引擎自己和 `runtime_controls` 都要写，不可行。

## 后果
- 金样本测试（`tests/test_tick_state_ownership.py`：两次策略失败的 tick、两次崩溃退避后恢复的 tick，每步固定 8 个属性与健康/运行状态）一字不改地通过；新增断言：状态在协作类里、`vars(engine)` 里没有这 8 个名字、替身转发同样的名字。
- 浅拷贝仍有 ADR 0018 里说的问题；仓库里没有这样的用法，需要复制时必须 `deepcopy`。
- **B6 到此收尾**：五片共迁移 13 + 3 + 8 + 7 + 8 = 39 个属性；协作类的状态所有权已经全部落实，剩下留在宿主上的是被多个写入者共享的引擎级状态（`_operational_state`、`_healthy`、`data_map`、`_snapshot`、`_last_account_sync_at`、`_protective_order_manager`，以及 `Broker` 的 `orders_by_id`/`opening_orders`/`execution_audit` 等），它们的归属是下一个层次的设计问题，不再作为 B6 的待办。
