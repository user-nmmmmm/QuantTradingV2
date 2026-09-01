# 策略源码详细技术分析

> 文档状态：技术分析 v1.0
> 生成日期：2026-08-31
> 分析范围：`strategies/`、`router/`、`composition/factory.py`、`core/state.py`、`core/phase4.py`（路由/分配部分）、`core/risk*.py`（策略集成面）、`core/indicators.py`、`core/factors/`（策略实际使用的子集）
> 关联文档：[`docs/modules/strategies.md`](modules/strategies.md)、[`docs/modules/router.md`](modules/router.md)、[`docs/strategy_development_roadmap.md`](strategy_development_roadmap.md)、[`docs/backtest_assumptions.md`](backtest_assumptions.md)

---

## 0. 阅读前须知

1. 本文只描述**当前代码的真实行为**。仓库中已有的 `docs/modules/strategies.md` 与 `docs/modules/router.md` 部分内容已过时（见 §11.1），以本文为准。
2. 本文不评估策略盈利能力。`strategy_development_roadmap.md` §2.2 已给出结论：当前策略组合在 2017–2026 六标的日线上**统计上无法排除“不赚钱”**。
3. 配置层面，`config/params.yaml` 目前把除 `TrendBreakout` 外的所有 regime 都路由到 `Cash`（见 §10）。也就是说“默认跑起来”只有一个策略在实际下单。

---

## 1. 整体架构与数据流

### 1.1 分层

```
历史/实盘行情 (MarketDataSlice)
        │
        ▼
core/state.py  MarketStateMachine  ── 把 OHLCV 序列映射为离散 MarketState
        │
        ▼
core/runtime.py  EventProcessor    ── 每根 bar 的确定性编排（唯一业务路径，回测/实盘共用）
        │
        ├─(1) 熔断器检查        core/risk/circuit_breaker.py
        ├─(2) 逐标的：持仓管理   router.Router.process_position_management
        ├─(3) 逐标的：入场候选   router.Router.collect_entry_candidate → Strategy.build_entry_candidate
        └─(4) 组合级分配        core/phase4.PortfolioSignalAllocator.allocate → Strategy.submit_entry_candidate
        │
        ▼
core/risk*.py  RiskManager        ── 定仓、名义上限削减(clamp)、准入闸门(gate)
        │
        ▼
core/execution_port.py (ExecutionPort)  ── 回测 SimulatedExecutionAdapter / 实盘 broker
        │
        ▼
Broker._execute_trade → core/lots.py 逐笔记账 → 生成 CloseEvent
        │
        ▼（下一根 bar 或本 bar 末，通过 _consume_execution_trades 回流）
Strategy.on_trade_closed  ── 更新健康度/连亏冷却等跨 bar 状态
```

### 1.2 关键设计原则

- **策略是纯信号插件**：`Strategy` 子类只实现 `should_enter` / `should_exit`，返回信号字典或 `None`。它不直接接触撮合、账本、事件管线。
- **编排与调度分离**：真正调用 `Portfolio` / `RiskManager` / `ExecutionPort` 的是基类 `Strategy` 的模板方法（`on_bar` 或两阶段的 `build_entry_candidate` / `submit_entry_candidate`）。
- **regime 路由与风险处置解耦**：状态识别在 `MarketStateMachine`，策略选择在 `Router`，状态切换时的风险处置也集中在 `Router`（当前实现是“停止新入场 + 冷却”，**不强平**，见 §6.3）。
- **回测/实盘同源**：`EventProcessor` 是唯一业务路径，两种模式只在“行情如何产生”和“订单如何撮合”这两个适配器上分叉。

---

## 2. 时间与执行模型（Next-Bar Execution）

这是理解所有策略代码的前提，写在 `strategies/base.py` 顶部 docstring 里：

| 时刻 | 发生的事 |
| --- | --- |
| bar `i` 收盘 | 策略基于 `df.iloc[:i+1]`（含第 `i` 根）判断信号，调用 `broker.submit_order(...)` |
| bar `i+1` 开盘 | Broker 撮合成交（回测模型：下一根 bar 开盘价 + 滑点 + 冲击成本） |
| bar `i+1` `on_bar` 再次运行 | 通过 `_consume_execution_trades` 感知到上一步的成交 |

由此产生两条**硬性不变量**：

### 2.1 一根 bar 的出场冷却（`just_entered`）

```python
just_entered = i <= ctx_pre.get("entry_bar", -2) + 1
```

入场信号在 bar `N` 提交，bar `N+1` 开盘成交，`on_bar` 在 `N+1` 再次运行。若此时立即检查 `should_exit`，会出现“刚开仓同一根就被平掉”的抖动。因此新开仓位会**跳过一次** `should_exit` 检查。

> 注意：`hard_stop_exit`（硬止损）**不受** `just_entered` 约束——即使刚开仓，只要盘中触及止损价仍然离场。

### 2.2 未来函数防护

- 状态机 / 唐奇安通道等所有滚动指标显式 `.shift(1)`，只用“上一根及更早”的数据。
- 多周期对齐 `align_state_to_lower_tf` 用 `ffill`（前向填充），低周期在某时刻只能拿到该时刻已确认的高周期状态。
- `core/indicators.py` 把 ATR/ADX/RSI 的前 `n-1` 根显式置为 `NaN`，避免早期不稳定值触发信号。

---

## 3. `strategies/base.py` — Strategy 基类详解

### 3.1 生命周期状态

