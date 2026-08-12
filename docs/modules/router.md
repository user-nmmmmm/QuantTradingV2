# router/ 模块说明

## `router/router.py` — Router

`Router` 把 `core/state.py` 产出的 `MarketState`（市场状态）按 `regime_map` 映射到具体策略名。默认映射：`TREND_UP → TrendUp`、`TREND_DOWN → TrendDown`、`SIDEWAYS → RangeMeanReversion`、`VOLATILE → Cash`。

`route(symbol, i, df, state, portfolio, broker, risk_manager, current_prices)` 由 `EventProcessor.process_symbol` 每根 bar、每个标的调用一次。核心逻辑：

1. 每个标的维护独立的冷却计数器，策略切换后处于冷却期。
2. 当 regime 变化导致映射到**不同**策略时，调用 `_handle_switch`：撤销该标的的挂单、以当前收盘价强制平掉任何持仓（`submit_order(..., exit_reason="StateSwitch")`）、清空旧策略上下文、设置 `cooldown_bars` 冷却窗口——而不是直接切去调用新策略。
3. `"Cash"` regime 或映射缺失时是空操作（保持空仓）。
4. 只有当 `state in strategy.allowed_states` 时才会调用 `strategy.on_bar(...)`（与 `regime_map` 形成双重保险）。
5. `save_log()`/`_log_routing` 把每根 bar 的路由决策写入 CSV（供诊断，被 `backtest.engine` 的 `routing_log_path` 使用）。

**需要注意的行为**：策略切换会在**当前 bar 收盘价**强制离场，而不是交给策略自身的出场逻辑处理——这是无条件的、由 regime 驱动的平仓，不受策略本身的止损/止盈规则影响。

## 与其他模块的关系

`Router` 是 `core/state.py`（状态识别）和 `strategies/`（策略执行）之间的调度层，同时也是策略切换时风险处置（撤单+强平+冷却）的唯一执行点——这部分风险管理被特意从各个策略自身的逻辑中解耦出来，统一由 `Router` 负责。
