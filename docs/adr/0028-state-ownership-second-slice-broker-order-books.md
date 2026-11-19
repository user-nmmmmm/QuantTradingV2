# 0028 状态所有权迁移的第二片：模拟撮合器的订单簿与风控动作目标（B6）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B6，[ADR 0027](0027-state-ownership-first-slice-circuit-breaker.md)，[ADR 0018](0018-mixins-to-collaborators.md)

## 背景
ADR 0027 给出了迁移方式和下一片的顺序：`Broker` 的 `Matching` 与 `Liquidation`（各写 3 个宿主属性）。盘点这 3 个属性：

- `pending_orders`、`active_orders`：由 `Matching` 读写（`process_orders`、取消类方法、`submit_order`），`Liquidation` 在强制减仓时过滤它们；回测引擎、保护单、回撤预算、选币研究管线和数十个测试文件直接读它们。
- `risk_action_targets`：只有 `Liquidation.force_liquidate` 读写；原来是"首次使用时创建、用 `getattr(host, …, None)` 探测"的惰性属性，类上只有一个注解。

## 决定
沿用 ADR 0027 的三个提交：先在迁移前的代码上录特征测试，再机械搬动，最后写文档。

- **`Matching` 拥有订单簿**：`pending_orders`、`active_orders` 在 `Matching.__init__` 里初始化，`matching.py` 里 30 处 `host.pending_orders`/`host.active_orders` 改为 `self.…`。`Broker.__init__` 里的两行初始化删除。
- **`Liquidation` 拥有 `risk_action_targets`**：在 `Liquidation.__init__` 里初始化为空字典，`force_liquidate` 里的探测与惰性创建删除。可观察的差别只有一个：`Broker.risk_action_targets` 在首次使用前读到的是 `{}`，而不是 `AttributeError`；仓库里没有依赖后者的代码，测试用 `not getattr(…, None)` 固定"无内容"。
- **`Broker` 用 `Delegated` 保留三个旧名字**（`core/risk/delegation.py`，ADR 0027），外部读写不变。
- `Liquidation` 过滤订单簿时仍通过 `host.pending_orders`/`host.active_orders`：订单簿的所有者是 `Matching`，`Liquidation` 是它的使用者，经由宿主的旧名字读写，与 `FillService` 的做法一致。
- 仍留在宿主上的：`orders_by_id`、`opening_orders`（`FillService`、`Matching` 共用）、`execution_audit`、`trades`、`close_events` 等。它们被两个以上协作类写入，需要单独设计归属，不属于这一片。

## 备选方案
- 同时迁移 `orders_by_id`/`opening_orders`：`Matching` 与 `FillService` 都写，要先决定谁拥有；与订单簿的搬动混在一起评审不了。
- 保留 `risk_action_targets` 的惰性创建：为了保留 `AttributeError` 这一个没有人依赖的行为，要在协作类里留一个"可能不存在"的属性，类型也只能是 `Any`。

## 后果
- 金样本测试（`tests/test_broker_state_ownership.py`：订单从提交、进入撮合、取消保护单/开仓单/按标的取消，以及风控动作两次调用的目标状态）一字不改地通过；新增一条断言状态在协作类里、宿主 `vars()` 里没有这三个名字。
- `vars(broker)` 不再列出这三个名字。`research/ml_selection/batch_comparison.py` 对 `SimulatedExecutionAdapter.__dict__` 做 `deepcopy`（带 `memo`），被缓存的 `_matching`/`_liquidation` 随宿主一起复制，且其 `_host` 经 `memo` 重定向到副本，所以订单簿被一起复制；该模块的测试在完整测试里通过。
- 浅拷贝仍有 ADR 0018 里说的问题，现在订单簿也在协作类里，浅拷贝的 `Broker` 会共享订单簿。仓库里没有浅拷贝 `Broker` 的用法；需要复制时必须 `deepcopy`。
- 下一片的候选：`LiveBroker` 的 `AccountSync`（8）、`LiveTradingEngine` 的 `Recovery`（5）与 `StateExport`（2）；`TickOrchestrator`（16）最后。