每个策略实例维护两组 per-symbol 状态：

| 字段 | 位置 | 用途 |
| --- | --- | --- |
| `context[symbol]` | `Strategy.get_context` | 跨 bar 交易状态：`entry_price` / `stop_loss` / `trailing_stop` / `entry_bar` / `entry_pending` / `exit_pending` |
| `observed_close_events` | 基类计数器 | 该策略实际观察到收盘的 round-trip 数，供 `core.diagnostics.calculate_lifecycle_coverage` 对账 |
| `_consumed_close_event_ids` | 基类 set | CloseEvent 幂等去重，防止同一事件被 `on_trade_closed` 消费两次 |

`reset_runtime_state()` 在每次回测 run 开始时由 `BacktestEngine.run` 调用，清空上述状态。

### 3.2 `on_bar` 模板方法（传统单标的路径）

执行顺序（每根 bar 每标的调用一次）：

1. **`_consume_execution_trades`**：先消费上一根 bar 的成交回执（见 §3.4）。
2. **出场优先**：若 `qty != 0` 且无 `exit_pending` 闩锁：
   - 先查 `hard_stop_exit`（共享的盘中硬止损，用 `df.low/high` 检测盘中触及）；
   - 若无硬止损且非 `just_entered`，再查 `should_exit`；
   - 命中则 `_publish_signal`（先发决策事实）→ `broker.submit_order`，被接受后置 `exit_pending = True`。
3. **入场其次**：若 `qty == 0` 且无 `entry_pending` 且 `state in allowed_states`：
   - 调 `should_enter` 拿信号；
   - **定仓**：
     - `stop_loss > 0` → `risk_manager.calculate_position_size(equity, price, stop_loss)`（风险定仓）；
     - 否则 → `calculate_position_size_fixed_pct(equity, price, pct=0.10)`（固定 10% 兜底）；
   - **削减**：`risk_manager.clamp_entry_qty(...)` 把 qty 削到风控上限内（而非整单作废，见 §8.3）；
   - **闸门**：`risk_manager.check_entry_risk(...)` 最终准入检查；
   - 通过则 `_publish_signal` → `submit_order`（带 `stop_loss`，会持久化到 lot 的 `initial_risk = |entry-stop| * qty`）；
   - **只有 `submission.accepted` 时**才初始化 `context[symbol]`，其中 `entry_pending = True`、`entry_bar = i`。

### 3.3 两阶段入场（Phase-4 组合分配路径，当前默认）

`EventProcessor` 实际走的是这条路径，把“产生候选”和“花风险预算”拆开，以便在同一时间戳的多个候选之间做组合级排序：

| 阶段 | 方法 | 做什么 | 不做什么 |
| --- | --- | --- | --- |
| 1. 提案 | `build_entry_candidate` | 检查空仓 / 无 `entry_pending` / `state in allowed_states`，调 `should_enter`，包装成 `EntryCandidate(symbol, self, i, df, state, signal, score)` | 不定仓、不占用风险预算 |
| 2. 提交 | `submit_entry_candidate` | 在组合排序**之后**：定仓 → clamp → `check_entry_risk` → `submit_order` → 初始化 context | — |

`score` 取自 `signal["score"]` 或 `signal["priority"]`，默认 `0.0`（当前所有策略都没设置，即全部同分，排序退化为按策略名+symbol 字典序）。

出场路径用 `process_exit_only`：只评估既有持仓，逻辑与 `on_bar` 的出场分支一致（`_consume` → `hard_stop_exit` → 非 `just_entered` 时 `should_exit` → 提交并置 `exit_pending`）。

### 3.4 `_consume_execution_trades` —— CloseEvent 回流（关键机制）

```python
close_events = getattr(broker, "close_events", None) or []
for event in close_events:
    if event.opening_strategy_id != self.name:   # 按“开仓策略”过滤，不是按平仓策略
        continue
    if event.close_event_id in self._consumed_close_event_ids:
        continue
    ...
    self.on_trade_closed(event.symbol, event.realized_pnl, trade, bar_index)
    if event.is_position_fully_closed:
        self.context[event.symbol] = {}       # 仓位完全平掉 → 清空 context
```

设计要点：

- **CloseEvent 是权威的**：由 `Broker._execute_trade` 在成交时基于 `core/lots.py` 的逐笔账本生成一次，**不管**平仓是哪条路径触发的（策略自身出场 / 硬止损 / Router 时间强平 / 熔断强平 / 回测结束 EndOfBacktest）。
- **按 `opening_strategy_id` 归属**：该字段记录在 lot 开仓时，因此即使仓位被 Router / CircuitBreaker / EndOfBacktest 平掉，PnL 与 `on_trade_closed` 回调仍然记到**当初开仓的那个策略**头上。
- **幂等**：`close_event_id` 去重，`_consume` 被多次调用（例如强平后 `engine.py` 额外主动调一次）也不会重复计数。
- **`entry_pending` 自愈**：如果 `entry_pending` 为真但仓位仍为 0 且 broker 报告该 symbol 没有活跃挂单（`has_active_open_order(symbol) is False`），清空 context——修复订单被拒/取消后闩锁不释放的问题（roadmap D-06）。

### 3.5 `hard_stop_exit` —— 共享盘中硬止损

```python
low = df["low"].iat[i]; high = df["high"].iat[i]
if qty > 0 and low <= stop:  return {"action": "sell",  "reason": "hard_stop", "order_type": "market"}
if qty < 0 and high >= stop: return {"action": "cover", "reason": "hard_stop", "order_type": "market"}
```

