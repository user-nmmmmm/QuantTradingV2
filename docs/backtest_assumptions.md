# 回测假设与逻辑

本文档概述了 QuantTrading 回测引擎的核心假设、执行逻辑及局限性。

## 1. 执行逻辑 (无前视偏差)

系统严格遵循 **Next-Bar Execution (次K线执行)** 模型以防止前视偏差 (Look-Ahead Bias)，同时也支持日内限价/止损单。

- **信号生成 ($t$)**: 策略仅分析 $t$ 时刻收盘及之前的数据。
- **订单提交 ($t$)**: $t$ 时刻产生的订单会在该 K 线结束时提交至券商队列。
- **订单处理 ($t+1$)**:
  - **市价单 (Market Orders)**: 在 $t+1$ 时刻的 **开盘价 (Open Price)** 成交。
  - **限价单 (Limit Orders)**:
    - 买入: 若 $Low_{t+1} \le Limit$ 则成交。
      - 如果 $Open_{t+1} \le Limit$ (低开穿价): 按 $Open_{t+1}$ 成交 (Taker)。
      - 否则: 按 $Limit$ 成交 (Maker)。
    - 卖出: 若 $High_{t+1} \ge Limit$ 则成交。
      - 如果 $Open_{t+1} \ge Limit$ (高开穿价): 按 $Open_{t+1}$ 成交 (Taker)。
      - 否则: 按 $Limit$ 成交 (Maker)。
  - **止损单 (Stop Orders)**:
    - 买入: 若 $High_{t+1} \ge Stop$ 则触发。按 $\max(Open_{t+1}, Stop)$ 成交 (Taker)。
    - 卖出: 若 $Low_{t+1} \le Stop$ 则触发。按 $\min(Open_{t+1}, Stop)$ 成交 (Taker)。

### 开仓单存活期（TTL）

`execution.opening_order_ttl_bars`（默认 10，0 表示关闭）：**开仓**单（buy/short）连续这么多根 bar 一笔都没成交就置为 `EXPIRED`。

理由不是"挂太久不合理"，而是两处会永久卡死的状态：未成交的开仓单一直持有它的风险预留（`core/risk/reservation.py` 只在终态释放，而工作中的订单没有终态），并且 `has_active_open_order` 会一直阻止该标的再次入场——一张永远触不到价的限价单等于把这个标的从整轮回测里永久摘除，同时还在占用组合风险额度。

三条边界：
- **任何成交都会重置计数**——被参与率限速拆到多根 bar 的大单是在推进而不是在空转，不会被误杀；
- **平仓单豁免**：过期会留下无人管理的持仓；
- **常驻保护性止损豁免**：它本来就该挂到持仓关闭为止。后两者都是 sell/cover，按定义不属于开仓单，所以是结构性豁免而非特判。

## 2. 费率与佣金

回测支持自定义费率结构 (在 `params.yaml` 中配置)。

- **佣金模式**: 双边收费 (开仓和平仓均收费)。
- **费率类型**:
  - **Maker Fee (挂单)**: 适用于日内被动成交的限价单。默认: **0.02% (2 bps)**。
  - **Taker Fee (吃单)**: 适用于市价单及立即成交的限价单。默认: **0.05% (5 bps)**。
- **计算公式**: $Cost = Price \times Qty \times FeeRate$

## 3. 滑点与流动性

- **固定滑点 (Fixed Slippage)**: 在成交价基础上增加/减少固定百分比。
  - 买入: $P_{fill} = P_{open} \times (1 + slip)$
  - 卖出: $P_{fill} = P_{open} \times (1 - slip)$
- **买卖价差**：使用 bar 的 `spread_bps`；缺失时使用配置默认值，并按半个价差计入单边成交。
- **波动率滑点**：`volatility_slippage_factor × (high-low)/reference_price`；若 bar 提供
  `volatility` 列则优先使用该列。
- **随机滑点 (Random Slippage)** (可选): 在 $[0, MaxSlip]$ 范围内均匀分布，模拟真实波动。
- **非线性市场冲击**：$impact = coefficient × participation^{exponent}$，默认指数 1.5。
- **分批成交**：所有订单共享每个 symbol/bar 的成交量预算；超过
  `max_participation_rate` 的剩余数量进入下一根 K 线，FOK/IOC 按各自语义取消。
  预算按 **bar** 计而不是按撮合调用计——引擎每根 bar 撮合两遍（普通订单簿 +
  常驻止损），两遍共用同一份额度，否则实际参与率会是配置值的倍数。
