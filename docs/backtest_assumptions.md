# 回测假设与逻辑

本文档概述了 QuantTrading 回测引擎的核心假设、执行逻辑及局限性。当前默认值按
2026-09-20 的 [`config/params.yaml`](../config/params.yaml) 与运行实现核对；标为历史的
数值只用于解释旧缺陷，不构成当前版本的研究结论或策略准入证据。

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

`execution.opening_order_ttl_bars`（默认 10，0 表示关闭）限制**开仓**单（buy/short）的
总可撮合 bar 数：允许前 10 根 bar 撮合，若仍有剩余量，在第 11 根可撮合 bar 撮合前
置为 `EXPIRED`。同一时间戳的重复撮合不会重复计龄。

理由不是"挂太久不合理"，而是两处会永久卡死的状态：未成交的开仓单一直持有它的风险预留（`core/risk/reservation.py` 只在终态释放，而工作中的订单没有终态），并且 `has_active_open_order` 会一直阻止该标的再次入场——一张永远触不到价的限价单等于把这个标的从整轮回测里永久摘除，同时还在占用组合风险额度。

三条边界：
- **部分成交不重置计龄**：超时后只终止订单剩余量，已成交持仓继续受持仓与保护单管理；避免微小成交无限延长风险预留和标的锁；
- **平仓单豁免**：过期会留下无人管理的持仓；
- **常驻保护性止损豁免**：它本来就该挂到持仓关闭为止。后两者都是 sell/cover，按定义不属于开仓单，所以是结构性豁免而非特判。

## 2. 费率与佣金

回测支持自定义费率结构（在 `config/params.yaml` 中配置）。当前费率表身份为
`venue=binance`、`market_type=spot_margin`，必须与 `account.mode` 一致；这是该回测的
配置假设，真实账户费率仍须由账户费用证据确认。

- **佣金模式**: 双边收费 (开仓和平仓均收费)。
- **费率类型**:
  - **Maker Fee (挂单)**: 适用于日内被动成交的限价单。默认: **0.10% (10 bps)**。
  - **Taker Fee (吃单)**: 适用于市价单、立即成交的限价单及触发的止损单。默认: **0.10% (10 bps)**。
- **计算公式**: $Cost = Price \times Qty \times FeeRate$

## 3. 滑点与流动性

- **固定滑点 (Fixed Slippage)**: 在成交价基础上增加/减少固定百分比。
  - 买入: $P_{fill} = P_{open} \times (1 + slip)$
  - 卖出: $P_{fill} = P_{open} \times (1 - slip)$
- **买卖价差**：使用 bar 的 `spread_bps`；缺失时使用配置默认值，并按半个价差计入单边成交。
- **波动率滑点**：`volatility_slippage_factor × (high-low)/reference_price`；若 bar 提供
  `volatility` 列则优先使用该列。
- **随机滑点 (Random Slippage)**（可选，`--random_slip`）：在 $[0, BaseSlip]$ 范围内
  均匀抽样基础滑点，价差、波动率和冲击成本仍按各自规则叠加；默认关闭。
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
LIQUIDATE/LOCKED 不会随自然日自动恢复；必须通过带审批人的 `RiskManager.manual_resume()` 恢复。
启用 `drawdown.recovery` 后，BLOCK_NEW 在 30 天冷静期后可进入 0.25 风险乘数的试运行，
试运行权益再亏 3% 则重新冷静；原高水位不变。回到原高水位 10% 回撤以内后，回归 REDUCE，
而非自动解除全部风控。具体条件、持久化与 A/B 证据见 [`p0_drawdown_recovery.md`](p0_drawdown_recovery.md)。
`drawdown.daily_loss_limit` 是独立的日内限制，仅该日内状态会跨日复位。

### 4.1 仓位削减（Clamp）语义

风险定仓的名义金额满足：

```
notional / equity = risk_per_trade ÷ (止损距离 / 价格)
```

止损越紧，仓位越大。因此在 `risk_per_trade=0.02`、`max_pos_size_pct=0.30` 下，
**止损距离小于价格 6.67%（=0.02/0.30）的信号，其仓位必然超过集中度上限**。

- **当前行为**：`RiskManager.clamp_entry_qty` 会按现金、杠杆、单标的集中度及相关簇敞口等
  适用上限削减仓位；组合分配还检查同批入场风险、相关止损风险与回撤预算，
  `check_entry_risk` 作为提交前的最后一道闸门。
- **历史行为（已修正）**：超限直接整单拒绝。由于加密日线 ATR 中位数约为价格的 4.4%，
  使用 1×ATR 止损的策略（如 `RangeMeanReversion`）100% 的信号都被拒绝，
  该策略在回测中从未成交过——表现为"策略无信号"，实为被风控静默封杀。