- 所有策略共用，在 `should_exit` **之前**执行。
- 用 bar 的 `low`/`high` 检测**盘中**触及，而非收盘价。
- `stop_loss` 来自 `context[symbol]["stop_loss"]`，即入场信号里给出的初始止损（不随行情移动，除非策略自己在 `context` 里更新——当前几个策略都没有做移动止损）。

### 3.6 `_publish_signal` —— 决策事实先于订单（T-2.9）

在提交订单之前，向 `broker.event_pipeline` 发布一个 `Signal` 事件（`strategy_id` / `symbol` / `action` / `signal_kind` entry|exit / `reference_price` / `reason` / `bar_time`），时间戳统一转 UTC，`idempotency_key = f"{name}:{symbol}:{iso}:{kind}:{action}"`。用于事后重建“策略当时想做什么” vs “实际成交了什么”。

---

## 4. `core/state.py` — 市场状态机

### 4.1 状态枚举

```python
class MarketState(Enum):
    TREND_UP = 1;  TREND_DOWN = 2;  SIDEWAYS = 3;  NO_TRADE = 4;  VOLATILE = 5
    # 别名：BULL_TREND=1, BEAR_TREND=2, RANGE=3
```

`NO_TRADE`（值 4）在当前 `calculate_states` 里**从未被赋值**，是保留状态。实际产出只有 4 种：`TREND_UP` / `TREND_DOWN` / `SIDEWAYS` / `VOLATILE`。

### 4.2 raw state 判据（互斥）

| 状态 | 条件 |
| --- | --- |
| `TREND_UP` | `close > SMA_fast > SMA_slow` 且 `ADX > adx_threshold` |
| `TREND_DOWN` | `close < SMA_fast < SMA_slow` 且 `ADX > adx_threshold` |
| `VOLATILE` | `ADX > adx_threshold` 且 `ATR/close > atr_pct_threshold` 且 **`~(up_cond \| down_cond)`** |
| `SIDEWAYS` | 其余 |

默认参数（`params.yaml` `state` 节）：`ma_fast=20`、`ma_slow=60`、`adx_period=14`、`adx_threshold=25`、`atr_period=14`、`atr_pct_threshold=0.025`。

**`VOLATILE` 的排他条件是关键设计**（代码注释与 `state.py` docstring 有实测数据）：三个条件共用同一个 ADX 门槛，若让 `VOLATILE` 无条件覆盖趋势 bar，在加密日线上（BTC ATR 中位数约为价格 4.4%，远超 2.5% 阈值）几乎所有趋势 bar 会被 `VOLATILE` 吞掉——实测 BTC 2017–2026 上 96.3% 的 `TREND_UP` 与 99.8% 的 `TREND_DOWN` 会消失，趋势策略永不上场。因此 `VOLATILE` 只认领“ADX 高、波幅大、但均线未成方向”的转折/whipsaw 段。

### 4.3 稳定性过滤器（去抖动）

`_apply_stability_filter` 逐根扫描 raw states：

- `current_stable`：已确认的稳定状态；
- `candidate_state` + `consecutive_count`：候选新状态及其连续出现次数；
- 候选状态**连续出现 ≥ `stability_period`（默认 5）**次，才把 `current_stable` 切过去。

效果：显著降低状态翻转带来的换仓/撤单噪声。代价：状态切换有最多 `stability_period` 根的滞后。

### 4.4 多周期对齐

`align_state_to_lower_tf(state_high_tf, index_low_tf)`：`reindex(..., method="ffill")` + 起始 NaN 填 `SIDEWAYS`。保证低周期只用已确认的高周期状态。

### 4.5 懒计算

`get_state(df, i)`：若 `df` 已有 `market_state` 列直接读；否则对全 `df` 算一次 `calculate_states` 并写回列。`EventProcessor._collect_symbol_candidate` 在循环里对每个 `(df, location)` 调用它。

---

## 5. `router/router.py` — Router

### 5.1 构造与 regime_map

```python
Router(strategies: Dict[str, Strategy], regime_map: Dict[str,str], cooldown_bars=3,
       log_path=None, log_flush_every=256, max_holding_days=None)
```

`regime_map` **必填且非空**（否则 `ValueError`），把 `MarketState.name` 映射到策略名或字符串 `"Cash"`。`build_router`（`composition/factory.py`）从 `params.yaml` 的 `routing` 节读取；`allow_short=False` 时把 `TREND_DOWN` 强制改成 `"Cash"`。

### 5.2 两个入口方法（当前 Phase-4 路径）

`EventProcessor._collect_symbol_candidate` 检测到 Router 同时实现 `process_position_management` 和 `collect_entry_candidate` 时，走这条分离路径：

#### `process_position_management(symbol, i, df, state, portfolio, broker) -> bool`

1. 对**所有**策略调 `_consume_execution_trades`（保证任何策略开的仓，其平仓回执都能被消费）。
2. 若 `qty != 0`：
   - **最大持有期检查**：`_max_holding_expired` → 若最早的 lot `entry_time` 距今 ≥ `max_holding_days`，用 `MaxHoldingPeriod` 策略名提交平仓单，返回 `True`。
   - 否则：找到 `_opening_strategy_name`（该 symbol 第一个 open lot 的 `strategy_id`），调用**那个策略**的 `process_exit_only`（只出场、不入场），返回 `True`。
