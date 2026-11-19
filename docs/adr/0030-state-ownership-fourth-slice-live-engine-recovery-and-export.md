# 0030 状态所有权迁移的第四片：实盘引擎的对账与导出状态（B6）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B6，[ADR 0027](0027-state-ownership-first-slice-circuit-breaker.md)，[ADR 0028](0028-state-ownership-second-slice-broker-order-books.md)，[ADR 0029](0029-state-ownership-third-slice-live-account-sync.md)

## 背景
ADR 0027 把 `LiveTradingEngine` 的 `Recovery`（写 5 个宿主属性）和 `StateExport`（写 2 个）列为下一片。盘点这 7 个属性：

- `Recovery`：`_unresolved_unknown_cache`、`_last_reconciliation_at`、`_reconciliation_status`、`_last_order_sync_at`、`_account_reconciliation_persistence_failed`。
- `StateExport`：`_last_export_tick`、`_last_exported_critical_state`。

外部读写：`tick_orchestrator`（重置缓存、重写 `_reconciliation_status` 并往里加字段）、`runtime_controls`（重置缓存）、引擎自己（`_last_order_sync_at` 的读写）、`ops/operations/startup_preflight.py` 与 `run_live.py`（读）、`state_export` 读 `_reconciliation_status`、若干测试。其中 `_account_reconciliation_persistence_failed` 在引擎里**故意不初始化**，调用方一律用 `getattr(host, "_account_reconciliation_persistence_failed", False)` 探测。

## 决定
沿用前三片的三个提交：特征测试 → 机械搬动 → 文档。

- **`Recovery` 拥有对账状态**：五个属性中，四个在 `Recovery.__init__` 里用原来的初值初始化（`None`、`None`、`None`、含 `last_run_at/checked_count/discrepancy_count/ok` 的字典）；`_account_reconciliation_persistence_failed` **保持未绑定**，只在类上声明注解，`getattr(…, False)` 的探测语义不变。`recovery.py` 里 17 处 `host.<状态>` / `getattr(host, "<状态>", …)` 改为 `self.…`。
- **`StateExport` 拥有导出记账**：`_last_export_tick`、`_last_exported_critical_state` 在 `StateExport.__init__` 里初始化为 `None`；`state_export.py` 里 7 处改为 `self.…`。
- **`LiveTradingEngine` 用 `Delegated` 保留 7 个旧名字**，引擎 `__init__` 里的初始化删除。`tick_orchestrator`、`runtime_controls`、`startup_preflight`、`run_live.py` 和测试都不用改。
- 其他协作类（`TickOrchestrator`、`StateExport`）对 `_reconciliation_status`、`_account_reconciliation_persistence_failed` 的读写仍经由 `host.<旧名字>`，由描述符转发给 `Recovery`。

## 备选方案
- 把 `_account_reconciliation_persistence_failed` 初始化为 `False`：`hasattr`/`vars` 等读法会变，并且要同时审查所有探测点；这一片不改行为。
- 把 `_reconciliation_status` 留在宿主：它由 `Recovery` 整体重写、`TickOrchestrator` 局部更新，所有者应是 `Recovery`；`TickOrchestrator` 的局部更新留到它自己的那一片再决定要不要改成 `Recovery` 的方法。

## 后果
- 金样本测试（`tests/test_live_engine_state_ownership.py`：新建、对账、未到期、导出、持久化失败五步，每步固定七个属性与状态字典的关键字段）一字不改地通过；新增断言状态在协作类里、`vars(engine)` 里没有这些名字。
- 浅拷贝仍有 ADR 0018 里说的问题，对账与导出状态现在在协作类里，浅拷贝的引擎会共享它。仓库里没有这样的用法；需要复制时必须 `deepcopy`。
- 剩下的最后一片是 `TickOrchestrator`（16 个属性），它在实盘主循环上、外部读者最多，需要单独盘点后再做。
