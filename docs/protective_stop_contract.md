# 保护性止损契约（Protective Stop Contract）

> 文档状态：Active v1.0
> 生效日期：2026-09-01
> 实现：[`core/protective_stops.py`](../core/protective_stops.py)、[`strategies/trend_breakout.py`](../strategies/trend_breakout.py)、[`backtest/engine.py`](../backtest/engine.py)
> 测试：[`tests/test_sr2_protective_stops.py`](../tests/test_sr2_protective_stops.py)
> 上位文档：[`current_strategy_remediation_roadmap.md`](current_strategy_remediation_roadmap.md) SR2

## 1. 三层退出保护

| 层 | 作用 | 实现 |
| --- | --- | --- |
| 单笔初始风险 | 结构止损与 ATR 最大风险距离的混合 | `plan_initial_stop` |
| 盈利保护 | 只上移的 Chandelier ATR 追踪止损（可选保本） | `update_trailing_stop` |
| 组合保护 | Daily Loss / Drawdown 熔断（不污染策略健康统计） | `core/risk_circuit_breaker.py` + [`strategy_health_contract.md`](strategy_health_contract.md) |

## 2. 无前视契约（SR2-1）

所有止损参数只使用**信号 bar（已完成 bar）**上的 ATR、Donchian 极值和 high/low。
不使用未来 bar，也不使用入场成交后才知道的价格来做原始仓位排序。

在 bar `i` 生效的止损，永远是在 bar `i-1` 计算出来的：`Strategy.on_bar` 先执行
`hard_stop_exit`，再执行 `should_exit`（追踪止损在这里上移），因此当根 bar 的
trail 更新不可能回过头改变本 bar 已经检验过的止损位。

`tests/test_sr2_protective_stops.py::TestNoLookahead` 用「改写 `i` 之后的所有 bar，
`i` 处的止损必须不变」来固定这条契约。

## 3. 混合初始止损（SR2-2）

多头：

```text
structural_stop = 前一段 Donchian low(exit_window)
atr_stop        = signal_reference_price - initial_atr_multiple * ATR
planned_stop    = max(structural_stop, atr_stop)      # 取更紧的一侧
```

空头镜像（取 `min`）。随后按预注册的风险距离带做检查：

| 情况 | 处置 |
| --- | --- |
| 距离 < `min_stop_distance_pct` | **拒绝信号**（`stop_too_close`），不允许被极小止损放大成巨额仓位 |
| 距离 > `max_stop_distance_pct` | 夹到最大距离（`clamped_max_distance`），仓位随之缩小 |
| 两条腿都不可用 | **拒绝信号**（`no_valid_stop_level`） |

**被删除的行为**：旧代码在 Donchian 止损无效时静默使用 `close * 0.95`（空头 `1.05`）。
这个隐式 5% fallback 掩盖了正是本 roadmap 需要看到的异常，现在一律改为显式拒绝。
每个信号都会带上 `stop_plan`（method / structural_stop / atr_stop / reject_reason），
方便审计到底用了哪条规则。

`use_atr_initial_stop` 默认 **false**：SR2-2 要求先在训练/验证集上比较
A（纯 Donchian）/ B（纯 ATR）/ C（混合）三条腿，冻结基线跑的仍是 A。

## 4. Chandelier 追踪止损（SR2-3）

```text
candidate = highest_high_since_fill - trailing_atr_multiple * ATR      # 多头
new_stop  = max(old_stop, initial_stop, candidate)
```

**硬性不变量：多头保护价只能上移**。ATR 扩大只会让 candidate 更低，
而 `max` 保证已经赚到的保护不会被吐回去；空头镜像（`min`，只能下移）。
`tests/.../TestTrailingStopMonotonicity` 用随机 ATR 路径（含剧烈扩张）做 property test。

context / 持久状态记录：`highest_high_since_fill`（空头 `lowest_low_since_fill`）、
`initial_stop`、`trailing_stop`、`effective_stop`，以及生效的 `stop_loss`。

可选保本规则（`breakeven_after_r`）默认关闭；启用时只有在达到预注册的 R 倍数后
才把止损移到 `entry ± breakeven_cost_buffer`，缓冲即预估往返成本，
避免"保本"实际上是一个确定的小亏。

## 5. 成交后风险重核（SR2-4）

仓位是按**信号收盘价**定的，成交发生在下一根 bar 的开盘，可能跳空。因此每个新开的 lot 都要重算一次：

