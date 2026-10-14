# 0027 状态所有权迁移的第一片：熔断器（B6）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B6，[ADR 0018](0018-mixins-to-collaborators.md)，[ADR 0022](0022-mypy-hard-gate-list.md)

## 背景
ADR 0018 把四个宿主类的 mixin 改成了协作类，但**状态仍由宿主持有**，协作类通过 `host.<属性>` 读写。2026-10-14 盘点各协作类写入宿主的属性数：`CircuitBreaker` 13、`PositionSizing` 1、`Matching` 3、`FillService` 1、`Liquidation` 3、`AccountSync` 8、`OrderReconciler` 1、`TickOrchestrator` 16、`Recovery` 5、`StateExport` 2、`PortfolioRiskActions` 1。外部代码（状态导出、恢复、两个引擎、前向桥、测试）直接读写其中许多属性，所以不能一次性全部搬走。

`CircuitBreaker` 的 13 个属性是最合适的第一片：全部由这一个协作类写入；有 `breaker_checkpoint()`/`restore_breaker_checkpoint()` 这个现成的、覆盖全部 13 个属性的持久化观察面；行为已被大量测试覆盖。

## 决定
- **迁移方式（以后每一片沿用）：先固定行为，再机械搬动，最后写文档，分三个提交。**
  1. 在**迁移前**的代码上录制特征测试（`tests/test_breaker_state_ownership.py`）：三条脚本化路径（逐级升级到锁定、日内亏损、冷静期 + 试用期 + 恢复）在每一步后的检查点（13 个属性）和决策，作为金样本；再加上"每个旧名字在 `RiskManager` 上仍能读写"、检查点往返、新实例默认值。
  2. 把 13 个属性的初始化搬进 `CircuitBreaker.__init__`，`circuit_breaker.py` 里的 `host.<状态>` 改读 `self.<状态>`（112 处，正则机械替换）。上一步的测试**一字不改**地通过。
  3. 文档、基线、清单。
- **宿主保留全部旧名字，用一个小描述符 `Delegated`**（`core/risk/delegation.py`）把读写转发给协作类：`blocked_until = Delegated[Optional[pd.Timestamp]]("_circuit_breaker")`。外部约 40 处引用一个都不用改，持久化格式不变，类型由泛型参数给出。
- 仍留在宿主上的：`breaker_audit`（列表，被多处共享引用）、`PositionSizing` 的 `_last_entry_context`、配置类属性。它们不属于这一片。
- 新增测试 `test_the_breaker_collaborator_is_where_the_state_lives`：断言值存放在协作类里、宿主实例的 `vars()` 里没有这些名字，防止以后又被放回宿主。

## 备选方案
- 给 13 个属性各写一对 `@property`：约 120 行样板，类型要逐个写；描述符一处定义、泛型参数给类型。
- 直接让外部代码改读 `risk._circuit_breaker.x`：约 40 处调用点同时改动，包含状态导出与恢复，一次评审不了；代理名字可以在后续逐步淘汰。
- 把整个熔断器抽成独立对象并从 `RiskManager` 移除这些名字：同上，且会破坏外部契约。

## 后果
- `core/risk/circuit_breaker.py` 与 `delegation.py` 零 mypy 发现，进入硬门槛（136 → 138）；mypy 存量 302 → 301；函数长度基线里 `check_circuit_breaker` 略有缩短。
- `vars(risk_manager)` 不再列出这 13 个名字。仓库里没有依赖它的代码（已核对 `__dict__`/`vars`/`deepcopy` 的使用）；`unittest.mock.patch.object(risk, "<这些名字>")` 在退出时会对描述符做 `delattr`，描述符没有 `__delete__`，所以不能这样用；目前没有这样的测试。
- 浅拷贝仍有 ADR 0018 里说的问题（协作类缓存在宿主 `__dict__` 里）；现在状态也在协作类里，所以浅拷贝的副本会**共享熔断状态**。仓库里没有浅拷贝风险管理器的用法；需要复制时必须 `deepcopy`。
- 下一片的候选，按"写入属性少、外部读者少"排序：`Matching`/`Liquidation`（`Broker`，各 3）、`AccountSync`（`LiveBroker`，8）、`Recovery`/`StateExport`（`LiveTradingEngine`，5 与 2）。`TickOrchestrator` 的 16 个属性最后做，因为它在实盘主循环上，且外部读者最多。
