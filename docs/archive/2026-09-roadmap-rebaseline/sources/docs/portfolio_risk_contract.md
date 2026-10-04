# 组合风险、候选排序与账户成本契约（Portfolio Risk Contract）

> 文档状态：Active v1.0
> 生效日期：2026-09-01
> 实现：[`core/candidate_scoring.py`](../core/candidate_scoring.py)、[`core/risk/portfolio_governor.py`](../core/risk/portfolio_governor.py)、[`core/account_cost_contract.py`](../core/account_cost_contract.py)、[`core/allocation.py`](../core/allocation.py)
> 测试：[`tests/test_sr3_portfolio_risk.py`](../tests/test_sr3_portfolio_risk.py)
> 上位文档：[`current_strategy_remediation_roadmap.md`](current_strategy_remediation_roadmap.md) SR3

## 1. 候选排序（SR3-1 / STR-P1-03）

旧实现里每个 `EntryCandidate` 的 `score` 都是 0，排序键退化为
`(strategy_name, symbol)`，也就是**按字母顺序分配资金**——审计轨迹上看起来像
排名，实际是一条从未公开的分配规则。

现在的评分只使用信号时点的事实，且每一项都是无量纲比值，因此可以跨标的比较：

| 分量 | 含义 |
| --- | --- |
| `breakout_extent` | 突破幅度 / ATR。0.1 ATR 的试探和 2 ATR 的推进不是同一个信号 |
| `trend_strength` | ADX 超过阈值的比例 |
| `volume_confirmation` | entry_window 内 OBV 累积，按自身近期尺度归一 |
| `liquidity` | 对数缩放的成交额：填不进去的信号价值更低 |

权重在 `config/params.yaml` 的 `candidate_scoring` 段，由
`composition.factory` 注入（策略不读 config）。

与组合相关的项（边际相关性、当前 cluster 敞口）**不在**评分里：它们取决于
当前持仓而不是信号本身，由 §2 的预算在分配时统一执行——那里才是唯一能同时
看到整批同时间戳候选的位置。

**退化排序必须显式暴露**：当一批候选（≥2 个）分数完全相同时，
`PortfolioSignalAllocator` 会

- 递增 `degenerate_ranking_batches`；
- 打一条 WARNING 说明本批次实际由确定性名称 tie-break 决定；
- 在 `allocation_audit.csv` 里把该批次标记为 `ordering=tie_break_alphabetical`。

字母序仍然可能发生（它保证确定性），但**不会再伪装成 Alpha 排名**。

## 2. 相关性簇与组合风险预算（SR3-2 / STR-P1-04）

单币 30% + 总杠杆 3 倍在纸面上是分散的；2021-05 那种行情里，十五个主流币是
一个有十五个代码的仓位。每笔 2% 只有在**互相独立**时才可以相加。

新增四个预算：

| 预算 | 含义 | 执行位置 |
| --- | --- | --- |
| `max_cluster_exposure_pct` | 单个相关簇的 gross 名义 / 权益 | `RiskManager._entry_notional_caps` |
| `max_crypto_beta_exposure` | 所有 crypto 相关持仓 gross / 权益 | `RiskManager._entry_notional_caps` |
| `max_same_session_entry_risk` | 单个 session 新开仓的初始风险 / 权益 | `PortfolioRiskGovernor`（分配时） |
| `max_correlated_stop_risk` | 单个簇内未平仓初始风险 / 权益 | `PortfolioRiskGovernor`（分配时） |

设计要点：

1. **名义类预算放在 `_entry_notional_caps`**——这是 `clamp_entry_qty`（削减）
   与 `check_entry_risk`（闸门）共同读取的唯一口径，因此削减和拒绝不可能漂移。
2. **风险类预算放在分配器**——只有那里能同时看到整批同时间戳候选。
   `PortfolioRiskGovernor` 按 session 累计已批准的初始风险：一个 session 的
   入场共享一份预算，而不是各自独立主张完整的每笔风险。
3. **超预算优先缩量而不是丢弃**：`scale = headroom / planned_risk`；只有
   headroom ≤ 0 时才拒绝。