- **削减后风险只会更小**（实际风险敞口低于 `risk_per_trade` 目标），
  因此削减不会放大风险，但会使实际风险预算低于名义设定值。
- **尘埃过滤**：基础门槛为权益的 1%；当前 `risk.minimum_entry.scale_with_risk=true`，
  门槛随组合风险乘数和策略健康乘数缩放。研究路径还取配置下限 10 USDT 与缩放门槛的
  较大值；该下限不是历史交易所最小订单证据，真实账户仍须满足交易所约束。

> 历史口径变更示例（旧配置）：该修复会显著改变回测结果。在 2017-08~2026-06 六标的样本上，
> `RangeMeanReversion` 从 0 笔成交变为 83 笔，总收益率从 -60.3% 变为 -71.0%——
> 变差不代表修复错误，而是此前该策略的负 alpha 被风控掩盖、未能体现在结果中。

## 4.2 市场状态判定（四状态互斥）

状态机 (`core/state.py`) 输出四个**互斥**状态，路由表按状态分派策略：

| 状态 | 条件 | 路由策略 |
|---|---|---|
| `TREND_UP` | ADX > 阈值 且 close > MA_fast > MA_slow | TrendBreakout |
| `TREND_DOWN` | ADX > 阈值 且 close < MA_fast < MA_slow | Cash |
| `VOLATILE` | ADX > 阈值 且 ATR% > 阈值，**且均线结构未成方向** | Cash |
| `SIDEWAYS` | 其余 | Cash |

`TrendBreakout` 的治理状态仍为 `paused_revalidation`，上述映射供研究、影子和沙盒使用；
真实资金入口只允许 `admitted` 策略。`Cash` 仅停止新入场，已有仓位仍受退出控制。

- **`VOLATILE` 的语义是"动得凶但没方向"**（转折/来回扫），不是"强趋势/突破"——
  干净的突破会被判为 `TREND_UP`/`TREND_DOWN`。当前 `VOLATILE` 映射到 `Cash`，
  `VolatilityReversion` 只保留隔离研究实现。
- **必须排除已成方向的 bar**：三者共用同一个 ADX 门槛且 `VOLATILE` 最后赋值，
  若不排除则会无条件覆盖趋势状态。加密日线 ATR 中位数约为价格的 4.4%，
  远高于 `atr_pct_threshold`（2.5%），实测 BTC 2017-2026 上 96.3% 的 `TREND_UP`
  与 99.8% 的 `TREND_DOWN` 因此被吞掉，趋势策略在整段回测中从未被路由到。
- **`stability_period`**：当前默认 5；设为 1 表示不做去抖，每次原始状态翻转都立即切换。
  空仓标的只有在状态变化导致策略映射改变时才撤销旧入场订单并进入路由冷却，
  不同状态均映射到 `Cash` 时不产生该切换事件；已有仓位的策略退出规则仍可读取新状态。

> 历史口径变更示例（旧配置）：修复互斥后，BTC 状态分布由
> `VOLATILE 55.7% / SIDEWAYS 43.4% / TREND_UP 0.83% / TREND_DOWN 0.03%`
> 变为 `SIDEWAYS 43.2% / TREND_UP 22.8% / TREND_DOWN 18.0% / VOLATILE 16.0%`；
> 六标的样本总收益率由 -71.0% 变为 +74.4%（Profit Factor 0.71 → 1.29）。

### 4.2.1 Regime 切换与策略出场的优先级

当前契约采用 **先管理持仓、再收集新入场候选**：`Router.process_position_management`
先消费成交与平仓事件；已有仓位先检查显式 `MaxHoldingPeriod`（当前 365 天），否则
由批次账本记录的开仓策略执行 `process_exit_only`。状态映射变化本身不会产生
`StateSwitch` 强平；开仓策略可以按自身规则因状态不再允许而退出。

只有确认空仓后才执行 `collect_entry_candidate`。映射改变时撤销旧入场订单，设置
`cooldown_until = 当前 bar 索引 + cooldown_bars`（默认 2），索引不超过该值时暂停
新入场。候选由组合分配器统一排序后定仓与提交，普通退出单仍遵循次 bar 撮合。
常驻保护性止损另按回测的保守盘中路径处理；该模拟不能替代交易所保护单运行证据。

账户风控、持有期等外部退出也按成交生成权威 `CloseEvent`；原开仓策略在持仓实际
归零后按持仓身份接收一次汇总 `on_trade_closed`，部分平仓另有回调。
趋势策略健康状态由 `StrategyHealthMachine` 管理，兼容视图为
`scope=exit_cohort_aggregate`：按开仓策略、UTC 退出日和退出控制方合并观察，
非策略控制方有风险行动身份时再按该身份区分。当前仅 `strategy`、`router` 控制的
cohort 计入健康触发；账户风控退出保留归因但不单独证明策略失效。
自动表现失败进入有期限的冷静期和分阶段恢复；显式人工锁定仍需审计恢复。

