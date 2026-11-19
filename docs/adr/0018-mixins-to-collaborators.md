# 0018 四个宿主类的 mixin 改为持有 host 引用的协作类（B4）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B4，[ADR 0005](0005-persisted-event-type-names.md)，[ADR 0002](0002-quality-ratchet.md)

## 背景
`Broker`（4 个 mixin）、`LiveBroker`（3）、`RiskManager`（3）、`LiveTradingEngine`（3，其中 `TickOrchestrator` 又继承 1 个）共 13 个 mixin、约 6300 行。每个 mixin 通过 `self` 隐式读写宿主的 10–60 个属性，所以耦合不可见，mypy 无法检查（475 项 `attr-defined`），测试也要靠"把 mixin 当基类"才能搭替身。代码里原有的说明把完整的组合重构（各自拥有状态的独立对象）列为"需要单独一轮"的工作，因为这些类靠近资金路径，而且外部代码（回测引擎、实盘引擎、状态导出与恢复、测试）直接读写宿主上的几十个属性。

## 决定
- 13 个 mixin 全部改成**普通协作类**，不再被宿主继承：`CircuitBreaker`、`PositionSizing`、`EntryPolicy`（`RiskManager`），`Matching`、`FillService`、`Financing`、`Liquidation`（`Broker`），`SubmissionService`、`OrderReconciler`、`AccountSync`（`LiveBroker`），`TickOrchestrator`、`PortfolioRiskActions`、`Recovery`、`StateExport`（`LiveTradingEngine`）。类名去掉 `Mixin`，仍在原文件里。
- 协作类持有 `host` 引用；方法体里原来的 `self.x` **机械地**改成 `host.x`（`staticmethod`/`classmethod` 不变）。**所有回调都经过宿主自己的方法**，所以子类覆盖和对宿主实例的 `patch.object` 仍然生效，行为与原来逐行等价。
- 宿主用 `functools.cached_property` **惰性**创建协作类（`_circuit_breaker` 等），并为每个方法保留**同签名**的转发方法（含 `@property`、`@staticmethod`、`@classmethod`）：外部调用点一个都不用改。惰性创建是为了让跳过 `__init__` 的对象（测试里的 `object.__new__(LiveBroker)`）继续可用。
- 转换由脚本生成并逐宿主验证（相关测试子集 → 完整测试），不手改方法体；生成的转发方法保留了类型注解，宿主属性补上了缺失的类型。
- 协作类用 `if TYPE_CHECKING:` 导入宿主类来标注 `host` 的类型。`tests/test_architecture_boundaries.py` 的"成员不得导入自己的门面包"规则防的是运行时循环导入，因此收窄为忽略 `TYPE_CHECKING` 块里的导入（这些导入不会执行）。
- 3 个把 `TickOrchestratorMixin` 当替身基类的测试改为继承测试辅助类 `tests/collaborator_host.py::TickOrchestratorHost`：它只把方法名转发给 `TickOrchestrator` 和 `PortfolioRiskActions`，与原来的 mixin 替身暴露完全相同的方法集合。**不能**直接继承 `LiveTradingEngine`：代码里有 `getattr(host, "_ensure_state_store", None)` 这类探测，多出来的方法会改变行为（第一版就因此让 4 个生命周期测试失败）。1 个把 `OrderReconcilerMixin._persist_fills` 当函数用的测试改为 `OrderReconciler(stub)._persist_fills(...)`。
- **状态仍由宿主持有**：协作类通过 `host` 读写，属性没有搬进协作类。这是有意的第一步：外部代码、状态导出（`state_export`）、恢复（`recovery`）和测试直接读写这些属性，把"状态所有权"迁到协作类需要逐个属性设计并改动外部调用点，应作为单独的、可回滚的后续步骤。
- 不在 B4 范围：报告生成的 4 个 mixin（`backtest/reporting/`，部分被当作无状态方法包单独实例化）和策略里的 3 个私有 mixin（`strategies/trend_breakout.py`）。

## 备选方案
- 一次性把状态也搬进协作类：改动面是 13 个类 × 几十个属性 × 全部外部读写点，且包含实盘下单与对账路径，无法在不引入行为风险的前提下一次评审完。
- 保持继承、只补类型声明：能减少 mypy 报错，但耦合仍然隐式，替身测试仍要靠继承。
- 用 `__getattr__` 或装饰器在运行时生成转发：mypy 与 IDE 看不到方法，丢掉可检查性。

## 后果
- mypy 存量 **869 → 404**（`attr-defined` 475 → 49），基线随之锁定；ruff 仍是 621。
- 协作类对宿主的依赖现在可以在每个文件里看到（`host.<属性>`），为之后迁移状态所有权提供了清单。
- 代价：宿主多了约 100 个转发方法；每次调用多一层转发。已用回测基线（等价性）与实盘相关测试验证行为不变。
- 注意：协作类被惰性缓存在宿主 `__dict__` 里，**对宿主做浅拷贝会让副本共用指向原宿主的协作类**。仓库里没有这样的用法；需要复制时应使用 `deepcopy`（会连同协作类一起复制并重定向 `host`）。
- 仍未做：状态所有权迁移；报告与策略 mixin。
