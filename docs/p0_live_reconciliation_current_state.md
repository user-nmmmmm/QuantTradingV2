# P0 实盘订单对账现状

## 当前触发点

- `LiveTradingEngine.initialize()` 调用 `_recover_orders()`，后者转发到
  `LiveBroker.recover_open_orders()`，逐笔对账账本中的非终态订单。这是进程启动后的恢复路径。
- 整改前的 `LiveTradingEngine._tick()` 也在每轮开头调用 `_recover_orders()`；因此它实际上会逐笔
  对账所有非终态订单，但与交易 tick 强耦合，无法表达独立周期、统计对账差异，也无法在状态文件中
  暴露最近一次对账结果。
- `LiveBroker.submit_order()` 遇到相同的幂等订单意图时，会对已经尝试提交的记录调用
  `reconcile_order()`；这是由相同信号再次触发的被动路径。
- `LiveBroker.cancel_order()` 在撤单异常或竞态时调用 `reconcile_order()`；这是撤单事实不确定时的
  被动路径。
- `LiveBroker.sync()` 只刷新现金和仓位，不核对 `OrderStore` 中的订单状态。

## 交易所外部仓位变化的发现时间

如果用户在交易所 App 手工平仓，系统不会通过订单账本知道这笔外部订单。它会在下一次成功执行
`broker.sync()` 时，通过 `fetch_balance()` / `fetch_positions()` 覆盖本地现金与仓位后发现变化。
整改前该调用发生在 `initialize()` 和每次 `_tick()` 的数据刷新之后，因此发现延迟取决于主循环间隔、
网络可用性以及该 tick 是否正常执行；它并不依赖相关 symbol 再次产生交易信号。外部订单本身不会被
写入本地 `OrderStore`，只能反映为仓位事实变化。

## 周期性订单对账的落点

新增的周期对账应位于 `LiveTradingEngine._tick()` 中 `broker.sync()` 成功之后、逐 symbol 处理之前。
该位置独立于 bar 路由：按配置间隔遍历 `order_store.list_non_terminal()` 并调用
`reconcile_order()`，随后立即复用 `_has_unresolved_unknown()` 的 fail-closed 逻辑。启动恢复仍只在
`initialize()` 执行，避免它绕过周期计时器。