- 每笔成交记录 `spread_slippage_rate`、`volatility_slippage_rate`、
  `impact_slippage_rate` 和 `participation_rate`，拒单/分批原因写入 `execution_audit`。

## 4. 账户、资金费率与杠杆

`account.mode` 明确选择且只选择一种账户语义：

- `spot`：买入交换现金与现货库存，禁止做空；可用现金约束开仓。
- `spot_margin`：现金表示抵押品，持仓名义金额不直接改变现金；做空受
  `borrow_available_qty`（或审计标记的配置回退值）限制，按 `borrow_rate_annual`
  与实际持有时间计提借币费。
- `perpetual`：现金表示合约抵押品；按历史 `funding_rate` 和配置结算周期计提资金费。
  正资金费时多头支付、空头收取；`funding_rate_required=true` 时缺失历史费率会失败关闭。

保证金账户每根 K 线保存：标记权益、总名义敞口、初始保证金、维持保证金、可用保证金、
保证金率和强平状态。新开仓必须通过初始保证金校验；标记权益不高于维持保证金时，
`Broker.force_liquidate` 按标记价格并叠加强平惩罚，通过统一成交/批次/CloseEvent 路径清算。
资金费和借币费分别写入 `financing_ledger`，并计入权益与会计恒等式。

组合风控另以历史权益高水位计算永久性回撤，依次执行降仓、停止开仓、强平与锁定。
这些状态不会随自然日自动恢复；只有带审批人的 `RiskManager.manual_resume()` 可以恢复。
`drawdown.daily_loss_limit` 是独立的日内限制，仅该日内状态会跨日复位。

### 4.1 仓位削减（Clamp）语义

风险定仓的名义金额满足：

```
notional / equity = risk_per_trade ÷ (止损距离 / 价格)
```

止损越紧，仓位越大。因此在 `risk_per_trade=0.02`、`max_pos_size_pct=0.30` 下，
**止损距离小于价格 6.67%（=0.02/0.30）的信号，其仓位必然超过集中度上限**。

- **当前行为**：`RiskManager.clamp_entry_qty` 会把仓位**削减到**现金/杠杆/集中度
  三项上限中最紧的那一项，再提交订单；`check_entry_risk` 作为最后一道闸门。
- **历史行为（已修正）**：超限直接整单拒绝。由于加密日线 ATR 中位数约为价格的 4.4%，
  使用 1×ATR 止损的策略（如 `RangeMeanReversion`）100% 的信号都被拒绝，
  该策略在回测中从未成交过——表现为"策略无信号"，实为被风控静默封杀。
- **削减后风险只会更小**（实际风险敞口低于 `risk_per_trade` 目标），
  因此削减不会放大风险，但会使实际风险预算低于名义设定值。
- **尘埃过滤**：削减后名义金额低于 `min_entry_notional_pct`（默认权益的 1%）时放弃该笔交易，
  避免额度将尽时成交出只付手续费的极小仓位。

> 口径变更影响：该修复会显著改变回测结果。在 2017-08~2026-06 六标的样本上，
> `RangeMeanReversion` 从 0 笔成交变为 83 笔，总收益率从 -60.3% 变为 -71.0%——
> 变差不代表修复错误，而是此前该策略的负 alpha 被风控掩盖、未能体现在结果中。

## 4.2 市场状态判定（四状态互斥）

状态机 (`core/state.py`) 输出四个**互斥**状态，路由表按状态分派策略：

| 状态 | 条件 | 路由策略 |
|---|---|---|
| `TREND_UP` | ADX > 阈值 且 close > MA_fast > MA_slow | TrendBreakout |
| `TREND_DOWN` | ADX > 阈值 且 close < MA_fast < MA_slow | TrendBreakdown |
| `VOLATILE` | ADX > 阈值 且 ATR% > 阈值，**且均线结构未成方向** | VolatilityReversion |
| `SIDEWAYS` | 其余 | RangeMeanReversion |

