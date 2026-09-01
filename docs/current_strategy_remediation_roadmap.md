# 当前策略修复、止损升级与重新准入 Roadmap

> 文档状态：Active v1.0  
> 生效日期：2026-08-31  
> 适用基线：`reports/20260831_214432_3239d_30Syms_Ret173.8pct` 及当前 `TrendBreakout` 生产路由  
> 上位路线图：[`unified_roadmap.md`](unified_roadmap.md)  
> 相关路线图：[`strategy_development_roadmap.md`](strategy_development_roadmap.md)、[`backtest_optimization_roadmap.md`](backtest_optimization_roadmap.md)  
> 放行原则：本 Roadmap 全部 P0/P1 和重新准入门槛完成前，`TrendBreakout` 仅允许研究、shadow 或 sandbox，不得按“已验证生产 Alpha”扩大真实资金。

## 1. 目标与非目标

本计划解决当前唯一生产策略在 2021 年被健康闸门永久关闭、报告仍显示正常运行，以及止损、组合风险、账户成本和研究证据不足等问题。最终目标不是让一次回测收益更高，而是让系统能够可靠回答：策略是否工作、为什么暂停、何时恢复、每笔真实风险是多少，以及该 Alpha 是否通过独立样本外验证。

本计划同时引入三层退出保护：

1. 单笔初始风险：Donchian 结构止损与 ATR 最大风险距离的混合止损；
2. 盈利保护：只上移、不下移的 Chandelier ATR 追踪止损；
3. 组合保护：保留 Daily Loss / Drawdown 控制，但不再污染策略健康统计，也不停止已有仓位管理。

本计划不预设 20/10、ATR 倍数、冷静期长度或恢复阈值一定有效；这些数值必须通过训练集和验证集研究，最终 holdout 不允许参与选择。

## 2. 当前事实基线

最新 30 标的、日线、spot-margin 报告覆盖 2017-08-17 至 2026-06-30：

| 指标 | 当前结果 | 解释 |
| --- | ---: | --- |
| 总收益 | 173.81% | 绝对收益为正，但不是完整九年持续交易结果 |
| CAGR | 12.03% | 包含 2022–2026 长期零交易现金期 |
| Sharpe | 0.992 | 包含大量零收益日，不等于 active strategy Sharpe |
| 最大回撤 | 13.23% | 当前仍比 2021 高点低约 9.09% |
| 水下时间占比 | 94.54% | 资金绝大部分时间未创新高 |
| 闭合交易 | 103 | 其中 57 笔由 DailyLossLimit 批量关闭 |
| PF | 4.65 | 逐交易样本高度相关，独立事件数明显小于 103 |
| AccountRisk 退出 PnL | 13,309.66 | 占总净利润约 76.6% |
| Top 10 盈利贡献 | 62.3% | 仍存在明显右尾依赖 |
| BTC/ETH 固定基准 | 1004.31% | 策略累计落后 830.50 个百分点 |
| 2022–2026 | 0 笔、0% | 不是市场无机会，而是策略健康状态永久关闭 |

直接根因时间线：

```text
2021-09-07  DailyLossLimit 批量关闭多个高度相关币种
      ↓
逐币种 CloseEvent 被当作独立健康 observation
      ↓
2021-09-21  NEAR 亏损，跨币种连续亏损达到 5
      ↓
2021-09-22  DOT 亏损，跨币种连续亏损达到 6
      ↓
TrendBreakout.check_health() 将 is_alive 永久设为 False
      ↓
2021-10-10  后续盈利只清零 consecutive_losses，不恢复 is_alive
      ↓
2022–2026  Router 仍记录 candidate，但 should_enter 永久返回 None
      ↓
报告错误显示 status=completed、active_end=2026、inactive_bars=0
```

## 3. 问题登记册

### 3.1 P0：阻止任何生产放行

| ID | 问题 | 影响 | 当前证据 |
| --- | --- | --- | --- |
| STR-P0-01 | `is_alive=False` 是无到期时间的永久死亡开关 | 唯一生产策略可静默停机数年 | 2021-09-22 后无新交易 |
| STR-P0-02 | 健康统计按跨币种逐笔平仓计数 | 同一系统性冲击被重复当作多个独立失败 | 同一次 DailyLossLimit 关闭多个相关币种 |
| STR-P0-03 | 健康停机未进入生命周期、报告或告警 | `completed` 与真实 inactive 状态矛盾 | `inactive_bars=0`，但 2022–2026 零交易 |
| STR-P0-04 | 健康闸门与外部风险退出耦合 | AccountRisk 批量退出可触发 Alpha Death | 57/103 笔由 DailyLossLimit 关闭 |
| STR-P0-05 | `TrendBreakout: admitted` 缺少当前版本独立 holdout 证据 | 研究治理标签高于真实证据等级 | 参数平台、因子消融、跨市场证据仍 pending |

### 3.2 P1：回测和真实风险可能显著失真