## 4.3 结果可信度诊断（core/diagnostics.py）

`core/metrics/` 回答"策略表现如何"，`core/diagnostics.py` 回答两个前置问题：
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

- **缺失值**: 无真实 K 线的标的不参与该时间戳的策略路由；估值使用已知价格的延续值，
  不合成可交易 K 线，不从未来数据反填。
- **时区**: 所有数据统一标准化为 UTC 时间。
- **对齐**: 默认时间戳并集 (`union`)，也可显式选择交集 (`intersection`)；见下文审计口径。

## 6. 基准对比

策略表现将与以下基准进行对比:
- **固定等权持有**：只在起始时纳入当时可观察的标的，之后不再平衡；当前默认主基准。
- **动态等权再平衡**：按各时点可观察标的再平衡，保存权重、换手和配置交易成本。

## 7. 输出文件结构

CLI 回测会在 `reports/` 下建立独立的时间戳目录。产物取决于 `--report-profile`：

| 模式 | 用途与主要产物 |
| --- | --- |
| `workbook`（默认） | 日常研究；主要交付 `backtest_report.xlsx`，权益、成交和指标在工作簿中查看。 |
| `compact` | 轻量分享；交付 `report.pdf`、`dashboard.png` 与核心 CSV。 |
| `full` | 审计与复现；在报告、权益、成交、基准和路由文件之外，写入事件账本、数据快照与 `run_manifest.json`。 |

某些附加文件取决于是否有成交、基准和相应输入。以当次输出目录和运行结果为准，不应从文件名推断运行或研究已获验收。

## Phase 2：复现、时间对齐、基准与审计

只有 `--report-profile full` 生成完整审计包。其 `run_manifest.json` 记录 Git 提交、分支与脏工作树状态、依赖锁摘要、完整配置快照及哈希、请求与实际区间、数据源和逐标的哈希、随机种子及执行设置。引擎实际输入保存在 `data_inputs/`；`--replay-manifest` 会验证这些哈希，并精确比较成交、权益、基准和报告载荷的摘要。

多标的时间轴有明确的对齐口径：

- `union`（默认）：事件时间轴包含任一标的的真实 bar；只路由该时间点确实有 bar 的标的。
- `intersection`：只保留所有标的均有 bar 的时间点。

完整报告保留两种基准：固定等权基准只在起点买入当时可观察的标的，此后不再平衡；动态等权基准按每个时间点可观察的标的再平衡，并记录单向换手和配置成本。`benchmark.csv` 是选定的主基准；`benchmark_fixed.csv`、`benchmark_dynamic.csv`、`benchmark_weights.csv` 与 `benchmark_turnover_cost.csv` 保存审计轨迹。

数据异常会在 OHLCV 帧中标记，不会悄然删除；成交记录保留执行 bar 的异常标记。`event_log.jsonl` 包含信号、风险决策、订单意图、订单、成交和平仓事件；`routing_log.csv` 保存路由决策。发生交易却缺少必需事件族时，CLI 返回产物失败状态。独立第二数据源核验写入 `top_trade_market_data_audit.json`；没有提供第二来源目录时，状态明确为 `unverified`。

可提供逐时点成员 CSV，包含 `symbol`、`listed_at` 和可选 `delisted_at`。上市前、退市时及退市后的 bar 不可交易，退市标的的更早历史仍保留在样本中。只给静态标的列表时，报告会标明它不能控制幸存者偏差。
## 8. 指标统计口径

- **年化因子**：根据权益曲线时间索引的中位正间隔推断 `periods_per_year`；加密日线为 365.25，4 小时、1 小时和 15 分钟周期分别按每日 6、24 和 96 个周期年化。报告同时输出该因子。
- **月收益**：使用连续月末权益序列的 `pct_change`，因此包含月初跨期收益；首个不具备上月基准的月份不参与平均。
- **空值语义**：`0` 仅表示有效计算结果为零；数据不足使用 `null + insufficient`，数学不可定义使用 `null + undefined`，不输出无穷值。
- **Profit Factor**：同时输出闭合交易样本数、亏损交易数、状态及 95% Bootstrap 区间；少于 30 笔闭合交易标记为 `insufficient`。
- **回撤**：同时输出最大回撤峰值、谷底、恢复日期、持续周期/天数、恢复周期/天数、当前回撤、水下比例及是否尚未恢复。
- **输入不可变**：指标计算只读传入的权益曲线，不添加临时列或修改原数据。