- **`VOLATILE` 的语义是"动得凶但没方向"**（转折/来回扫），不是"强趋势/突破"——
  干净的突破会被判为 `TREND_UP`/`TREND_DOWN`。这与路由表把它分派给均值回归策略一致。
- **必须排除已成方向的 bar**：三者共用同一个 ADX 门槛且 `VOLATILE` 最后赋值，
  若不排除则会无条件覆盖趋势状态。加密日线 ATR 中位数约为价格的 4.4%，
  远高于 `atr_pct_threshold`（2.5%），实测 BTC 2017-2026 上 96.3% 的 `TREND_UP`
  与 99.8% 的 `TREND_DOWN` 因此被吞掉，趋势策略在整段回测中从未被路由到。
- **`stability_period`**：设为 1 表示不做去抖，每次原始状态翻转都立即切换。
  由于相邻状态分派给不同策略，每次切换都会触发 `StateSwitch` 强制平仓 + 路由冷却，
  调大该值可减少这类摩擦。

> 口径变更影响：修复互斥后，BTC 状态分布由
> `VOLATILE 55.7% / SIDEWAYS 43.4% / TREND_UP 0.83% / TREND_DOWN 0.03%`
> 变为 `SIDEWAYS 43.2% / TREND_UP 22.8% / TREND_DOWN 18.0% / VOLATILE 16.0%`；
> 六标的样本总收益率由 -71.0% 变为 +74.4%（Profit Factor 0.71 → 1.29）。

### 4.2.1 Regime 切换与策略出场的优先级

当前契约采用 **regime 切换即平仓**：当新状态映射到不同策略时，Router 先取消该标的
所有未完成订单，再提交全量平仓单，并进入路由冷却；不会等待旧策略的 `should_exit`
条件或设置额外超时。平仓成交仍在 Next-Bar Execution 模型下发生，成交归因为
`exit_strategy=Router`、`exit_reason=StateSwitch`。

Router/CircuitBreaker 的外部平仓不会绕过策略生命周期：持仓实际归零后，原开仓策略的
`on_trade_closed` 必须且只会回调一次。因此连续亏损、冷却和熄火闸门使用真实成交结果，
而不是依赖旧策略在切换后再次被路由。趋势策略的 `health_stats` 当前明确采用
`scope=cross_symbol_aggregate`，即总交易数、滚动 PnL 与连续亏损是跨标的组合级计数。
## 4.3 结果可信度诊断（core/diagnostics.py）

`core/metrics.py` 回答"策略表现如何"，`core/diagnostics.py` 回答两个前置问题：
**这个业绩数字能不能信**，以及**系统的实际行为是否与代码描述一致**。
每项指标都对应一个真实存在过、且被现有指标完全掩盖的缺陷：

| 指标 | 暴露的问题 |
|---|---|
| `calculate_pnl_concentration` | 收益是否依赖极少数交易。Top-N 贡献占比、剔除后净盈亏、利润 HHI。**份额>100% 表示剔除后系统净亏损**。 |
| `calculate_exit_attribution` | 谁真正平掉了仓位。按 `exit_reason` 与"开仓策略 vs 平仓方"拆分；自身出场占比低于 10% 的策略会被标进 `inert_exit_logic`——其出场规则与相关参数实际上是死代码。 |
| `calculate_lifecycle_coverage` | 策略是否观测得到自己的平仓事件。依赖平仓回调的风控（熄火闸门、连亏冷却）在覆盖率远低于 1.0 时处于失效状态。 |
| `calculate_calendar_returns` | 按自然年/季而非索引位置切分，用于看"10年里有几年是亏的""某一年是否贡献了大部分利润"。 |
| `calculate_streaks` | 最长连盈/连亏。连亏长度远超冷却阈值即说明该规则从未触发。 |

产出位于 `metrics["Diagnostics"]`，并在 `report.txt` 的
`Result Diagnostics (结果可信度诊断)` 分节渲染，含警告行。
该分节即使在收益为正时也可能给出警告——这正是它的目的。

**前置改动**：闭合交易记录新增 `exit_reason` 与 `exit_strategy` 两个字段
（来自平仓那一笔成交）。此前只保留开仓策略，导致"仓位被 Router 强平"
这一情况在按策略/标的归因中完全不可见。