| ID | 问题 | 影响 |
| --- | --- | --- |
| STR-P1-01 | 当前 hard stop 是收盘后发现穿越、下一 bar 市价退出 | 不等同于交易所常驻 stop-market，止损价格和时点失真 |
| STR-P1-02 | 仓位按信号收盘价定仓，未按真实 fill 重新核验 | 突破跳空后实际风险可超过配置的 2% |
| STR-P1-03 | 所有 EntryCandidate 默认 score=0 | 资金不足时实际按币种字母排序，而不是按 Alpha/边际风险排序 |
| STR-P1-04 | 单币 30%、总杠杆 3 倍，但无相关性簇或 crypto beta 上限 | 多个币种表面分散，系统性下跌时近似单一杠杆风险 |
| STR-P1-05 | spot-margin 杠杆多头未计报价币借款利息 | 历史最大 gross/equity 约 1.456，但 financing ledger 为空 |
| STR-P1-06 | 账户是 spot-margin，手续费注释却采用 futures taker rate | 交易场所、费率和融资语义不一致 |
| STR-P1-07 | 策略类允许 VOLATILE，生产路由却把 VOLATILE 路由到 Cash | 代码语义与实际能力不一致，运行范围容易被误读 |
| STR-P1-08 | 收益主要由组合熔断退出贡献 | 当前 PF 不能直接归因于 20/10 Donchian Alpha |

### 3.3 P2：研究可信度、数据和报告问题

| ID | 问题 | 影响 |
| --- | --- | --- |
| STR-P2-01 | 使用静态现存 30 币种，未提供 point-in-time universe | 存在幸存者偏差和未来选币信息 |
| STR-P2-02 | 本地数据 `exchange=null` | 无法证明数据、费率和账户属于同一交易场所 |
| STR-P2-03 | 611 个异常/大跳变 bar 仅标记，15 个 fill 位于异常 bar | 关键盈亏可能受数据异常或极端行情影响 |
| STR-P2-04 | 头部交易第二数据源核验为 `unverified` | Top 10 贡献 62.3%，但未完成市场数据复核 |
| STR-P2-05 | PF 将同日相关批量退出当作独立样本 | 置信区间可能明显偏窄 |
| STR-P2-06 | “0/10 负收益年份”把六个零交易年份当作稳定表现 | 报告表述误导 |
| STR-P2-07 | active/full period 指标未识别策略健康停机 | CAGR、Sharpe 和 opportunity cost 口径不正确 |
| STR-P2-08 | 20/10、OBV、ADX、稳定期、冷却期、风险阈值缺少完整稳定性证据 | 一次历史结果不足以证明参数稳健 |

### 3.4 已修复但必须防回归的问题

以下历史问题不是本次零交易的直接原因，不应重复诊断为当前根因，但必须保留回归测试：

| ID | 已修复行为 | 回归要求 |
| --- | --- | --- |
| REG-01 | `BLOCK_NEW` 不再整体跳过 Router | 禁止新仓时仍必须处理已有仓位、hard stop 和策略退出 |
| REG-02 | `LIQUIDATE/LOCKED` 回测生命周期显式终止 | 不允许清算后多年静默遍历并伪装 active |
| REG-03 | CloseEvent 以 opening strategy 归因并完整回调 | lifecycle coverage 必须保持 100% |
| REG-04 | Router 状态变化停止新风险而不抢占 lot 所有权 | 退出控制器必须可审计且不重复平仓 |

## 4. 目标架构

### 4.1 策略健康状态机

```text
ACTIVE
  │ 独立亏损 cohort 达到阈值
  ▼
COOLDOWN
  │ 到达持久化的 cooldown_until
  ▼
PROBATION
  ├─ 观察期通过 ──────────────> ACTIVE
  └─ 观察期失败 ─> COOLDOWN
                         │ 连续失败达到上限
                         ▼
                    MANUAL_LOCK
```

状态语义：

| 状态 | 新开仓 | 已有仓位管理 | 风险乘数 | 恢复方式 |
| --- | --- | --- | ---: | --- |
| ACTIVE | 允许 | 必须 | 1.00 | 正常运行 |
| COOLDOWN | 禁止 | 必须 | 0.00 | 时间到期后进入 PROBATION |
| PROBATION | 允许 | 必须 | 研究初值 0.25 | 满足预注册表现门槛 |
| MANUAL_LOCK | 禁止 | 必须 | 0.00 | 人工审查并记录原因 |

`COOLDOWN` 不是死亡；`MANUAL_LOCK` 也不能静默。每次迁移必须产生持久事件、报告字段和告警。

### 4.2 健康 observation 的正确单位

权威统计单位改为退出 cohort，而不是逐币种 close：

```text
cohort_id = opening_strategy
          + exit_session
          + exit_controller
          + risk_action_id/breaker_epoch
```

每个 cohort 聚合：

```text
net_pnl
initial_risk
R = net_pnl / initial_risk
symbols
trade_count
exit_controller
correlation_cluster
```

