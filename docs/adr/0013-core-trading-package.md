# 0013 订单、保护单、账户与状态持久化移到 `core/trading/`（B2 第 2 簇）

- 状态：已采纳
- 日期：2026-10-12
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B2，[ADR 0012](0012-core-market-package.md)，[ADR 0005](0005-persisted-event-type-names.md)

## 背景
B2 第 1 簇（行情）之后，`core/` 根还有 46 个平铺模块。订单、保护单、持仓批次、账户事实、状态存储这一组模块彼此交叉引用最多，被 `backtest`、`live_trading`、`composition`、`strategies`、`router`、`scripts` 和测试共同使用；已有的 `core/broker/`、`core/live_broker/`、`core/risk/`、`core/events/` 子包是它们的使用方，而不是它们的成员。

## 决定
- 新建子包 `core/trading/`（仍属 `core` 层；`__init__.py` 为空），搬入 20 个模块，**模块名不变**：`orders`、`order_lifecycle`、`order_store`、`order_latency`、`execution_port`、`protective_orders`、`protective_stops`、`lots`、`accounts`、`account_source`、`account_reconciliation`、`account_cost_contract`、`margin_rebalance`、`position_management`、`entry_audit`、`entry_risk`、`state`、`state_store_v2`、`sqlite_utils`、`sqlite_backup`。`core.orders` → `core.trading.orders`。
- 不留垫片；所有导入点（含 `patch("…")` 字符串、子进程命令里的 `-m core.sqlite_backup` 与 `-c "from core.state_store_v2 import …"`）一次性改写，运行手册同步。
- **持久化格式不变**：搬迁前检查了 `__module__`、pickle、动态导入；事件类型名按 ADR 0005 是显式常量，不依赖模块路径；状态/订单存储的表结构与键未改动，现有测试覆盖读写与恢复。
- 冻结的历史证据不改写。

## 备选方案
- 拆成"订单"和"账户/状态"两个包：二者互相引用（`order_store` ↔ `state_store_v2` ↔ `accounts`），拆开会立刻产生包间环；先合在一起。
- 取名 `orders`：与现有模块 `orders.py` 重名，且范围不止订单，所以用 `trading`。

## 后果
- 行为身份报告显示为"移动 20 个文件、若干文件仅导入变化"。
- 运行手册里的命令变为 `python -m core.trading.sqlite_backup …`。
- 这些模块的 logger 名（`__name__`）随路径变化。
