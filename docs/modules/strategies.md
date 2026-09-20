# strategies/ 模块说明

策略是可插拔的信号生成器，回测和实盘共用同一套实现。注册与策略政策注入集中在 `composition/factory.py`；是否允许真实资金入场另由 `core/strategy_governance.py` 检查。当前 `TrendBreakout=paused_revalidation`，空头突破与区间均值回归为 `paused_redesign`，波动反转为 `isolated_research`。

## `strategies/base.py` — Strategy 基类

`Strategy(ABC)` 是所有策略实现的接口。抽象方法 `should_enter`/`should_exit` 返回 `None` 或一个信号字典（`action`、可选 `stop_loss`/`order_type`/`price`/`reason`）。

当前主路径分为持仓退出、入场候选和组合分配三个步骤；`on_bar(...)` 保留兼容编排，Router 不再通过 `route()` 调用它：

1. `process_exit_only(...)` 消费成交事件后，先运行统一 `hard_stop_exit`，再按冷却规则调用 `should_exit`。退出单接受后置 `exit_pending=True`，上下文等实际平仓事实确认后才清理。
2. `build_entry_candidate(...)` 只对空仓、没有待成交入场且状态允许的标的调用 `should_enter`，返回含信号和评分的 `EntryCandidate`，不提交订单。
3. 组合分配器排序后调用 `submit_entry_candidate(...)`：有有效止损时按风险定仓，否则保留权益 10% 的兼容定仓；再应用策略健康乘数、敞口限额、相关风险及回撤预算，并经 `check_entry_risk` 检查。只有订单被接受时才初始化 `entry_pending`、止损、参考入场价和 `entry_bar` 等上下文。

**关键不变量——普通退出冷却不阻止硬止损**：`just_entered = i <= entry_bar + 1` 只推迟 `should_exit`；`hard_stop_exit` 在此前执行，刚成交仓位仍可因止损退出。常驻保护单也有独立的盘中触发路径。原开仓策略通过权威 `CloseEvent` 接收按持仓身份汇总且去重的 `on_trade_closed`，部分平仓调用 `on_partial_close`；生命周期由实际成交而非提交时估计盈亏驱动。

## `strategies/mean_reversion.py` — RangeStrategy

注册名 `"RangeMeanReversion"`，允许状态为 `SIDEWAYS`，但当前路由为 `Cash`、策略暂停待重设计。当 `low <= 布林带下轨` 且默认 RSI < 30（买入），或 `high >= 布林带上轨` 且 RSI > 70（做空）时入场，用 ATR/价格上限（默认 3%）过滤；止损为 `±1×ATR`。回归到布林带中轨或触及止损（用 bar 的 low/high 检测）时出场。`on_trade_closed` 使用实际已实现盈亏更新每个标的的 `trade_state`：连续 3 笔亏损后设置 `cooldown_until = bar_index + 24` 并重置连亏计数，`i <= cooldown_until` 时不入场。当前类不重写 `on_bar`。

## `strategies/trend_breakout.py` — TrendBreakoutStrategy / TrendBreakdownStrategy

`TrendBreakoutStrategy`（`"TrendBreakout"`，仅允许 `TREND_UP`）与镜像的 `TrendBreakdownStrategy`（`"TrendBreakdown"`，`TREND_DOWN`）：唐奇安通道突破/破位系统。入场要求收盘价突破 `shift(1)` 滞后的 N 根 bar 滚动高/低点（默认 `entry_window=20`），并在存在成交量数据时通过默认启用的 OBV 同向确认。止损由 `plan_initial_stop` 生成，默认采用出场窗口（`exit_window=10`）的结构位；无法计量或止损距离过近时拒绝信号，超出配置最大距离时裁剪，不再隐式回退 5%。ATR 初始止损和移动止损能力保留，但当前配置均关闭。出场包括统一硬止损、退出通道突破或 regime 不再被允许。

两个类通过 `_PersistentHealthMixin` 接入 `StrategyHealthMachine` 并持久化状态。健康观察按退出 cohort 合并，以权威平仓净盈亏和初始风险计算 R，当前计入控制方为 `strategy`、`router`；账户风险退出保留报告而不触发健康降级。连续 3 个负 cohort 进入默认 30 天冷静期，再按当前统一恢复配置从 0.10、0.25、0.50 风险阶段逐级恢复，最终回到 1.00；阶段须满足时间、cohort 数、标的分散和剔除最佳 cohort 后仍为正等条件。反复失败进入延长冷静期，显式 `MANUAL_LOCK` 需授权恢复。健康闸门只约束新入场，退出照常管理；`health_stats` 是兼容视图，不再是永久 `is_alive` 死亡开关。这些参数仍是研究候选，详见[健康契约](../strategy_health_contract.md)与[统一规则契约](../roadmap_policy_contract.md)。

## `strategies/volatility.py` — VolatilityReversionStrategy

注册名 `"VolatilityReversion"`，允许 `VOLATILE`，当前只保留隔离研究。默认用 20 bar 均值和标准差识别 ±2 倍标准差的扩张，并要求随机指标 `%K` < 20（多头）或 > 80（空头）确认；以平均 bar 高低幅度的 1.5 倍设置初始风险距离。回归均值或 regime 不再允许时退出，并共用基类硬止损。当前仓库没有 `trend_following.py`，也没有注册 `TrendUp`/`TrendDown` 这两个旧策略名。

## 与其他模块的关系

策略通过名称索引的字典（`Dict[str, Strategy]`）挂载到 `Router`，每个策略自行声明 `allowed_states`。基类读取 `Portfolio`，由 `RiskManager` 和组合分配器约束新入场，再通过 `ExecutionPort` 提交订单。Router 按批次账本保持原开仓策略对持仓的退出管理；空仓后的映射改变只撤销旧入场意图并冷却。策略仍能在 `should_exit` 中读取当前 regime，具体见[路由模块说明](router.md)。`strategies/statistical_arbitrage.py` 的 `PairsTradingModel` 未注册到默认策略表，仅供单独研究使用。