## 5. 数据质量与处理

- **缺失值**: 执行时跳过缺失 K 线，但指标计算可能受影响 (采用前值填充)。
- **时区**: 所有数据统一标准化为 UTC 时间。
- **对齐**: 多标的回测基于时间戳交集 (Intersection) 进行对齐。

## 6. 基准对比

策略表现将与以下基准进行对比:
- **等权组合 (Equal Weight)**: 等权重持有所有选定标的（当前代码实现）。

## 7. 输出文件结构

每次回测都会在 `reports/` 目录下生成一个独立的时间戳文件夹 (例如 `reports/20260208_...`)，避免文件在根目录堆积。文件夹内包含：

- **report.txt**: 回测配置与核心指标汇总。
- **equity.csv**: 每日账户净值与现金数据。
- **trades.csv**: 详细的交易执行记录 (包含成交时间、价格、滑点、手续费)。
- **benchmark.csv**: 基准策略 (Buy & Hold) 的净值数据。
- **data_quality_report.json**: 输入数据的质量分析报告 (缺失值、异常值统计)。
- **routing_log.csv**: 策略路由的详细决策日志。

## Phase 2: reproducibility, alignment, benchmarks and audit

Every normal CLI backtest is an immutable report bundle. `run_manifest.json`
records Git SHA/branch/dirty state, dependency-lock hashes, the complete config
snapshot and hash, requested/effective periods, data-source identity, per-symbol
data hashes, seed and execution settings. Exact engine inputs are stored under
`data_inputs/`; `--replay-manifest` verifies those hashes and compares the
trade, equity, benchmark and report-payload digests exactly.

Multi-asset alignment is explicit:

- `union` (default): the event timeline contains every real bar from any asset;
  only symbols with a real bar at that timestamp are routed.
- `intersection`: the timeline contains only timestamps present for every asset.

The report always saves two benchmark definitions. The fixed benchmark buys,
at equal weights, only assets observable at the benchmark start and never
rebalances. The dynamic benchmark rebalances equally across assets observable
at each event timestamp; one-way turnover and the configured transaction cost
are saved alongside every weight. `benchmark.csv` is the selected primary
benchmark, while `benchmark_fixed.csv`, `benchmark_dynamic.csv`,
`benchmark_weights.csv`, and `benchmark_turnover_cost.csv` preserve the audit
trail.

Data anomalies are marked in the OHLCV frame rather than silently removed.
Each fill persists the anomaly flags for its execution bar. `event_log.jsonl`
contains signal, risk-decision, order-intent, order, fill and close events;
`routing_log.csv` contains routing decisions. If trading occurred and a required
event family is absent, the CLI returns a failed artifact status. Independent
second-source verification is written to `top_trade_market_data_audit.json`;
without a supplied secondary directory it is explicitly `unverified`.

A point-in-time universe CSV may be supplied with `symbol`, `listed_at`, and
optional `delisted_at`. Bars before listing and at/after delisting are ineligible,
while a delisted asset's earlier history remains in the sample. A static symbol
list is recorded as not controlling survivorship bias.
## 8. 指标统计口径

- **年化因子**：根据权益曲线时间索引的中位正间隔推断 `periods_per_year`；加密日线为 365.25，4 小时、1 小时和 15 分钟周期分别按每日 6、24 和 96 个周期年化。报告同时输出该因子。
- **月收益**：使用连续月末权益序列的 `pct_change`，因此包含月初跨期收益；首个不具备上月基准的月份不参与平均。
- **空值语义**：`0` 仅表示有效计算结果为零；数据不足使用 `null + insufficient`，数学不可定义使用 `null + undefined`，不输出无穷值。
- **Profit Factor**：同时输出闭合交易样本数、亏损交易数、状态及 95% Bootstrap 区间；少于 30 笔闭合交易标记为 `insufficient`。
- **回撤**：同时输出最大回撤峰值、谷底、恢复日期、持续周期/天数、恢复周期/天数、当前回撤、水下比例及是否尚未恢复。
- **输入不可变**：指标计算只读传入的权益曲线，不添加临时列或修改原数据。
