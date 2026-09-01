# 策略健康生命周期契约（Strategy Health Contract）

> 文档状态：Active v1.0
> 生效日期：2026-09-01
> 实现：[`core/strategy_health.py`](../core/strategy_health.py)、[`strategies/trend_breakout.py`](../strategies/trend_breakout.py)
> 测试：[`tests/test_sr1_strategy_health.py`](../tests/test_sr1_strategy_health.py)
> 上位文档：[`current_strategy_remediation_roadmap.md`](current_strategy_remediation_roadmap.md) SR1

## 1. 本契约解决的问题

旧实现用一个布尔量 `is_alive` 表示"alpha 是否还活着"，并按**逐币种平仓**计数连续亏损：

- `is_alive=False` 没有到期时间，没有事件，没有报告字段，也没有任何恢复路径；
- 一次 `DailyLossLimit` 同时关闭 15 个高度相关的币种，被记成 15 次独立失败；
- 结果是 2021-09-22 之后策略静默停机，报告仍显示 `status=completed`、`inactive_bars=0`。

本契约把健康度改成**可观测、可恢复、可审计的状态机**，观测单位从"逐笔平仓"改为"退出 cohort"。

## 2. 状态机

```text
ACTIVE ──连续亏损 cohort 达阈值──> COOLDOWN ──到期──> PROBATION ──通过──> ACTIVE
                                     ▲                    │
                                     └──观察期失败────────┘
                                                          │ 失败次数达上限
                                                          ▼
                                                     MANUAL_LOCK
```

| 状态 | 新开仓 | 已有仓位管理 | 风险乘数 | 离开方式 |
| --- | --- | --- | ---: | --- |
| ACTIVE | 允许 | 必须 | 1.00 | 触发阈值 → COOLDOWN |
| COOLDOWN | 禁止 | 必须 | 0.00 | `cooldown_until` 到期 → PROBATION |
| PROBATION | 允许 | 必须 | `probation_risk_multiplier`（默认 0.25） | 达标 → ACTIVE；不达标 → COOLDOWN |
| MANUAL_LOCK | 禁止 | 必须 | 0.00 | 只能 `manual_resume(approved_by, reason)` → PROBATION |

不变量：

1. **健康闸门只拦新风险**（REG-01）。`check_health()` 只在 `should_enter` 前调用；`should_exit`、`hard_stop_exit` 和外部平仓永远不受影响。
2. **COOLDOWN 一定有到期时间**。若进入冷静期时没有任何已知时间（例如调用方未提供 timestamp），机器会在观察到第一个真实时间时立即锚定 `cooldown_started_at` / `cooldown_until`，绝不退化成永久开关。
3. **到期只进入 PROBATION**，不直接恢复满风险。
4. **MANUAL_LOCK 不会因为时间流逝或后续盈利自动解除**。
5. **盈利不能绕过状态机**。盈利会清零 `consecutive_negative_cohorts`，但 COOLDOWN 仍需等到期。
6. 每次迁移都写入 `transitions`（时间、from、to、reason、trigger_event_id、risk_multiplier），并随状态一起持久化。

## 3. 权威观测单位：退出 cohort

```text
cohort_id = opening_strategy : exit_session : exit_controller : risk_action_id
```

- `exit_session`：平仓 fill 的 UTC 日期（无 timestamp 时退化为 `bar-<index>`，避免把无关平仓合并）。
- `exit_controller`：由 `exit_reason` 判定
  - `strategy`：`signal`、`hard_stop`、Donchian 出场等策略自身退出；
  - `account_risk`：`DailyLossLimit`、`AccountLiquidation`、`MarginLiquidation`、`DrawdownReduce`；
  - `router`：`MaxHoldingPeriod`、`StateSwitch`、`Regime ... Not Allowed`；
  - `system`：`EndOfBacktest`。
- `risk_action_id`：组合级风险动作 id（breaker transition / daily action / epoch）。由
  `Broker.force_liquidate(..., risk_action_id=...)` 透传到 `Order` 和 `CloseEvent`，
  因此**同一个动作关闭 N 个币种只产生 1 个 cohort**。

每个 cohort 聚合 `net_pnl`、`initial_risk`（按平仓数量占比从 lot 上分摊）、`trade_count`、
`symbols`、`opened_at/closed_at`，并给出