3. `qty == 0` 返回 `False`。

#### `collect_entry_candidate(...) -> Optional[EntryCandidate]`

仅对已确认空仓的 symbol：

1. **冷却检查**：`i <= cooldowns[symbol]` → 记 `cooldown` 日志，返回 `None`；过期则删除冷却项。
2. **状态切换检查**：
   ```python
   previous_name = regime_map[last_state]; strategy_name = regime_map[state]
   changed = last_state is not None and previous_name != strategy_name
   if changed:
       broker.cancel_symbol_orders(symbol)      # 撤销陈旧的入场挂单
       cooldowns[symbol] = i + cooldown_bars     # 进入冷却
       return None                               # 本 bar 不产生候选
   ```
   **注意：状态切换只“停止新入场 + 撤挂单 + 冷却”，绝不平掉已有 lot。** 已有持仓由 §5.2 的 `process_position_management` 交给开仓策略处理，或由 `MaxHoldingPeriod` 超时强平。
3. `strategy_name` 为空或 `"Cash"` → 记 `CASH` 日志，返回 `None`。
4. 策略不存在或 `state not in strategy.allowed_states` → 记 `MISSING_STRATEGY` 日志，返回 `None`（双重保险：regime_map 和 `allowed_states` 都要同意）。
5. 否则调 `strategy.build_entry_candidate(...)` 返回候选。

### 5.3 路由日志

`_log_routing` 缓冲行（`timestamp` / `symbol` / `regime` / `strategy` / `current_qty` / `route_event` / `strategy_changed`），每 `log_flush_every` 行 flush 到 CSV（`reports/routing_log.csv`）。`route_event` 取值：`time_exit` / `position_exit_control` / `cooldown` / `stop_new_entries` / `cash` / `missing_strategy` / `candidate`。

---

## 6. `core/phase4.py` — 组合信号分配

### 6.1 `EntryCandidate`（frozen dataclass）

`(symbol, strategy, bar_index, frame, state, signal, score)`。`strategy_name` property 取 `strategy.name`。

### 6.2 `PortfolioSignalAllocator`

```python
@staticmethod
def rank(candidates):
    return sorted(candidates, key=lambda c: (-float(c.score), c.strategy_name, c.symbol))

def allocate(self, candidates, *, portfolio, broker, risk_manager, current_prices):
    for rank, candidate in enumerate(self.rank(candidates), start=1):
        result = candidate.strategy.submit_entry_candidate(candidate, ...)
        accepted = bool(getattr(result, "accepted", False))
        self.audit.append(AllocationDecision(symbol, strategy, score, rank, accepted, reason))
```

- 同一时间戳的所有候选**先排序，后逐个提交**。排序在前意味着高分候选先占用风险预算（杠杆/集中度上限），低分候选拿到的是剩余额度。
- `pending_open_notional` / `RiskReservationProjection` 让排在后面的候选能看到前面候选已“预留”的名义敞口。
- 当前所有策略 `score=0`，排序完全由 `(strategy_name, symbol)` 字典序决定——**这是一个待补的能力点**（roadmap S2）。

### 6.3 研究分析函数（非交易路径）

`state_duration_and_transition_matrix`（状态停留时长 + 转移矩阵）、`joint_entry_exit_attribution`（入场策略 × 出场控制器的 PnL 矩阵）、`holding_period_audit`（持有期分位数 + 超时清单）。供报告使用。

---

## 7. 各策略详解

### 7.1 `TrendBreakoutStrategy`（注册名 `"TrendBreakout"`）

**allowed_states**：`{TREND_UP, VOLATILE}`（突破常在波动扩张时发生）。

**指标**（`_ensure_indicators`）：
- `HIGH_MAX_{entry_window}` = `df.high.rolling(entry_window).max().shift(1)` —— 唐奇安上轨，`shift(1)` 不含当前 bar。
- `LOW_MIN_{exit_window}` = `df.low.rolling(exit_window).min().shift(1)` —— 唐奇安下轨（出场/止损用）。
- `OBV`（若有 `volume` 列）。

默认 `entry_window=20`、`exit_window=10`、`use_obv=True`。

**入场 `should_enter`（仅做多）**：
1. `check_health()` 闸门（见下），未通过直接 `None`。
2. `i < entry_window` → `None`。
3. `close > HIGH_MAX_entry_window` 判定有效突破。
4. **OBV 成交量确认**：`OBV[i] > OBV[i - entry_window]`（窗口内净流入），否则视为缩量假突破，`None`。数据无 `volume` 时跳过此确认。
5. 止损 = `LOW_MIN_exit_window`（唐奇安出场位）；若 `NaN` 或 `>= close` 则回退到 `close * 0.95`（固定 5%）。
6. 返回 `{"action":"buy", "stop_loss":..., "order_type":"market", "price": close}`。

**出场 `should_exit`**：
1. `close < LOW_MIN_exit_window` → 平多（`reason="Breakout Exit (Below Low10)"`）。
2. `state not in allowed_states` → 防御性平多（`reason="Regime ... Not Allowed"`）。

