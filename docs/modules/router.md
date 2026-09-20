# router/ 模块说明

## `router/router.py` — Router

`Router` 把 `core/state.py` 产出的 `MarketState`（市场状态）按 `regime_map` 映射到具体策略名。映射必须显式传入且不能为空；[`config/params.yaml`](../../config/params.yaml) 当前映射为 `TREND_UP → TrendBreakout`，其余三个状态均为 `Cash`。`TrendBreakout` 仍是 `paused_revalidation`，真实资金准入由 `core/strategy_governance.py` 的入口检查控制。

`core/runtime.py` 的 `EventProcessor` 在每个有效标的 bar 上先调用 `process_position_management(...)`，再按账户状态决定是否调用 `collect_entry_candidate(...)`；`collect_candidate(...)` 是组合这两个步骤的兼容入口。当前没有 `Router.route()` 方法。核心逻辑：

1. 先让策略消费权威成交和平仓事件。已有仓位达到 `max_holding_days` 时提交显式 `MaxHoldingPeriod` 退出单；否则从批次账本取得开仓策略，由它执行 `process_exit_only(...)`。持仓尚在时不会产生新入场候选。
2. 空仓标的独立维护冷却截止索引。未处于冷却期且状态变化导致映射到**不同**策略时，撤销该标的旧订单、设置 `cooldowns[symbol] = i + cooldown_bars`，记录 `stop_new_entries` 后返回；`i <= cooldowns[symbol]` 时继续暂停入场。配置默认 `cooldown_bars=2`。
3. `Cash` 或映射缺失时不生成新入场候选。不同状态都映射到 `Cash` 时不算策略切换；已有仓位已在前一步接受退出管理。
4. 只有策略存在且 `state in strategy.allowed_states` 时才调用 `strategy.build_entry_candidate(...)`。候选本身不花费风险预算；同一时间戳的候选交给 `PortfolioSignalAllocator` 按分数降序、策略名和标的名排序，再执行定仓、风险检查与订单提交。
5. `save_log()`/`_log_routing` 把路由决策写入 CSV（供诊断，被 `backtest.engine` 的 `routing_log_path` 使用），包括 `route_event`、`strategy_changed` 与当前数量。

**退出边界**：当前实现没有 `_handle_switch`，不会因为映射改变而隐式提交 `StateSwitch` 强平。开仓策略可因新状态不再被允许而自行退出；保护性止损、显式最长持有期与账户级风控也可退出。普通退出订单由执行适配器撮合，回测遵循次 bar 执行，不能把提交时的参考收盘价当作已成交价。

## 与其他模块的关系

`Router` 连接状态识别、原策略持仓管理和新入场候选收集；`core/allocation.py` 统一分配组合预算，`core/risk/` 与执行适配器处理账户风险和成交事实。原开仓策略的身份来自 lot 账本，不能因当前 regime 改变而改写持仓归属。相关接口见[策略模块说明](strategies.md)和[回测假设](../backtest_assumptions.md)。