```text
R = net_pnl / initial_risk
```

阈值只使用 R，不使用未标准化的美元 PnL。若某 cohort 没有记录 initial_risk，
`r` 退化为 ±1 并置 `r_is_estimated=true`，绝不静默丢弃该观测。

### 3.1 与 AccountRisk 的解耦（STR-P0-04 / SR3-3）

`counted_controllers` 默认是 `[strategy, router]`。`account_risk` cohort 仍然被记录、
仍然进入 `cohort_trades.csv` 和归因，但**不会**触发健康迁移：组合熔断批量平仓
证明的是组合风险被触发，不是 alpha 失效。

### 3.2 幂等性

`ingest_close` 以 `close_event_id` 去重。重复投递同一个 CloseEvent（重启重放、
多次 `_consume_execution_trades`）不会重复计数，重启后重放历史事件也不会。

## 4. 配置

`config/params.yaml` 的 `strategy_health` 段（经 `composition.factory` 注入，
策略自身不读 config，遵守 `tests/test_architecture_boundaries.py` 的依赖方向）：

```yaml
strategy_health:
  enabled: true
  consecutive_negative_cohorts: 3   # 候选 [2, 3, 4]
  cooldown_days: 30                 # 候选 [14, 30, 60]
  probation_risk_multiplier: 0.25   # 候选 [0.25, 0.50]
  probation_required_cohorts: 3     # 候选 [3, 5, 10]
  probation_min_total_r: 0.0
  max_failed_probation_cycles: 2
  rolling_cohort_window: 20
  counted_controllers: [strategy, router]
```

这些值是**注册在案的研究候选中位数**，不是已验证的生产常数，见
[`research/current_strategy_experiment_registry.jsonl`](research/current_strategy_experiment_registry.jsonl)。

## 5. 持久化

`StrategyHealthMachine.to_dict()` / `.load()` 使用 schema `strategy_health/v2`，
经 `Strategy.bind_state_store` 写入状态库，键为 `strategy_health:<name>`。
所有时间都是绝对 UTC timestamp，**禁止**用单次 run 的 bar index 表示冷静期，
因此重启后冷静期到期时间不漂移。schema 不匹配的旧 payload 会被忽略（回到默认 ACTIVE），
而不是被误解成半个状态。

## 6. 报告与产物

| 产物 | 内容 |
| --- | --- |
| `report.txt` → `Strategy Health Lifecycle` | 每个策略的完整生命周期字段 |
| `report.txt` → `Strategy Activity Consistency` | 最长零交易间隔、被抑制信号数、P0 findings |
| `strategy_health.json` | `strategy_health` 快照 |
| `strategy_health_timeline.csv` | 全部状态迁移事件 |
| `cohort_trades.csv` | 全部 cohort（含 account_risk cohort） |
| `suppressed_setups.csv` | 每策略 raw / suppressed setup 计数与时间 |
| `BacktestLifecycle` | `strategy_health_status`、`disabled_or_cooldown_at`、`health_gated_days`、`probation_periods`、`suppressed_raw_setups`、`shadow_setup_count`、`health_transition_log` |

### 6.1 一致性检查（SR1-4）

`core.diagnostics.strategy_activity_consistency` 在以下条件同时成立时产生 **P0** 诊断：

- 最长零交易间隔 ≥ 365 天，且
- 该间隔内确实存在 raw setup（`last_raw_setup_at` 落在窗口内），或存在被健康闸门抑制的 setup。

"市场确实没有信号"不会被误报为缺陷；"策略被静默关闭"一定会被报出来。

## 7. 实盘接口

- `live_trading/state_export.py` 把 `strategy_health` 写进 live_status JSON，
  并把「每个策略的状态」纳入 critical-state signature：健康迁移会立即触发一次
  fsync 落盘，而不是等下一个周期导出。
- `live_trading/tick_orchestrator.py` 在每个 tick 结束时把新产生的迁移作为
  `strategy_health_transition` 告警发出，每条迁移只发一次；
  迁移到 `manual_lock` 是 `critical`，其余是 `warning`。

## 8. 未纳入本契约的部分

- SR1-3 的阈值搜索本身（本契约只提供可搜索的机器与注册表）；
- SR2-5 的保护单生命周期（见 [`protective_stop_contract.md`](protective_stop_contract.md)）。