**健康闸门（Alpha Death，`_PersistentHealthMixin` + `check_health`）**：
- `health_stats`：`total_trades` / `consecutive_losses` / `rolling_pnl`（list）/ `is_alive` / `death_reason`，`scope="cross_symbol_aggregate"`（**跨标的混合计数**，非按 symbol 分组——见 §11.2）。
- `on_trade_closed` 每次收盘更新：追加 `realized_pnl` 到 `rolling_pnl`，累计/清零 `consecutive_losses`，`_persist_health()`（若绑定了 state_store）。
- `check_health()` 判死条件（**永久，不自动复活**）：
  - `consecutive_losses > 5` → `is_alive=False`，`death_reason="Consecutive Losses > 5"`；
  - `len(rolling_pnl) >= 20` 且 `mean(rolling_pnl[-20:]) < 0` → `death_reason="Rolling Mean Return < 0 (20 trades)"`。
- `bind_state_store` 支持从 `state_store["strategy_health:{name}"]` 恢复健康状态（实盘持久化）。

### 7.2 `TrendBreakdownStrategy`（注册名 `"TrendBreakdown"`）

`TrendBreakoutStrategy` 的镜像做空版。

- **allowed_states**：`{TREND_DOWN}`（**不含** VOLATILE）。
- 列名互换：`col_high_max = HIGH_MAX_{exit_window}`（止损用），`col_low_min = LOW_MIN_{entry_window}`（入场用）。
- 入场：`close < LOW_MIN_entry_window` + OBV 净**流出**确认（`OBV[i] < OBV[i-entry_window]`）；止损 = `HIGH_MAX_exit_window`，回退 `close * 1.05`；返回 `action="short"`。
- 出场：`close > HIGH_MAX_exit_window` 平空，或 regime 不允许。
- 同样的 `check_health()`（`check_health` 实现略有精简但逻辑等价）。

### 7.3 `RangeStrategy`（注册名 `"RangeMeanReversion"`）

**allowed_states**：`{SIDEWAYS}`。

**指标**（`_ensure_indicators`）：`BB_UPPER/BB_MIDDLE/BB_LOWER`（布林带 20, 2.0）、`ATR_14`、`RSI_14`。

默认参数：`atr_threshold_pct=0.03`、`rsi_oversold=30`、`rsi_overbought=70`、`use_rsi=True`。

**入场 `should_enter`**：
1. **冷却检查**：`i <= trade_state[symbol]["cooldown_until"]` → `None`。
2. `i < 1` 或指标 `NaN` → `None`。
3. **波动率过滤**：`ATR/close > atr_threshold_pct`（3%）→ `None`（趋势/剧烈波动中不抄底摸顶）。
4. **入场判据**：
   - `low <= BB_LOWER` 且（`not use_rsi` 或 `RSI < rsi_oversold`）→ `{"action":"buy", "stop_loss": close - 1*ATR}`；
   - `high >= BB_UPPER` 且（`not use_rsi` 或 `RSI > rsi_overbought`）→ `{"action":"short", "stop_loss": close + 1*ATR}`。
   - RSI 确认用于过滤布林带单指标的假信号。

**出场 `should_exit`**：
1. **回归中轨止盈**：多头 `close >= BB_MIDDLE` / 空头 `close <= BB_MIDDLE` → `reason="Target hit (Mid Band)"`。
2. **止损**：多头 `bar_low < stop_loss` / 空头 `bar_high > stop_loss`（盘中检测）→ `reason="Stop Loss"`。

> **死代码提示**：`strategies/mean_reversion.py` 当前实现的 `should_exit` 在上述逻辑后就 `return`，`docs/modules/strategies.md` 与 roadmap D-05 提到的“第一个 `return None` 之后有不可达的重复止损块”在当前文件里已不存在（文件已被清理为紧凑实现）。若历史 diff 中看到，请以现文件为准。

**连亏冷却（`trade_state`，重写 `on_trade_closed`）**：
- `trade_state[symbol]`：`consecutive_losses` / `cooldown_until`。
- `realized_pnl < 0`：`consecutive_losses += 1`；达到 **3** 时设 `cooldown_until = bar_index + 24` 并清零计数。
- `realized_pnl >= 0`：`consecutive_losses = 0`。

与 TrendBreakout 的 alpha-death 不同：这里是**按 symbol 分组**的**临时**冷却（24 根 bar），不是永久熄火。

### 7.4 `VolatilityReversionStrategy`（注册名 `"VolatilityReversion"`）

**allowed_states**：`{VOLATILE}`。“交易失败的波动扩张，用 ATR 定义硬风险。”

**指标**（`_indicators`，全部内联计算，不写回 df 的既有列）：
- `mean = close.rolling(window).mean()`
- `std = close.rolling(window).std()`
- `atr = (high - low).rolling(window).mean()` —— **注意这是简化 ATR**（只用当根 high-low 的均值，不含跳空的 true range）。
- `STOCH_K/STOCH_D`（`_ensure_stoch`，仅在 `use_stochastic` 时）。

默认参数：`window=20`、`entry_z=2.0`、`stop_atr=1.5`、`stoch_oversold=20`、`stoch_overbought=80`。

**入场 `should_enter`**：
1. `state not in allowed_states` 或 `i < window` → `None`。
2. `close/mean/std/atr` 任一非正或 `NaN` → `None`。
3. `z_score = (close - mean) / std`。
4. **做多**：`z_score <= -entry_z` 且（`not use_stochastic` 或 `STOCH_K < stoch_oversold`）→ `{"action":"buy", "price": close, "stop_loss": close - stop_atr*atr}`。
5. **做空**：`z_score >= entry_z` 且（`not use_stochastic` 或 `STOCH_K > stoch_overbought`）→ `{"action":"short", ..., "stop_loss": close + stop_atr*atr}`。
   - Stochastic 确认避免“只有 z-score 极值、无动量衰竭”时入场。