```text
actual_risk_per_unit = |actual_fill_price - protective_stop|
actual_total_risk    = actual_risk_per_unit * filled_qty
risk_budget          = fill_time_equity * base_risk_per_trade * health_risk_multiplier
```

- `actual_total_risk <= risk_budget * (1 + tolerance)`：通过，只记录；
- 超限且 `action=resize`：提交具名 `GapRiskResize` 减仓单，把数量降到
  `risk_budget / actual_risk_per_unit`；若剩余不足 `min_remaining_fraction`，
  直接平掉整个 lot（不留灰尘仓位）；
- 超限且 `action=audit_only`：只记录不交易（研究模式）。

每次检查（无论是否超限）都写一行到 `risk_budget_reconciliation.csv`，
字段包含 reserved 与实际风险、比值、动作与原因。每个 lot 只检查一次。

`health_risk_multiplier` 来自策略健康生命周期，因此 PROBATION 的 0.25 乘数
会同时收紧 sizing 与这里的预算，两处口径一致。

减仓单走普通订单通道，在下一根 bar 成交：这是真实系统在「刚刚知道成交价」之后
最早能采取的动作，不假装可以在同一时刻回到过去。

## 6. 配置

```yaml
stops:
  use_atr_initial_stop: false     # SR2-2 A/B/C 比较前保持 A
  use_trailing_stop: false
  atr_period: 14
  initial_atr_multiple: 2.0       # 候选 [1.5, 2.0, 2.5, 3.0]
  trailing_atr_multiple: 3.0      # 候选 [2.5, 3.0, 4.0]
  min_stop_distance_pct: 0.005
  max_stop_distance_pct: 0.35
  breakeven_after_r: null
  breakeven_cost_buffer: 0.0

entry_risk:
  enabled: true
  tolerance: 0.10
  action: resize                  # resize | audit_only
  min_remaining_fraction: 0.10
```

与健康策略一样，参数在 `composition.factory` 注入，策略自身不读 config。

## 7. 保护单生命周期（SR2-5）

实现：[`core/protective_orders.py`](../core/protective_orders.py)，
实盘接入点在 `live_trading/tick_orchestrator.py::_reconcile_protective_orders`。

`ProtectiveOrderManager` 是一个**对事实的纯状态机**：输入当前净持仓、策略希望
生效的保护价、交易所报告的保护单；输出需要执行的动作
（`place` / `replace` / `cancel` / `flatten` / `none`）。它自己不接触交易所，
因此实盘、沙盒故障注入和 replay 可以驱动同一个判定函数。

强制的不变量（每条都有测试）：

| 不变量 | 行为 |
| --- | --- |
| 入场未成交前不能假设止损已生效 | `entry_pending` 且净持仓为 0 → `PENDING_ENTRY`，不产生任何保护单 |
| 保护数量等于净持仓 | 部分成交后数量不符触发 `replace`（`reason=qty_mismatch`） |
| 只上移 | 期望价更松时忽略；cancel-replace 在途时用记忆中的已接受价继续 ratchet |
| 只能有一个权威 close | 持仓归零时取消**全部**残留保护单；重复保护单只保留最紧的一条（OCO 泄漏） |
| 未知状态不是保护 | `unknown` / `pending_cancel` → `FAILED` + `flatten`，绝不当作"有止损" |
| 没有可用保护价 | 同样 `flatten`，不允许裸持仓 |
| 重启从交易所恢复 | `reconcile_after_restart` 以交易所订单 + 持仓为准：缺失的重建，孤儿单取消 |

实盘接入行为：每个 tick 结束时对账；`flatten` 会把 `_operational_state` 置为
`DEGRADED` 并发 `position_unprotected` critical 告警，再用 reduce-only 市价单
平掉裸仓；任何执行异常同样降级并告警（fail closed）。
保护单状态经 `live_trading/state_export.py` 的 `protective_orders` 字段导出。
开关：`config/params.yaml` 的 `protective_orders.enabled`。

## 8. 仍然未关闭：回测的 intrabar 止损等价性（STR-P1-01）

回测目前仍然是「bar 收盘后发现穿越、下一根 bar 市价退出」，与交易所常驻
stop-market 不等价，止损价格与时点存在失真。要关闭它，需要让回测撮合器也持有
`ProtectiveOrderManager` 产生的常驻止损意图，并按预注册的保守 OHLC 路径规则
在 bar 内触发。这是 SR2 的最后一项，也是 `reports/.../stop_order_audit.csv`
在回测侧落地的前提；任何真实资金放行前必须先关闭。