同一个 DailyLossLimit action 即使关闭 15 个币，也只能贡献一个健康 observation。正常策略退出若属于同一市场 episode，可按 UTC session 或预注册的 episode 规则聚合。阈值研究必须使用 cohort R，不再使用未标准化的美元 PnL 均值。

### 4.3 新止损栈

多头初始止损研究公式：

```text
structural_stop = previous Donchian low(exit_window)
atr_stop        = signal_reference_price - initial_atr_multiple * ATR
planned_stop    = max(structural_stop, atr_stop)
```

实际成交后必须重新计算：

```text
actual_risk_per_unit = actual_fill_price - protective_stop
actual_total_risk    = actual_risk_per_unit * filled_qty
risk_budget          = fill_time_equity
                     * base_risk_per_trade
                     * health_risk_multiplier
```

若实际风险超限，必须在进入资金风险前缩减数量、拒绝剩余数量，或立即具名减仓；不得只保留信号收盘价的估算。

Chandelier 追踪止损：

```text
candidate_trailing_stop = highest_high_since_fill
                        - trailing_atr_multiple * ATR
new_stop = max(old_stop, initial_stop, candidate_trailing_stop)
```

硬性不变量：多头保护价只能上移，不能因 ATR 扩大而下移。可选保本规则只有在达到预注册的 R 倍数后启用，并包含预估往返成本。

### 4.4 保护单生命周期

```text
EntryIntent
  -> EntryFill
  -> 创建 reduce-only StopMarket
  -> bar/行情推进后 cancel-replace 上移止损
  -> StopFill 或 StrategyExit 或 AccountRiskExit
  -> 取消其余退出意图/OCO sibling
  -> 对账后 PositionFlat
```

回测和实盘必须共享以下契约：

- 入场未成交前不能假设止损已生效；
- 入场成交后保护单必须及时建立；
- 部分成交后保护数量等于净持仓；
- 止损、策略退出和组合强平之间只能有一个权威 close；
- 重启后从交易所和订单账本恢复保护单，不能只依赖内存 context；
- 同一 bar 的 OHLC 路径不明确时使用预注册、保守且确定性的撮合规则。

## 5. 实施阶段总览

```text
SR0 证据冻结与治理
 └─> SR1 健康生命周期与报告修复
      ├─> SR2 保护性止损和实际风险核验
      └─> SR3 组合风险、候选排序与账户成本
              └─> SR4 数据与研究基础
                    └─> SR5 样本外重新准入
                          └─> SR6 Shadow / Sandbox / Paper 放行
```

| 阶段 | 名称 | 主要关闭问题 | 退出条件摘要 |
| --- | --- | --- | --- |
| SR0 | 证据冻结与治理 | STR-P0-05、口径混淆 | 当前策略状态改为 `paused_revalidation`，基线可重复 |
| SR1 | 健康生命周期 | STR-P0-01～04、STR-P2-06～07 | 不再永久静默；报告准确标记 inactive/probation |
| SR2 | 止损升级 | STR-P1-01～02 | 保护单可恢复、只上移、实际风险不超预算 |
| SR3 | 组合与账户风险 | STR-P1-03～08 | 排名有经济含义；相关性和融资成本进入账本 |
| SR4 | 数据与研究基础 | STR-P2-01～05、08 | PIT universe、第二源核验、事件级统计完成 |
| SR5 | 重新准入 | STR-P0-05 | 冻结 holdout 全部门槛通过或明确拒绝 |
| SR6 | 运行放行 | 全部 | shadow/sandbox/paper 无未解释差异后才允许灰度 |

## 6. SR0：证据冻结与治理

### SR0-1 冻结最新事实基线（P0）

保存并登记：配置 hash、代码 SHA、30 标的数据 hash、103 个 CloseEvent、equity、routing、breaker audit、health 重建结果和基准结果。新增一个机器可读基线，明确记录 `strategy_disabled_at=2021-09-22` 的预期诊断。

**验收：**同一代码和数据运行三次，orders、fills、CloseEvents、health transitions 和最终权益在容差内一致。

### SR0-2 修正策略治理状态（P0）

在完成 SR5 前，将配置中的 `TrendBreakout: admitted` 改为 `paused_revalidation` 或等价状态；运行入口必须拒绝把未准入策略解释为 production-ready，但允许显式 research/shadow。

**验收：**manifest、报告、运行日志和 dashboard 对治理状态显示一致。

### SR0-3 建立实验注册边界（P1）

登记所有将被研究的参数族，而不是登记每一个最优结果后才计算 trial count：health cohort 阈值、cooldown、probation、ATR 周期与倍数、breakeven R、Donchian 20/10、OBV 开关、状态参数和风险阈值。

**验收：**任何参数实验先有 hypothesis、数据分区和 experiment ID；holdout 不可用于排序。

## 7. SR1：健康生命周期与报告修复

### SR1-1 用结构化状态替换 `is_alive`（P0）

