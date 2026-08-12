# strategies/ 模块说明

策略是可插拔的信号生成器，回测和实盘共用同一套实现。

## `strategies/base.py` — Strategy 基类

`Strategy(ABC)` 是所有策略实现的接口。抽象方法 `should_enter`/`should_exit` 返回 `None` 或一个信号字典（`action`、可选 `stop_loss`/`order_type`/`price`/`reason`）。

具体的 `on_bar(...)` 是被 `Router.route` 调用的统一编排逻辑：
1. 若已有持仓且不是刚开仓（`not just_entered`），检查出场信号并提交平仓单；只有在 `submission.accepted` 时才清空 `context[symbol]`。
2. 若空仓且 `state in allowed_states`，检查入场信号；若给出 `stop_loss>0` 则用 `risk_manager.calculate_position_size` 计算数量，否则用 `calculate_position_size_fixed_pct`（默认 10% 仓位）兜底；经 `risk_manager.check_entry_risk` 预检（若 broker 暴露了 `pending_open_notional` 会一并考虑）；提交订单，**只有被接受时**才初始化 `context[symbol]`（`stop_loss`/`entry_price`/`trailing_stop`/`entry_bar`）。

**关键不变量——一根 bar 的出场冷却**：`just_entered = i <= entry_bar + 1`，防止在刚开仓的那根 bar 上就检查出场。这与"下一 bar 执行"模型相关：信号在 bar N 产生，成交在 N+1 的开盘，`on_bar` 会在 N+1 再次运行——此时绝不能立刻平掉刚开的仓位。

## `strategies/mean_reversion.py` — RangeStrategy

注册名 `"RangeMeanReversion"`，只在 `SIDEWAYS` 状态生效。当 `low <= 布林带下轨`（买入）或 `high >= 布林带上轨`（做空）时入场，用 ATR/价格 波动率上限（`atr_threshold_pct`，默认 3%）过滤；止损为 `±1×ATR`。回归到布林带中轨或触及止损（用 bar 的 low/high 检测盘中触及，而非收盘价）时出场。重写了 `on_bar` 以额外追踪 `trade_state`（连续亏损/冷却）：连续 3 笔亏损后强制 24 根 bar 冷却期，期间 `should_enter` 直接返回 `None`。

**代码质量提示**：`should_exit` 中第一个 `return None` 之后存在一段无法到达的重复止损代码块，属于历史遗留死代码，不是当前生效行为，文档中特此标注以免误读。

## `strategies/trend_breakout.py` — TrendBreakoutStrategy / TrendBreakdownStrategy

`TrendBreakoutStrategy`（`"TrendBreakout"`，允许在 `TREND_UP`/`VOLATILE`）与镜像的 `TrendBreakdownStrategy`（`"TrendBreakdown"`，`TREND_DOWN`）：唐奇安通道突破/破位系统。入场：收盘价突破 `shift(1)` 滞后的 N 根 bar 滚动高/低点（默认 `entry_window=20`）；止损用出场窗口（`exit_window=10`）的唐奇安出场位，无效时回退 5%。出场：价格跌回出场窗口极值，或 regime 不再被允许。

两个类都实现了**健康闸门（"Alpha Death"）**：`check_health()` 在 `连续亏损 > 5` 或最近 20 笔记录交易的平均盈亏为负时，**永久**禁用后续入场（`is_alive=False`）——这是一个不会自动重置的单实例级"死亡开关"。`_record_trade_result` 根据 `context["entry_price"]` 与出场价计算盈亏。

## `strategies/trend_following.py` — TrendUpStrategy / TrendDownStrategy

`TrendUpStrategy`（仅 `TREND_UP`）/`TrendDownStrategy`（仅 `TREND_DOWN`）：基于 SMA 的回调/反弹入场策略，用斜率和反转 K 线确认，ATR 初始止损（`atr_multiplier=2.5`）+ 单调收紧的移动止损（只收紧不放松，用 bar 的 low/high 检测盘中触发）。若 `state` 脱离 `allowed_states` 也会防御性出场。代码注释显示这些倍数/带宽是针对加密货币高日内波动率而特意放宽的（相对于偏紧的股票默认值），修复标注为 "Issue3/Issue4 fix"。

## 与其他模块的关系

策略通过名称索引的字典（`Dict[str, Strategy]`）挂载到 `Router`，每个策略自行声明 `allowed_states`。真正接触组合/经纪商/风控的是 `Strategy.on_bar`——它读取 `Portfolio.get_position`/`get_equity`，经 `RiskManager` 计算仓位与预检，再通过 `ExecutionPort`（即当前引擎装配的执行适配器，回测或实盘皆可）提交订单。Regime 切换导致的策略更换完全由 `router.py` 的 `_handle_switch` 处理，策略自身不感知切换风险。