4. **未映射的币种默认属于 `crypto_beta`**：未知币被假定为相关，永远不被假定为独立。

`correlated_risk_audit.csv` 记录每个候选的簇、计划风险、允许风险、缩放比例与
binding 约束名。

## 3. Alpha 与风险叠加层的归因（SR3-3 / STR-P1-08）

冻结基线里 76.6% 的净利润来自 `DailyLossLimit` 退出，因此总体 PF 不能被读作
20/10 Donchian Alpha 的证据。`calculate_attribution` 现在按**实际执行平仓的
控制器**再切一次同一批交易：

```text
alpha_only        策略自身退出（signal / hard_stop / Donchian 出场）
risk_overlay      AccountRisk（DailyLossLimit / AccountLiquidation / ...）
router_and_system Router 时间/regime 退出与 EndOfBacktest
combined          合计
```

这是一个划分（partition），因此三者恒等于 `total_net_pnl`；`reconciles=false`
会在报告里打出 P0。控制器判定复用
[`core.strategy_health.classify_exit_controller`](../core/strategy_health.py)，
与健康 cohort 的口径是同一个，不存在两套定义。

## 4. 账户模式与成本语义（SR3-4 / STR-P1-05、STR-P1-06）

### 4.1 报价币借款

spot-margin 账户用**报价币借款**支撑超过自有权益的多头，这笔借款有利息。
旧实现只对空头计提币借款利息，于是一段 gross/equity 峰值 1.456 的运行
在多头一侧的 financing ledger 是空的。

```text
borrowed_quote = max(0, long_notional - equity)
```

自有资金先支撑前 `equity` 的多头敞口，超出部分是保证金负债；gross/equity ≤ 1
时不计提，计提金额随当时的真实杠杆逐 bar 变化。账本 kind 为 `quote_borrow`，
symbol 为 `__QUOTE__`。

### 4.2 费率必须与账户模式一致

`execution.fee_schedule` 现在必须写明 venue / market_type / source，
`core.account_cost_contract.validate_account_cost_contract` 在**回测和实盘两个
入口**都会校验：

- fee_schedule 的 market_type 必须与 `account.mode` 兼容；
- `spot_margin` 必须有正的 `default_borrow_rate_annual`（杠杆多头不是免费的）；
- `perpetual` 必须 `funding_rate_required: true`（缺失的历史 funding 必须失败，
  不能被静默当作 0）。

实盘额外通过 `validate_runtime_account_cost_contract` 校验真正传给 broker 的
`--market-type`：`margin → spot_margin`，`swap/future/futures → perpetual`。
运行时模式必须与 `config.account.mode` 相同；CLI 未显式指定时，会从配置模式推导
匹配的安全默认值，不能再出现「校验 spot-margin、实际启动 spot」的分叉。

因此配置里的手续费已从 futures 费率（0.05%/0.02%）改为 Binance
spot-margin 费率（0.10%/0.10%）。这是**更保守**的方向：所有既有基线的净值都会
下降，`tests/fixtures/backtest/engine/engine_baseline_v1.json` 已随之重新生成。
校验结果写入 `account_cost_contract.json`，并进入引擎结果供 manifest 审计。

## 5. 产物

| 产物 | 内容 |
| --- | --- |
| `allocation_audit.csv` | 每个候选的 score、rank、ordering、是否被接受、相关风险预算结果 |
| `correlated_risk_audit.csv` | 每次相关风险预算判定（簇、计划/允许风险、缩放、binding 约束） |
| `account_cost_contract.json` | 已校验的账户模式/费率/融资三元组 + `degenerate_ranking_batches` |
| `financing_ledger.csv` | 新增 `quote_borrow` 条目 |
| `report.txt` → Control Attribution | alpha_only / risk_overlay / router_and_system / combined |

## 6. 未纳入本契约的部分

- `portfolio_expected_shortfall_stress`（SR3-2 的压力情景清单：2020-03、
  2021-05、2021-09 与数据中最大共同下跌日）需要真实历史数据集，属于 SR4；
- 边际相关性作为评分分量（当前只做预算约束，未进评分）；
- 簇定义目前是配置里的静态映射，尚未由历史相关矩阵估计。