责任文件：`strategies/trend_breakout.py`、`strategies/base.py`、`core/state_store_v2.py` 或现有状态存储适配层。

新增持久字段：

```text
status
status_changed_at
cooldown_started_at
cooldown_until
trigger_event_id
trigger_reason
consecutive_negative_cohorts
rolling_cohort_r
probation_closed_cohorts
probation_total_r
probation_risk_multiplier
failed_probation_cycles
manual_lock_reason
```

禁止使用不可持久的单次 run bar index 表示实盘冷静期；统一存储 UTC timestamp。

### SR1-2 建立 CloseEvent cohort 聚合器（P0）

责任文件建议：新增 `core/strategy_health.py`，由权威 CloseEvent 和 risk action id 构建 cohort。聚合器必须幂等；重复投递同一 CloseEvent 不能重复计数。

### SR1-3 冷静期、观察期和人工锁定（P0）

第一轮研究候选而非生产结论：

```yaml
consecutive_negative_cohorts: [2, 3, 4]
cooldown_days: [14, 30, 60]
probation_risk_multiplier: [0.25, 0.50]
probation_required_cohorts: [3, 5, 10]
```

恢复规则必须使用 cohort R 和策略回撤，不得使用未标准化美元 PnL。达到时间只进入 PROBATION，不直接恢复满风险。

### SR1-4 生命周期、报告与告警（P0）

责任文件：`backtest/engine.py`、`backtest/reporting.py`、`backtest/writers.py`、`live_trading/state_export.py`、告警适配层。

新增输出：

```text
strategy_health_status
disabled_or_cooldown_at
inactive_bars/days
suppressed_raw_setups
shadow_setup_count
probation_periods
resume_count
health_transition_log
full_capital_period_metrics
active_strategy_period_metrics
```

报告一致性检查：若连续 365 天无交易但存在 raw setups，且 status 仍为 ACTIVE，则报告生成必须失败或给出高优先级诊断，不能静默标记 completed。

### SR1 退出门槛

- 同一 breaker action 关闭任意数量币种，只形成一个 health cohort；
- COOLDOWN 禁止新仓，但仍管理已有仓位；
- 冷静期跨重启后到期时间不漂移；
- 到期只进入 PROBATION；
- PROBATION 风险乘数在 risk reservation、订单和报告中一致；
- 两次失败进入 MANUAL_LOCK 的行为可配置且可审计；
- 2021 历史回放不再出现 `inactive_bars=0`；
- REG-01～04 全部保持通过。

## 8. SR2：保护性止损和实际风险核验

### SR2-1 ATR 指标与无前视契约（P1）

所有止损参数只允许使用信号时已完成 bar 的 ATR、Donchian 和 high。禁止使用未来 bar 或入场后才知道的数据参与原始仓位排序。

### SR2-2 混合初始止损（P1）

研究比较：

```text
A: 原 Donchian stop
B: 纯 ATR initial stop
C: max(Donchian stop, entry - k*ATR)
```

需要预注册最小/最大 ATR 距离；过近信号拒绝，过远信号缩仓或拒绝，不允许使用隐式 5% fallback 掩盖异常。

### SR2-3 Chandelier 追踪止损（P1）

context 和持久状态记录 `highest_high_since_fill`、`initial_stop`、`trailing_stop`、`effective_stop` 和 stop order identity。属性测试必须证明多头 stop 单调不下降。

### SR2-4 成交后风险重核（P0）

EntryFill 后以真实成交价和最新可用权益重新计算实际风险。若超过 risk reservation 容差，执行具名 `GapRiskResize` 或拒绝剩余量。禁止出现配置 2% 而账本实际初始风险显著大于 2% 且无审计原因。

### SR2-5 OCO、部分成交与重启恢复（P0）

保护单、策略退出和 AccountRisk 强平共享权威 close lifecycle。止损成交后不得重复卖出；外部强平后不得遗留可触发的 stop；重启后必须对账并重建缺失保护。

### SR2 退出门槛

- 初始 stop 公式有手算 fixture；
- Chandelier stop 在任意 ATR 路径下只上移；
- 同一 bar 同时触发多种退出时结果确定且保守；
- actual risk 不超过 reservation + 明确容差；
- 保护单部分成交、拒单、cancel-replace、unknown 和重启恢复测试通过；
- 回测、replay、sandbox 对相同事件流生成相同 stop intents；
- 所有 close 仍满足账户和 lot 对账恒等式。

## 9. SR3：组合风险、候选排序与账户成本

### SR3-1 经济含义明确的候选 score（P1）

候选评分至少研究以下只使用当时信息的组成：突破幅度/ATR、ADX 强度、OBV 确认、流动性、与现有组合的边际相关性、当前 cluster exposure。所有 score=0 时必须输出诊断，不能无提示退化为字母排序。

### SR3-2 相关性簇和组合风险预算（P1）

增加：