**出场 `should_exit`**：
- 无持仓 → `None`。
- 多头 `close >= mean`（回归到中枢）或 `state not in allowed_states` → 平多（`reason="Volatility mean reversion complete"`）。
- 空头 `close <= mean` → 平空。
- **未实现独立止损分支**——止损完全依赖基类 `hard_stop_exit` 用 `context["stop_loss"]`。

### 7.5 `PairsTradingModel`（`strategies/statistical_arbitrage.py`）

**独立于 `Strategy` 基类**，不接入 `Router` / `EventProcessor`，仅供相关性/统计套利研究。

```python
PairsTradingModel(window=60, entry_z=2.0, exit_z=0.5, min_correlation=0.6)
signal(left: pd.Series, right: pd.Series) -> PairSignal(action, z_score, hedge_ratio)
```

- 取两序列末尾 `window` 根对齐数据（不足则 `hold`）。
- `correlation = corr(left.pct_change(), right.pct_change())`，`|corr| < min_correlation` → `hold`。
- `hedge = cov(x,y) / var(y)`；`spread = x - hedge*y`；`z = (spread[-1] - spread.mean()) / spread.std()`。
- `z >= entry_z` → `"short_left_long_right"`；`z <= -entry_z` → `"long_left_short_right"`；`|z| <= exit_z` → `"exit"`；否则 `"hold"`。

---

## 8. 与 RiskManager 的集成面

`RiskManager` = `CircuitBreakerMixin` + `PositionSizingMixin` + `EntryPolicyMixin` 三个 mixin 通过继承组合（`core/risk/`），所有方法读写同一个 `self`。策略只通过 4 个方法与它交互。

### 8.1 定仓公式

| 方法 | 公式 | 何时用 |
| --- | --- | --- |
| `calculate_position_size(equity, entry, stop)` | `qty = (equity * risk_per_trade) / \|entry - stop\| * risk_multiplier` | 信号带 `stop_loss > 0`（所有当前策略都带） |
| `calculate_position_size_fixed_pct(equity, entry, pct=0.10)` | `qty = (equity * pct) / entry * risk_multiplier` | 无止损兜底 |

- `risk_per_trade` 默认 `0.02`（`params.yaml`，注释说明从 1% 上调）。
- `risk_multiplier`：正常 `1.0`；熔断 `REDUCE` 态 `reduced_risk_multiplier`（默认 0.5）；`BLOCK_NEW` 及以上 `0.0`。
- 两个方法开头都检查 `_blocks_new_risk()` 和 `_health_allows_new_risk()`，不满足返回 `0.0`。

> **反直觉点（roadmap S3-4 待解决）**：风险定仓下 `notional/equity = risk_per_trade ÷ (止损距离/价格)`，止损越紧仓位越大。这就是为什么需要 clamp 而不是直接拒单——否则风险最小的信号反而永远无法成交。

### 8.2 名义上限口径（`_entry_notional_caps`）

`check_entry_risk`（布尔闸门）与 `max_entry_notional`（削减上限）**共用同一份 caps**，避免“双重限速口径”漂移。caps 是一个 `{约束名: 允许的最大 trade_value}` 字典：

| 约束 | 上限 |
| --- | --- |
| `leverage` | `equity * max_leverage - current_exposure - reserved_exposure` |
| `concentration` | `equity * max_pos_size_pct - current_pos_value - reserved_symbol_value` |
| `cash`（仅 SPOT 做多） | `portfolio.cash - reserved_exposure` |
| `initial_margin`（保证金账户） | `equity / initial_margin_rate - current_exposure - reserved_exposure` |
| `account_mode`（SPOT 做空） | `0.0`（现货禁止做空） |

默认：`max_leverage=3.0`、`max_pos_size_pct=0.30`、`liquidity_limit_pct=0.01`。

`current_prices=None` 且账户有持仓 → 返回 `None`（fail closed，无法核实敞口就拒绝）。

### 8.3 clamp vs gate

- **`clamp_entry_qty`**：`min(qty, allowed_notional / price)`。若削减后 `clamped * price < equity * min_entry_notional_pct`（默认 1%）→ 返回 `0`（尘埃过滤，避免只剩一点额度成交纯付手续费的仓位）。`CAP_SAFETY_MARGIN = 1e-9` 确保 `qty*price` 的浮点舍入不会反弹越限。
- **`check_entry_risk`**：最后一道防线（布尔）。依次检查熔断 → 健康 → `qty/price > 0` → 流动性（`qty > volume * liquidity_limit_pct`）→ cash → leverage → concentration。策略路径中 clamp 已经先执行，这里通常应通过。

### 8.4 熔断器（`CircuitBreakerMixin`）

两条独立线：

1. **日内损失熔断**（`daily_loss_triggered`）：`daily_drawdown >= daily_loss_limit`（默认 0.05）。每个 UTC 日边界 `reset_daily_breaker` 重置。
2. **组合回撤动作**（`portfolio_breaker_action`，**sticky**，只有 `manual_resume` 能降级）：按 `last_drawdown`（相对 high-water）阶梯：
   | drawdown ≥ | action |
   | --- | --- |
   | `reduce_threshold` 0.10 | `REDUCE`（仓位 ×0.5） |
   | `block_threshold` 0.15 | `BLOCK_NEW`（停止新开仓，保留出场） |
   | `liquidate_threshold` 0.20 | `LIQUIDATE`（全平） |
   | `lock_threshold` 0.25 | `LOCKED`（终止） |

