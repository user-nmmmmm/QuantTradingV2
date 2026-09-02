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
| 组合保护 | Daily Loss / Drawdown 熔断（不污染策略健康统计） | `core/risk/circuit_breaker.py` + [`strategy_health_contract.md`](strategy_health_contract.md) |

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

每次检查（无论是否超限）都记录 reserved 与实际风险、比值、动作与原因。
回测写入 `risk_budget_reconciliation.csv`；实盘写入持久状态库并通过
`live_status.json.fill_risk_audit` 导出。回测按 lot 幂等，实盘按持久订单的累计
成交数量幂等；部分成交增加时只提交新增的缩量数量。

`health_risk_multiplier` 来自策略健康生命周期，因此 PROBATION 的 0.25 乘数
会同时收紧 sizing 与这里的预算，两处口径一致。

减仓单走普通订单通道。回测在下一根 bar 成交；实盘在同步到权威 fill 后立即提交
reduce-only `GapRiskResize`。实盘扫描持久订单账本而不是只监听内存 callback，
因此重启发生在 fill 与核验之间时仍会补做核验；缩量拒绝会把引擎降级、发 critical
告警并停止该 tick 的新策略工作。

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

实盘接入行为：账户同步后、策略评估前先对账，tick 结束时再对账一次以接收最新
trail；`flatten` 会把 `_operational_state` 置为
`DEGRADED` 并发 `position_unprotected` critical 告警，再用 reduce-only 市价单
平掉裸仓；任何执行异常同样降级并告警（fail closed）。
保护单状态经 `live_trading/state_export.py` 的 `protective_orders` 字段导出。
开关：`config/params.yaml` 的 `protective_orders.enabled`。

## 8. 回测的 intrabar 止损等价性（STR-P1-01，已关闭）

实现：[`backtest/protective_stops.py`](../backtest/protective_stops.py)
（`ResidentStopSimulator`）、`core/broker/matching.py` 的 `process_orders(...,
order_filter=...)`、`backtest/engine.py` 的每根 bar 三段式顺序。
测试：[`tests/test_sr2_backtest_intrabar_stops.py`](../tests/test_sr2_backtest_intrabar_stops.py)（15）。
开关：`config/params.yaml` 的 `protective_orders.backtest_resident`。

原先回测是「bar 收盘后发现穿越、下一根 bar 市价退出」，与交易所常驻
stop-market 不等价：止损价格与时点都被系统性地美化。现在回测持有的是
**同一个** `ProtectiveOrderManager` 产生的常驻止损意图——实盘
`tick_orchestrator` 驱动的就是这个对象——并由历史撮合器在 bar 内成交。

### 预注册的保守 bar 内路径

OHLC 不说明极值先后，所以只允许一条路径，且不得逐根 bar 挑选：

```text
open -> 不利极值 -> 有利极值 -> close
```

多头即 `open -> low -> high -> close`。由此得到的三条后果都是保守的：

| 情形 | 结果 |
| --- | --- |
| armed 期间任意 bar 触及 low ≤ stop | 触发，不会漏掉任何一次穿越 |
| bar 跳空穿过止损价 | 成交价 = `min(open, stop)`（空头 `max`），不允许成交在市场没有走到的价位 |
| 同一根 bar 既能打止损又能吃到有利极值 | 先走不利极值，判为止损 |

### 一根 bar 内的顺序

1. 上一根 bar 排队的市价/限价单在本 bar **开盘**成交（入场、策略退出）；
2. `ResidentStopSimulator.step`：先按此刻的真实净持仓对账保护单，再把常驻
   止损单撮合到本 bar；
3. 策略仓位管理与入场信号在**收盘**运行。

对账发生在 (1) 之后，所以本 bar 开盘成交的入场在**自己这根 bar 内**就受保护
（不再存在裸露的入场 bar）；已经在开盘被平掉的仓位，其残留止损在这里被取消而
不是留到下一根 bar 触发。期望止损价在 (3) 之前读取，因此生效的永远是上一根
已完成 bar 推出的价位——无前视。

这个分工由订单过滤器强制：(1) 走
`SimulatedExecutionAdapter.on_market_data`，它用 `_is_not_protective_stop`
把常驻止损排除在外；(2) 用 `order_filter=is_protective_stop` 只撮合止损。缺少
(1) 的过滤器时，**在更早的 bar 挂出、此后一直静止的止损**会在 (1) 里成交——净
持仓结果相同，但成交既不进 `stop_order_audit` 也不计入 `triggered_stops`，而且
绕过了 (2) 的撤单对账。因为移动止损每根 bar 都会 cancel-replace、静态止损不会，
这个漏记是按策略类型有偏的。回归见
`tests/test_backtest_stop_pass_and_liquidity.py`。

两遍撮合共用同一份 bar 成交量预算（`MatchingMixin._bar_volume_budget`），止损
那一遍不会拿到新的参与率额度。

### 唯一权威 close

`force_liquidate`（DailyLoss / Drawdown / 强平）与 `EndOfBacktest` 现在都会先
取消常驻止损单，再作为权威 close 成交；部分减仓后由下一次对账按新数量重新挂
出。这与实盘「持仓归零取消全部残留保护单」是同一条不变量，回测侧不会出现同一
批仓位被卖两次。

### 与实盘的一处有意差异

实盘遇到「有持仓但没有可用止损价」会 fail closed 平仓；回测把这种情况记为
`no_protective_level` 审计行并计入 `unprotected_position_bars`，**不平仓**。
回测里没有止损价通常意味着这个研究臂本来就不定义止损，直接平掉会悄悄改写研究
问题。该计数会写进 `report.txt` 的 Protective Stop Execution 分节，所以条件是
可见的，而不是被隐藏或被替研究者做了决定。

### 产物

`reports/.../stop_order_audit.csv`：每一次 place / replace(上移或数量对齐) /
cancel / fill，带当时生效的止损价、实际成交价与路径假设；`report.txt` 的
Protective Stop Execution 分节声明本次运行用的是常驻 intrabar 口径还是旧的
next-open 口径（后者会打 WARNING，因为它相对实盘偏乐观）。