```text
max_crypto_beta_exposure
max_cluster_exposure
max_same_session_entry_risk
max_correlated_stop_risk
portfolio_expected_shortfall_stress
```

压力情景至少包括 2020-03、2021-05、2021-09 以及数据中最大共同下跌日。单笔 2% 风险不得被解释为独立可相加风险。

### SR3-3 解耦 Alpha health 与 AccountRisk（P0）

AccountRisk exit 继续归属于开仓策略的 PnL，但健康统计必须按外部退出 cohort 单独标记。研究报告同时展示：自有退出 Alpha、AccountRisk 增量和组合整体表现。

### SR3-4 统一账户模式（P1）

二选一并禁止混合：

1. 真正 spot-margin：计入报价币借款、借币可用量、历史/保守借款利率和 spot-margin 手续费；
2. perpetual：使用 perpetual 撮合、maintenance margin、历史 funding 和 futures fee。

当前“spot-margin collateral 语义 + futures fee 注释 + 多头融资为零”不得用于重新准入。

### SR3 退出门槛

- 候选顺序不依赖输入列表，也不退化为未披露字母优先；
- 相同高相关冲击下，组合损失压力不超过预注册预算；
- Alpha-only、Risk-overlay-only、Combined 三种归因对账到总 PnL；
- 融资、手续费、滑点、spread、impact 与账户模式一致；
- 成本增加时固定订单集合的净 PnL 不增加。

## 10. SR4：数据和研究基础

### SR4-1 Point-in-time universe（P1）

建立历史上市、退市和可交易区间文件；保留退市前历史，退市后停止新仓并定义现有仓位处置。选币规则必须仅使用当时可得流动性和上市年龄。

### SR4-2 数据来源和第二源核验（P1）

manifest 必须记录 exchange、market type、下载时间、时区和原始标识。Top 20 winners/losers 及所有异常 bar 成交必须用独立数据源或交易所原始数据复核；未通过时不得进入最终 holdout 结论。

### SR4-3 异常数据政策（P1）

区分真实极端行情与数据错误。禁止为了改善收益静默删除异常点；所有 exclude/correct 必须有原始值、修正值、原因和数据源。对无法确认的 bar 运行 include/exclude 双情景并报告敏感性。

### SR4-4 独立事件统计（P1）

PF、bootstrap、置信区间和 concentration stress 使用 exit cohort、趋势 episode 或 block bootstrap，不能假设同日多币种交易相互独立。

### SR4 退出门槛

- manifest 显示 `survivorship_bias_controlled=true`；
- exchange 和 account market type 非空且一致；
- 头部交易第二源核验不是 `unverified`；
- 异常 fill 全部具有审核状态；
- trade-level 与 cohort-level 两套统计同时输出；
- 研究结果能够在冻结数据上重复。

## 11. SR5：样本外重新准入

### 11.1 数据分区

使用时间顺序 train/validation/final holdout；事件标签的持有区间必须通过 purge/embargo 防止相邻泄漏。币种上市时间不同，分区需要同时保存 timestamp 和 point-in-time universe 快照。

### 11.2 必做消融矩阵

```text
E0  Buy-and-hold / cash /简单 20 日突破基准
E1  Trend regime + Donchian 20/10
E2  E1 + OBV
E3  E2 + ATR initial stop
E4  E3 + Chandelier trailing stop
E5  E4 + breakeven（可选）
E6  E4 + health lifecycle
E7  E6 + correlation allocation
E8  E7 + DailyLoss/Drawdown overlay
```

每个增量必须在多数 OOS window 中贡献正向、稳定、成本后的边际价值；不满足则删除或退回研究，不因单次总收益更高而保留。

### 11.3 稳健性矩阵

至少覆盖：

- 两个交易所或独立价格源；
- 1d 与 4h；
- 牛市、熊市、震荡和高波动制度；
- 主要参数邻域而非单一最优点；
- 1.0×、1.5×、2.0×、3.0×成本；
- 头部 5/10 cohort 移除；
- universe 与异常数据政策敏感性；
- 组合相关性和共同跳空压力。

### 11.4 最终准入门槛

具体数值在打开 holdout 前冻结，最低要求：

| Gate | 要求 |
| --- | --- |
| G-S1 生命周期 | 无静默 inactive；health/stop 状态可重建 |
| G-S2 正 OOS edge | 策略和风险调整后超额收益均为正 |
| G-S3 PF 显著性 | cohort-level PF 超过门槛且置信区间下界 > 1 |
| G-S4 回撤 | 不超过预注册最大回撤，且恢复期可接受 |
| G-S5 不集中 | 移除头部 5/10 cohort 后净收益仍为正 |
| G-S6 成本压力 | 1.5×必过，2×结果不发生灾难性翻转 |
| G-S7 参数平台 | 最优点相邻参数多数通过，不是尖峰 |
| G-S8 跨市场 | 至少两个数据源/周期和多个制度保持方向一致 |
| G-S9 执行风险 | actual risk、stop lifecycle、融资成本全部可对账 |
| G-S10 多重检验 | deflated Sharpe/注册 trial 数达到预注册门槛 |

