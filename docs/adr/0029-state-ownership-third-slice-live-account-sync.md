# 0029 状态所有权迁移的第三片：实盘账户同步状态（B6）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B6，[ADR 0027](0027-state-ownership-first-slice-circuit-breaker.md)，[ADR 0028](0028-state-ownership-second-slice-broker-order-books.md)

## 背景
ADR 0027 的下一片候选是 `LiveBroker` 的 `AccountSync`（写 8 个宿主属性）。这 8 个属性全部只由 `AccountSync` 写入：

- `_account_source`、`_account_source_environment`、`_account_source_maximum_age`、`account_reconciliation_report`：由 `configure_account_source()` 绑定；`account_snapshot`、`account_balance_facts`：由 `sync()` 绑定。这六个在 `LiveBroker.__init__` 里**故意不初始化**，调用方用 `getattr(host, "_account_source", None)` 探测"是否已配置"，类上只有注解。
- `unowned_positions`、`last_account_sync_at`：在 `LiveBroker.__init__` 里初始化为 `{}` 和 `None`，`sync()` 更新。

外部读者：`live_trading/recovery.py`（读写 `account_reconciliation_report`）、`live_trading/tick_orchestrator.py`（读 `account_reconciliation_report`、`unowned_positions`）、若干测试。

## 决定
沿用 ADR 0027/0028 的三个提交：特征测试 → 机械搬动 → 文档。

- `AccountSync` 拥有这 8 个属性：`unowned_positions`、`last_account_sync_at` 在 `AccountSync.__init__` 里初始化；其余六个**保持未绑定**，只在类上声明注解，所以 `getattr(host, "_account_source", None)` 的探测语义不变（描述符转发时 `AttributeError` 照常传出，`getattr` 的默认值生效）。
- `account_sync.py` 里 24 处 `host.<状态>` / `getattr(host, "<状态>", …)` 机械改为 `self.<状态>`。
- `LiveBroker` 用 `Delegated` 保留 8 个旧名字（描述符见 ADR 0027），外部读写不变；`__init__` 里两行初始化删除。
- 金样本测试（`tests/test_live_broker_state_ownership.py`）覆盖四步：新建、配置账户来源、同步成功、余额漂移后同步失败，每步固定八个属性的值（含"未绑定"）。

## 备选方案
- 把六个"未绑定"属性初始化为 `None`：会让 `getattr(host, "_account_source", None)` 之外的读法行为改变（例如 `hasattr`），并且需要同时修改调用方的探测；这一片不改行为。

## 后果
- `vars(live_broker)` 不再列出这 8 个名字；仓库里没有依赖它的代码。
- 浅拷贝仍有 ADR 0018 里说的问题，账户同步状态现在在协作类里，浅拷贝的 `LiveBroker` 会共享它。仓库里没有这样的用法；需要复制时必须 `deepcopy`。
- 下一片的候选：`LiveTradingEngine` 的 `Recovery`（5）与 `StateExport`（2）；`TickOrchestrator`（16）最后。
