# 策略健康生命周期契约（Strategy Health Contract）

> 2026-09-14 修订：当前应用配置启用统一分级恢复，所有自动冷却后从 0.10 起步，
> 晋级要求每级至少 30 天、5 个新退出事件组、3 个币种、正总 R 且去掉最大正 R 后仍为正。
> 新规则、v2→v3 迁移与组合预算见 [本轮完整契约](research/strategy_remediation_contract_20260914.md)。
> 下文 9 月 9 日及更早描述作为历史契约保留，不再代表本轮默认配置；准入保持暂停。

> 2026-09-09 P0 修订：当前应用主配置已显式启用 `extended_cooldown`。
> 两次自动试运行失败后冷却 90 天，再以正常风险预算的 10% 进入 PROBATION；
> 达到新的 cohort 证据门槛后才能恢复 ACTIVE，再次失败重新冷却。
> 以下 v1.0 及“默认关闭”章节记录历史行为，不再描述当前 params.yaml 的有效默认。
> `StrategyHealthPolicy()` 的无配置构造仍保留兼容默认 manual_lock；正式应用必须加载主配置。
> 人工主动锁定和已有持久化 MANUAL_LOCK 不会因更换配置而自动解除；组合清算同样保持约束。
> 本次启用用于解决自动暂停后没有恢复路径的工程问题，未构成研究准入，仍为 paused_revalidation。

当前回测新增逐 bar 的 `strategy_activity`，并在 lifecycle 中导出
`strategy_inactive_days/bars`、`account_inactive_days`、`operating_status`、
`last_fill_at`、`strategy_recovery`。`health_gated_days` 按实际阻止开仓的
观测日计数，不再把某次暂停到数据结束的全部时间都记成停机。
`status=completed` 仅表示引擎完成请求；交易运行状态以 `operating_status`
及逐 bar 状态为准。策略快照给出 `next_recovery_at` 和
`recovery_requires_operator`，区分有期限冷却和人工处置。
终止后的账户现金尾段与被处理 bar 内的策略暂停分别计量；普通账户 BLOCK_NEW
在逐 bar 的 `account_blocks_new_risk` 中单列，不能误认作策略健康锁定。

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

## 9. 默认关闭的延长冷却研究选项（2026-09-06）

60 币种回测确认：原方案于 2022-08-20 因两次试运行失败进入永久人工锁定。
新增 `repeated_failure_action`，默认仍为 `manual_lock`，上文生产默认契约不变。
研究候选 `extended_cooldown` 只替换**未来自动触发的失败次数达限动作**：

1. 两次失败后进入 90 天 COOLDOWN，不产生 MANUAL_LOCK。
2. 到期进入 PROBATION，风险乘数从普通试运行 0.25 降到 0.10。
3. 仅使用本次试运行边界后的新退出 cohort 评判；3 个 cohort 总 R > 0 后恢复 ACTIVE。
4. 再次失败仍冷却完整 90 天，失败计数不会因冷却/重启清零；只有通过后清零。
5. 显式人工锁定和已经持久化的 MANUAL_LOCK 都不会被此选项解锁。

新增研究配置（未写入正式 params.yaml）：

```yaml
repeated_failure_action: extended_cooldown
extended_cooldown_days: 90
recovery_risk_multiplier: 0.10
```

延长冷却不得短于普通冷却，恢复风险必须为正且不大于普通试运行风险。截止日期和失败计数沿用既有 v2 状态持久化；重启必须加载同一策略配置。

风险乘数沿用现有仓位计算及成交后风险核验链路，不新增第二套仓位账本。基础单笔风险 2% × 恢复乘数 10% = 0.2% 的权益风险预算上限（未考虑其他更严限额）；它**不是**仓位市值固定为权益 10%。组合恢复、总风险、相关性簇预算、保护止损、退出权限及实盘准入规则均不改动。

本轮是固定候选工程对照，不是参数寻优或独立样本外验证。重复试运行仍可能持续亏损，不能因为不再永久锁定就认定可以实盘。
脚本：`scripts/run_health_recovery_experiment.py`；测试：`tests/test_health_extended_recovery.py`。

## 10. P0 分级恢复与信号归因（默认关闭）

`recovery_stages: [0.10, 0.25, 0.50, 1.0]` 配合 `recovery_stage_min_days: 30`，
在延长冷却恢复中每次只允许晋升一级；晋级须同时满足新退出 cohort 门槛、正总 R 和最低时间。
每级重置判定样本边界，失败回最低级并冷却；人工锁定不受自动晋级影响。阶段索引随 v2 checkpoint 保存，恢复须使用相同配置。

首次阻断归因与四组固定研究的完整口径、验收、未解决事项见
[`P0 归因与仓位恢复联合报告`](research/p0_attribution_recovery_20260906.md)。
特别注意：风险预算乘数不能替代实际名义仓位准入；小风险恢复与当前 1% 权益最小开仓规则的冲突已经被审计定位，尚未更改生产规则。