任何核心 Gate 失败，结论必须是 `reject` 或 `continue_research`，不得通过修改同一 final holdout 参数后再次“准入”。

## 12. SR6：Shadow、Sandbox、Paper 与灰度

### SR6-1 Shadow

至少覆盖一个完整冷静期和恢复周期。比较 raw setups、shadow fills、保护单轨迹和 health transitions；shadow 不产生真实订单。

### SR6-2 Sandbox

故障注入：stop cancel-replace 超时、部分成交、unknown order、断网、重启、状态库损坏、交易所已有孤儿 stop、DailyLoss 与 stop 同时触发。

### SR6-3 Paper

连续运行不少于一个有代表性的市场窗口；每日报告 signal、risk reservation、intent、exchange order、fill、position、stop 和 health state 对账。任何未解释差异归为 P0/P1。

### SR6-4 小额灰度

仅在上位 `unified_roadmap.md` 的 R7/R8 条件同时满足后允许：单交易所、少标的、最低订单、禁用提现、人工急停、最大损失预算和可验证回滚。扩大标的、杠杆或资金都视为重新放行。

## 13. 测试计划

### 13.1 健康生命周期

- 同一 breaker action 的 1/5/15 个 close 都只计一个 cohort；
- 重复 CloseEvent 不重复计数；
- 正常策略退出与 AccountRisk 退出可区分；
- COOLDOWN 禁止 entry 但允许 exit；
- UTC 跨日、缺 bar、重启后 cooldown 到期一致；
- PROBATION 风险乘数贯穿 sizing、reservation、fill 和报告；
- 盈利清零 loss count 时不会绕过状态机；
- MANUAL_LOCK 不能自动恢复；
- 每次迁移有事件、告警和持久状态；
- active/inactive 指标窗口与状态时间线一致。

### 13.2 止损和订单

- ATR/Donchian 手算 fixture；
- 无前视和输入不变性；
- Chandelier stop 单调性 property test；
- 跳空后实际风险缩量；
- 同 bar entry/stop 路径使用保守规则；
- OCO 不重复平仓；
- 部分成交后 stop qty 正确；
- stop rejection/unknown/cancel timeout fail closed；
- 重启后缺失 stop 自动恢复，多余 stop 安全处理；
- AccountRisk 强平后无遗留 stop。

### 13.3 组合、成本和研究

- score 相等时明确记录 tie-break，不把字母序伪装成 Alpha；
- 高相关组合压力和 cluster cap；
- spot-margin quote borrow 或 perpetual funding 手算；
- PIT universe 上市/退市边界；
- block bootstrap 保留时间相关性；
- train/validation/holdout、purge、embargo 无重叠；
- 成本单调性、归因加总和会计恒等式。

## 14. 交付物

每个阶段必须产生代码、测试、机器可读证据和人读摘要：

```text
docs/strategy_health_contract.md
docs/protective_stop_contract.md
docs/research/current_strategy_protocol.json
docs/research/current_strategy_experiment_registry.jsonl
docs/research/current_strategy_parameter_stability.json
docs/research/current_strategy_factor_ablation.json
docs/research/current_strategy_cross_market.json
docs/research/current_strategy_final_holdout.json
reports/.../strategy_health_timeline.csv
reports/.../stop_order_audit.csv
reports/.../cohort_trades.csv
reports/.../suppressed_setups.csv
reports/.../risk_budget_reconciliation.csv
```

命名可以按现有目录规范调整，但信息不得只存在于日志文本或人工说明中。

## 15. 建议执行批次

| 批次 | 范围 | 可并行项 | 完成标志 |
| --- | --- | --- | --- |
| B0 | SR0 + SR1-1/2 | 基线冻结、状态 schema、cohort 设计 | 能重放出 2021-09-22 正确迁移 |
| B1 | SR1-3/4 | 生命周期、报告、告警 | 冷静/观察/恢复全链路通过 |
| B2 | SR2-1/2/3 | ATR、初始 stop、Chandelier shadow | 无前视、stop 单调、手算一致 |
| B3 | SR2-4/5 | fill 风险重核、OCO、恢复 | 保护单和账本故障测试通过 |
| B4 | SR3 | 排名、组合簇、账户成本 | 风险和成本语义统一 |
| B5 | SR4 | PIT universe、第二源、cohort 统计 | 数据审计门槛通过 |
| B6 | SR5 | 消融、稳定性、最终 holdout | admit/reject 有冻结证据 |
| B7 | SR6 | shadow、sandbox、paper | 无未解释 P0/P1 差异 |

B0/B1 必须先完成；B2 的纯指标和 shadow 计算可与 B1 后半段并行；真实保护单生命周期依赖稳定的 health、order 和 state-store 契约。B6 不得在 B0–B5 未完成时提前打开最终 holdout。

## 16. 完成定义

本 Roadmap 只有在以下条件全部满足时才算完成：