`RiskControlDecision` 把这些翻译成 `allow_position_management` / `allow_new_entries` / `force_liquidate` / `terminal`，`EventProcessor` 据此决定这一根 bar 是否还调用 `collect_entry_candidate` / `allocate`。

> **BLOCK_NEW 永不禁用止损与策略出场**——`process_position_management` 始终执行，只有 `allow_new_entries` 被关掉。

### 8.5 健康闸门（数据/系统健康，区别于策略 alpha-death）

`set_health_assessment(assessment)` 安装最新的数据健康事实；`_health_allows_new_risk()` 在 `assessment.allows_new_risk` 为假时拒绝一切新开仓（记 `logger.critical`）。这与 §7.1 的策略级 `check_health()` 是**两套独立机制**。

---

## 9. 指标实现细节（策略实际依赖的子集）

### 9.1 `core/indicators.py`（策略/状态机的最小集）

| 指标 | 实现要点 |
| --- | --- |
| `SMA` | `series.rolling(n).mean()` |
| `EMA` | `ewm(span=n, adjust=False)` |
| `ATR` | TR = max(H-L, \|H-Cprev\|, \|L-Cprev\|)；Wilder 平滑 `ewm(alpha=1/n, adjust=False)`；**前 `n-1` 根显式置 NaN** |
| `ADX` | +DM/-DM → Wilder 平滑 → +DI/-DI → DX → `ewm(alpha=1/n)` 平滑；前 `n-1` 根 NaN。注：标准 ADX 初始化更复杂（先 SMA 后 Wilder），此处全程 EWM 近似 |
| `BBANDS` | `middle = SMA(n)`；`upper/lower = middle ± k * rolling_std(n)` |

### 9.2 `core/factors/`（opt-in 扩展指标，不自动挂载）

策略实际用到的：

- `MomentumFactors.RSI`（Wilder 平滑，`avg_loss==0` 时置 100，前 `n-1` NaN）—— RangeStrategy 用。
- `MomentumFactors.STOCH`（`%K_raw` → SMA(smooth_k) → `%D` = SMA(d_period)）—— VolatilityReversion 用。
- `VolumeFactors.OBV`（`sign(close.diff()) * volume` 的 `cumsum`）—— TrendBreakout/Breakdown 用。

`core.factors` 包与 `core.indicators` **完全独立**（设计上刻意隔离，避免影响已验证的信号路径）。

---

## 10. 当前配置状态（`config/params.yaml`）

### 10.1 routing 节

```yaml
routing:
  TREND_UP: "TrendBreakout"
  TREND_DOWN: "Cash"     # 做空侧准入待重新设计 (T-4.5)
  SIDEWAYS: "Cash"       # RangeMeanReversion 待重新设计 (T-4.6)
  VOLATILE: "Cash"       # VolatilityReversion 仅隔离研究 (T-4.7)
```

**实际只有 `TrendBreakout` 在默认配置下会下单。** 其余三个策略虽在 `build_strategy_registry()` 中注册，但 regime 全部指向 `Cash`，`collect_entry_candidate` 走 `CASH` 分支不产生候选。

### 10.2 strategy_governance 节

```yaml
strategy_governance:
  TrendBreakout: admitted
  TrendBreakdown: paused_redesign
  RangeMeanReversion: paused_redesign
  VolatilityReversion: isolated_research
```

### 10.3 其他关键参数

| 节 | 参数 | 值 | 影响 |
| --- | --- | --- | --- |
| `router` | `cooldown_bars` | 2 | 状态切换后 2 根 bar 内不产生新候选 |
| `phase4` | `max_holding_days` | 365 | 单个 lot 持有满 365 天由 `MaxHoldingPeriod` 强平 |
| `state` | `stability_period` | 5 | 状态切换需连续 5 根确认 |
| `risk` | `risk_per_trade` | 0.02 | 单笔风险 2% equity |
| `execution` | `commission_rate_taker` | 0.0005 | 0.05% taker |

---

## 11. 已知缺陷、局限与文档偏差

### 11.1 模块文档已过时

- `docs/modules/router.md` 描述的 `route()` / `_handle_switch()` / “状态切换在当前 bar 收盘价强制平仓”**是旧实现**。当前 Router 用 `process_position_management` + `collect_entry_candidate`，状态切换**只停不平**（§5.2 步骤 2）。
- `docs/modules/strategies.md` 引用的 `strategies/trend_following.py`（`TrendUpStrategy` / `TrendDownStrategy`）**文件已不存在**，registry 里也没有。
- `router.md` 里的默认 regime_map（`TREND_UP → TrendUp` 等）与实际 `params.yaml` 不符。

### 11.2 策略层结构性问题（`strategy_development_roadmap.md` §2.3，均有实测证据）

