# 0005 事件类型的持久化名称是线上格式

- 状态：已采纳
- 日期：2026-10-11
- 相关：[PR #66](https://github.com/user-nmmmmm/QuantTradingV2/pull/66)，`core/events/codec.py`，`tests/test_event_wire_compat.py`

## 背景
事件存储把 dataclass 事件序列化为 `{"class": "<module>:<qualname>"}`，解码时按名称查表，幂等校验也会规范化这些名称。`CashEvent` / `MarkPriceEvent` 最初定义在 `research.audit.ledger`，所以存储里的名称是 `research.audit.ledger:CashEvent`。把类移动到 `core`（为消除 `core → research`）若让名称随 `__module__` 变化，就会改变新写入的线上格式。

## 决定
- 类的**定义位置**与**持久化名称**解耦：`codec.py` 用显式的 `_PERSISTED_NAMES` 保持历史名称；仍接受 `core.ledger:*` 旧名称，未注册名称一律拒绝。
- 移动前先在未改动的代码上写锁定格式的测试并确认通过，移动后再次通过。
- 以后移动或重命名任何会被事件存储序列化的类型，都要沿用这个做法，或提供迁移。

## 备选方案
- 让名称跟随新位置并写迁移：需要改写已存事件或双写，风险和成本更高。
- 伪造 `__module__`：隐式且容易被后续重构破坏。

## 后果
core 里保留了一个指向 `research.audit.ledger` 的历史字符串，这是有意的；它只是数据，不构成导入依赖。