1. 策略不会因跨币种相关平仓被错误永久杀死；
2. 冷静、观察、恢复和人工锁定都有持久状态、事件、告警和报告；
3. 已有仓位在任何健康/组合状态下都有有效退出管理；
4. 初始和追踪止损使用无前视信息，保护单可对账、可恢复、不重复平仓；
5. 实际 fill 风险与预留风险一致，超限有具名动作；
6. 组合风险识别相关币种不是独立仓位；
7. 候选分配不再无提示依赖字母顺序；
8. 账户模式、手续费和融资成本一致；
9. point-in-time universe、第二数据源和异常审核完成；
10. 当前版本通过预注册的样本外门槛，或被诚实标记为 reject；
11. shadow/sandbox/paper 无未解释 P0/P1 差异；
12. 上位 R7/R8 放行条件同时满足后，才允许最小资金灰度。

收益提高不是完成条件；如果最终证据证明策略无效，但系统能可靠、可重复地得出并执行 `reject`，本 Roadmap 的工程和研究目标仍然达成。

---

## 17. 实施状态（2026-09-01）

> 本节记录 roadmap 各条目的**代码落地状态**，与 §16 的完成定义配合阅读。
> 契约细节见 [`strategy_health_contract.md`](strategy_health_contract.md)、
> [`protective_stop_contract.md`](protective_stop_contract.md) 与
> [`portfolio_risk_contract.md`](portfolio_risk_contract.md)。

### 17.1 已完成（含测试）

| 条目 | 状态 | 实现 / 证据 |
| --- | --- | --- |
| SR0-2 治理状态 | 完成 | `config/params.yaml` 改为 `TrendBreakout: paused_revalidation`；`core/strategy_governance.py` + `run_live.py --live` 拒绝路由未准入策略 |
| SR0-3 实验注册边界 | 完成 | `docs/research/current_strategy_protocol.json`、`docs/research/current_strategy_experiment_registry.jsonl`（14 个参数族已登记，holdout 标记为不可用于排序） |
| STR-P0-01 永久死亡开关 | 完成 | `core/strategy_health.py` 状态机；COOLDOWN 必有到期时间，唯一终态 MANUAL_LOCK 需人工恢复 |
| STR-P0-02 逐币种健康计数 | 完成 | 退出 cohort 聚合；`risk_action_id` 由 `force_liquidate → Order → CloseEvent` 透传，一个熔断动作只产生一个 observation |
| STR-P0-03 停机未进报告 | 完成 | `report.txt` 新增 Strategy Health Lifecycle / Strategy Activity Consistency 分节；`strategy_health.json`、`strategy_health_timeline.csv`、`cohort_trades.csv`、`suppressed_setups.csv`；lifecycle 新增 `strategy_health_status`/`disabled_or_cooldown_at`/`health_gated_days`/`suppressed_raw_setups`/`shadow_setup_count` |
| STR-P0-04 健康与外部风险耦合 | 完成 | `counted_controllers` 默认 `[strategy, router]`：account_risk cohort 记录但不触发迁移 |
| STR-P2-06/07 报告口径 | 完成 | 零交易间隔 + raw setup 存在时产生 P0 诊断；纯粹无信号的安静市场不会误报 |
| SR1-4 告警 | 完成 | `live_trading/tick_orchestrator.py` 每条迁移发一次 `strategy_health_transition`（manual_lock 为 critical）；`live_trading/state_export.py` 导出 `strategy_health` 并纳入 critical-state signature |
| SR2-1 无前视 | 完成 | 止损只用已完成 bar；`TestNoLookahead` 用"改写未来 bar 结果不变"固定 |
| SR2-2 混合初始止损 | 完成（默认 arm A） | `plan_initial_stop`；隐式 5% fallback 已删除，改为显式拒绝/夹取；ATR 腿默认关闭待 A/B/C 研究 |
| SR2-3 Chandelier | 完成（默认关闭） | `update_trailing_stop`；单调性 property test；可选保本含成本缓冲 |
| SR2-4 成交后风险重核 | 完成（回测 + 实盘） | 共用 `evaluate_fill_risk`；回测由 `BacktestEngine._recheck_entry_risk` 核验并写 `risk_budget_reconciliation.csv`；实盘从持久订单账本扫描 opening fills，按累计部分成交幂等核验，超限只提交增量 `GapRiskResize`，checkpoint 跨重启恢复，拒单 critical 告警并停止该 tick 新策略工作；审计经 `live_status.json.fill_risk_audit` 导出 |
| SR3-1 候选 score | 完成 | `core/candidate_scoring.py`：突破幅度/ATR、ADX、OBV、流动性四项无量纲分量；全零批次记 `degenerate_ranking_batches` 并输出 WARNING + `ordering=tie_break_alphabetical`，字母序不再伪装成排名 |
| SR3-2 相关性簇与组合预算 | 完成 | `core/portfolio_risk.py`：名义类预算（cluster / crypto beta）进 `_entry_notional_caps`（clamp 与 gate 同一口径），风险类预算（同 session 入场风险 / 簇内未平仓止损风险）由 `PortfolioRiskGovernor` 在分配时缩量或拒绝；未映射币种默认相关 |
| SR3-3 Alpha 与 AccountRisk 解耦归因 | 完成 | `calculate_attribution` 新增 `by_exit_controller` 与 `control_attribution`（alpha_only / risk_overlay / router_and_system / combined），三者恒等于总 PnL，不对账即报 P0；控制器口径与健康 cohort 共用 |
| SR3-4 账户模式统一 | 完成 | `core/account_cost_contract.py` 在回测与实盘入口校验 mode/fee_schedule/融资三元组；实盘把 CLI `market-type` 规范化后与配置模式强制比对，未指定时从配置推导匹配默认值；费率由 futures 0.05%/0.02% 改为 spot-margin 0.10%/0.10%；`_accrue_quote_borrow` 计提 `max(0, long_notional - equity)` 的报价币借款利息 |
| STR-P1-07 运行范围不一致 | 完成 | `TrendBreakout.allowed_states` 收窄为 `{TREND_UP}`，与生产 routing 一致；新增测试要求 declared == routed |
| SR2-5 保护单生命周期 | 完成（实盘 + 回测） | `core/protective_orders.py` 状态机 + `live_trading/tick_orchestrator.py` 每 tick 对账：入场成交前不假设有止损、数量等于净持仓、只上移、持仓归零取消全部残留、未知状态 fail closed 平仓、重启以交易所为准重建/清理孤儿单 |
| STR-P1-01 回测 intrabar 止损等价性 | 完成 | `backtest/protective_stops.py` 的 `ResidentStopSimulator`：回测持有与实盘同一个 `ProtectiveOrderManager` 产生的常驻止损意图，由历史撮合器在 bar 内成交；预注册保守路径 `open -> 不利极值 -> 有利极值 -> close`，跳空时成交在 `min(open, stop)` 而非走不到的止损价；入场 bar 在自己这根 bar 内即受保护；`force_liquidate` 与 `EndOfBacktest` 先取消常驻止损单，保证唯一权威 close；产出 `stop_order_audit.csv` 与 report.txt 的 Protective Stop Execution 分节 |
| REG-01～04 | 保持通过 | 健康闸门只拦新仓；全量测试 537 passed、1 skipped、46 subtests passed |