| ID | 问题 | 对策略行为的影响 |
| --- | --- | --- |
| D-01 | 趋势策略出场规则历史上从未触发（`TrendBreakout` 自身出场 0/91），绝大多数由 Router 强平 | `exit_window` 参数事实上无效（当时）；出场链路修复后 3.42 的 PF 需重测 |
| D-02 | 平仓回调断链，`lifecycle_coverage` 仅 20.7% | `on_trade_closed` 不触发 → `check_health()` 的连亏计数/rolling PnL 收不到数据 → **alpha-death 闸门与连亏冷却静默失效**（实测最长连亏 12 > 阈值 5 却未熄火）。当前 `_consume_execution_trades` 的 CloseEvent 机制（§3.4）就是为修这个而设计的 |
| D-04 | `_consume_execution_trades` 曾是 O(n²)（按全量 `close_events` 重扫 + 无上限的 processed set） | 六标的九年回测性能问题；当前用 `close_event_id` set 去重，仍是每次遍历全部 `close_events` |
| D-06 | `entry_pending` 闩锁在订单被取消（非状态切换路径）时不释放 → 该标的被永久锁死 | 当前 `_consume_execution_trades` 末尾的 `has_active_open_order(symbol) is False` 自愈分支是补丁 |

### 11.3 设计局限

1. **`health_stats` 跨标的混合计数**（`scope="cross_symbol_aggregate"`）：TrendBreakout/Breakdown 的健康度是所有 symbol 的交易混在一起统计的。一个标的连亏 6 次会熄火**整个策略**在所有标的上的入场。roadmap S0-1 要求改为按 symbol 分组或明确记录语义。
2. **alpha-death 永久且不可自动复活**：`is_alive=False` 后除非重启进程或 `reset_runtime_state()`，该策略再也不出信号。
3. **移动止损未实现**：`context["trailing_stop"]` 被初始化为 `±inf` 但没有任何策略更新它；`hard_stop_exit` 只用固定的初始 `stop_loss`。
4. **`score` 恒为 0**：`PortfolioSignalAllocator` 的排序能力实际上未被利用，同时间戳多候选按字典序而非信号强度分配。
5. **VolatilityReversion 的 ATR 是简化版**：`(high-low).rolling(window).mean()`，不含跳空，止损距离在跳空行情下会偏小。
6. **做空成本未建模**：现货做空被 `account_mode` cap 直接禁止；保证金账户下借券/资金费率成本尚未接入 PnL（roadmap S3-2，标 `not_modeled`）。

### 11.4 死代码与清理项（roadmap D-05/D-07）

- `strategies/mean_reversion.py` 历史上的不可达止损块——当前文件已是紧凑实现（见 §7.3 提示）。
- `strategies/__init__.py` 只有一个 `#`，是空占位。

---

## 12. 关键不变量清单（改代码前务必确认）

1. **信号在 bar `i`，成交在 `i+1`**——所有指标读 `df.iloc[:i+1]`，`submit_order` 不当场成交。
2. **`just_entered` 出场冷却**：新开仓位跳过一次 `should_exit`；但 `hard_stop_exit` 不跳过。
3. **`entry_pending` / `exit_pending` 闩锁**：提交订单被接受后置位，成交回执消费后（`_consume_execution_trades`）或自愈分支清除。绕过它会导致重复下单。
4. **CloseEvent 按 `opening_strategy_id` 归属**：外部平仓（Router/熔断/EndOfBacktest）的 PnL 仍记到开仓策略。
5. **状态切换 ≠ 平仓**：Router 状态切换只 `cancel_symbol_orders` + 冷却，不动已有 lot。
6. **`allowed_states` 与 `regime_map` 双重把关**：两者都同意才会调用策略入场。
7. **clamp 在 gate 之前**：`clamp_entry_qty` 削减 → `check_entry_risk` 布尔确认，两者共用 `_entry_notional_caps`。
8. **熔断 sticky**：`portfolio_breaker_action` 只能由 `manual_resume` 降级，新 high-water 不会自动解除。
9. **指标 `shift(1)` / NaN 前缀**：唐奇安通道、状态机 SMA/ADX/ATR 都不含当前 bar 或显式屏蔽早期值。
10. **`reset_runtime_state()` 清空一切**：`context` / `observed_close_events` / `_consumed_close_event_ids` / `health_stats`（子类 override）。每次回测 run 开头调用。

---

## 附录 A：策略注册与状态映射速查

| 策略类 | 注册名 | `allowed_states` | 当前 regime_map 指向 | governance |
| --- | --- | --- | --- | --- |
| `TrendBreakoutStrategy` | `TrendBreakout` | `TREND_UP`, `VOLATILE` | `TREND_UP` ✅ | admitted |
| `TrendBreakdownStrategy` | `TrendBreakdown` | `TREND_DOWN` | `Cash`（暂停） | paused_redesign |
| `RangeStrategy` | `RangeMeanReversion` | `SIDEWAYS` | `Cash`（暂停） | paused_redesign |
| `VolatilityReversionStrategy` | `VolatilityReversion` | `VOLATILE` | `Cash`（暂停） | isolated_research |
| `PairsTradingModel` | —（不接入 Router） | — | — | research-only |

## 附录 B：信号字典字段

| 字段 | 入场 | 出场 | 说明 |
| --- | --- | --- | --- |
| `action` | `buy` / `short` | `sell` / `cover` | 必填 |
| `stop_loss` | 可选 | — | `> 0` 触发风险定仓；持久化为 lot 的 `initial_risk` |
| `order_type` | 可选（默认 `market`） | 可选 | `market` / `limit` / `stop` |
| `price` | 可选（默认 `close[i]`） | 可选 | 限价/止损单价格，或参考价 |
| `reason` | —（固定 `signal`） | 可选 | 出场原因，进报告与事件流 |
| `score` / `priority` | 可选（默认 0） | — | `PortfolioSignalAllocator` 排序键 |