测试入口：`tests/test_sr1_strategy_health.py`（19）、`tests/test_sr2_protective_stops.py`（19）、`tests/test_sr2_protective_orders.py`（25）、`tests/test_sr2_backtest_intrabar_stops.py`（15）、`tests/test_sr3_portfolio_risk.py`（27）。

> 注：SR3-4 的费率修正会**降低**所有既有基线的净值（成本更保守），
> `tests/fixtures/backtest/engine/engine_baseline_v1.json` 已随之重新生成。
>
> 注：STR-P1-01 同样会**降低**既有基线——旧口径在 bar 收盘才发现穿越、到下一根
> bar 开盘才退出，等于假设仓位活过了跳空日；基线里那笔 ETH 交易由 03-21 开盘
> 3196.69 改为 03-20 跳空开盘 3119.23 成交。基线 fixture 已再次重新生成，
> 所有 STR-P1-01 之前的报告都不可与之后的报告直接比较。

### 17.2 未完成（按优先级）

| 条目 | 阻塞原因 |
| --- | --- |
| SR0-1 基线三次可重复冻结 | 需要 30 标的原始数据；本次只登记了预期诊断（`current_strategy_protocol.json`），未实际重跑并比对三次。**注意：STR-P1-01 已改变止损成交口径，冻结必须在新口径下重跑。** |
| SR3-2 压力情景 | `portfolio_expected_shortfall_stress` 与 2020-03 / 2021-05 / 2021-09 情景需要真实历史数据集，随 SR4 一起做 |
| SR3 簇定义 | 目前是配置里的静态映射，尚未由历史相关矩阵估计 |
| SR4 全部 | 需要 point-in-time universe 文件、第二数据源与异常审核，属数据工程与外部数据获取 |
| SR5 全部 | 依赖 SR3/SR4；final holdout 必须保持关闭 |
| SR6 全部 | 依赖 SR5；shadow/sandbox/paper 需要真实运行时间 |

因此 §16 的完成定义中，第 1～8 条已满足：第 4 条现在在实盘与回测两侧都成立
（保护单可对账、可恢复、不重复平仓，且回测按同一常驻止损意图在 bar 内成交）。
第 9～12 条仍未满足，且全部依赖真实历史数据与外部数据源（SR0-1 重跑冻结、
SR3-2 压力情景、SR4 数据基础、SR5 holdout、SR6 运行时间），不是代码缺口。
`TrendBreakout` 维持 `paused_revalidation`，不得按"已验证生产 Alpha"扩大真实资金。
