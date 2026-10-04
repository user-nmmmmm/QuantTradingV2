# QuantTrading 回测指标开发 Roadmap

> 文档状态：Draft v1.0  
> 创建日期：2026-08-01  
> 适用范围：回测指标、订单与成交分析、归因、稳健性验证及报告输出  
> 目标：把“收益不错”升级为“统计可信、风险透明、成交可解释、结果可复现”的回测结论。

---

## 1. 当前基线与主要缺口

当前 `backtest/reporting.py` 已输出 CAGR、总收益、最大回撤、月均收益、Sharpe、胜率、Profit Factor、Expectancy、平均盈利/亏损、手续费、滑点及策略级汇总。

当前主要缺口：

1. Sharpe 固定使用 `sqrt(252)`，不适合 365 日加密市场和其他 timeframe。
2. 只有最大回撤幅度，没有回撤持续时间、恢复时间和水下时间。
3. 只有指标点估计，没有置信区间、Bootstrap、PSR/DSR 或最小样本提示。
4. 权益曲线没有 Gross/Net Exposure、资金使用率、换手和风险贡献。
5. BT-03 已支持部分成交与 TIF，但报告没有订单数、成交率、剩余数量、等待时间和取消原因。
6. 成本归因不完整；spread、impact、funding、borrow 尚未独立记录。
7. 缺少按标的、方向、市场状态、年份和退出原因的统一归因。
8. 缺少 walk-forward、样本外、参数敏感性和多重测试校正。

最新固定 synthetic 回测为 50/50 测试全绿，但只有 8 笔完整交易，因此必须显式标注样本不足。
### 1.1 当前指标实现审查（2026-08-01）

| 优先级 | 问题 | 当前行为 | 目标行为 | 对应任务 |
| --- | --- | --- | --- | --- |
| P0 | Sharpe 年化固定为 252 | 加密日线和日内周期可能误算 | 推断并输出 periods_per_year | BM0-01、BM0-02 |
| P0 | 月收益漏掉月初跨期收益 | 当月末权益除以当月首条权益 | 月末权益序列 pct_change | BM0-07 |
| P0 | 指标函数修改输入 | 写入 equity_curve 的 month 列 | 指标函数只读输入 | BM0-08 |
| P0 | 不可定义 Sharpe 返回 0 | 数据不足与真实零值混淆 | 返回 null 与状态 | BM0-05 |
| P0 | Profit Factor 显示 inf 或极端值 | 少量亏损制造虚假精确度 | 输出亏损数、状态和区间 | BM2-09、BM2-10 |
| P0 | 回撤缺少日期和持续时间 | 只有幅度与金额 | 输出 peak/trough/recovery/duration | BM1-01～BM1-04 |
| P1 | 闭合交易字段不足 | FIFO 后丢失时间、价格、symbol、方向 | 输出完整 closed_trades.csv | BM0-04 |
| P1 | 未平仓仓位未进入交易统计 | 权益含浮盈亏，交易指标只看闭合交易 | 分离 closed/unrealized PnL 并对账 | BM0-09 |
| P1 | 滑点字段容易误解 | slip 是每单位价格差 | 拆分单位滑点、bps 和总成本 | BM0-03、BM4 |
| P1 | Expectancy 只有货币值 | 无法跨资金规模比较 | 增加百分比和 R-Multiple | BM2-04、BM6 |
| P1 | 策略归因只保留开仓策略 | 无法区分切换、熔断和平仓来源 | 保存 entry/exit strategy/reason | BM5 |
| P1 | 缺少暴露与资金效率 | 无法区分择时能力和方向暴露 | 增加 exposure/utilization/turnover | BM3 |

### 1.2 指标解释规则

- 0 只表示有效计算后结果确实为零。
- null + insufficient 表示样本不足。
- null + undefined 表示数学上不可定义。
- null + unmodeled 表示缺少成本或市场数据。
- inf 不直接写入 JSON 或报告，应转换为状态和警告。
- 胜率、Profit Factor、Expectancy 和 Sharpe 必须同时显示样本数。
- 最终权益必须通过 closed PnL、unrealized PnL、现金流和成本完成对账。

---

## 2. 开发原则

- 指标计算放在纯函数内核，报告层只展示，不重复实现公式。
- 每个指标必须定义输入、单位、年化方法、最小样本和 NaN 语义。
- 区分“值为零”“不可定义”“数据不足”和“尚未建模”。
- 不原地修改传入的 DataFrame。
- 任意汇总指标必须能追溯到 equity、order、fill 或 closed trade。
- 回测参数、随机种子、数据区间、配置哈希和试验次数必须随报告保存。
- 指标增加不能改变策略信号、订单或成交结果。

---

## 3. 目标输出文件

| 文件 | 主要内容 |
| --- | --- |
| `report.txt` | 人类可读的核心结论、警告和分组汇总 |
| `metrics.json` | 结构化指标、单位、状态、样本数和公式版本 |
| `metrics.csv` | timestamp/scope/metric/value/status/window 长表 |
| `equity.csv` | equity、cash、returns、drawdown、exposure、leverage、utilization |
| `orders.csv` | requested/filled/remaining、TIF、状态、等待时间、拒绝与取消原因 |
| `trades.csv` | fill、成本、participation、implementation shortfall、市场状态 |
| `closed_trades.csv` | FIFO 配对后的完整交易、持有期、PnL、R、MAE、MFE |
| `attribution.csv` | 策略、标的、方向、regime、时间段和退出原因归因 |
| `robustness.json` | Bootstrap、walk-forward、参数敏感性和多重测试结果 |

---

## 4. Phase BM0：统计口径修复

目标：先保证已有指标计算正确。

| ID | 工作项 | 主要文件 | 完成标准 |
| --- | --- | --- | --- |
| BM0-01 | 自动推断 `periods_per_year` | `core/metrics.py` | 1d/4h/1h/15m 与不规则序列测试通过 |
| BM0-02 | 修正 Sharpe 年化 | `backtest/reporting.py` | 不再固定 `sqrt(252)`；报告写出年化因子 |
| BM0-03 | 修正滑点金额 | `core/broker.py`, `backtest/reporting.py` | `slippage_cost = unit_slip × filled_qty` |
| BM0-04 | 统一闭合交易构建 | 新增 `core/trade_ledger.py` | 支持部分成交、加权成交价、跨 fill FIFO |
| BM0-05 | 指标状态协议 | `core/metrics.py` | 输出 `ok/insufficient/undefined/unmodeled` |
| BM0-06 | 公式单测 | 新增 `tests/test_metrics.py` | 固定手算样本精确断言 |
| BM0-07 | 修正月收益 | core/metrics.py | 月末权益序列 pct_change，覆盖跨月收益 |
| BM0-08 | 禁止修改输入 | core/metrics.py, backtest/reporting.py | 计算前后输入完全一致 |
| BM0-09 | 期末权益对账 | core/trade_ledger.py, backtest/reporting.py | closed/unrealized/cost/cash 与 End Equity 对上 |

验收门槛：

- 原报告变化均可由口径修复解释；
- 加密日线默认使用 365 或从索引可靠推断；
- 空序列、常数序列、单交易和无亏损样本不会产生误导性无穷值；
- 完整测试保持全绿。

---

## 5. Phase BM1：风险与回撤指标

| ID | 指标 | 定义/用途 |
| --- | --- | --- |
| BM1-01 | Current/Maximum Drawdown | 当前与历史最大水下幅度 |
| BM1-02 | Drawdown Duration | 峰值至恢复或回测结束的持续时间 |
| BM1-03 | Recovery Time | 谷底恢复至前高所需时间 |
| BM1-04 | Underwater Ratio | 回测期间未创新高的时间占比 |
| BM1-05 | Sortino Ratio | 只使用下行偏差的风险调整收益 |
| BM1-06 | Calmar Ratio | CAGR / abs(MaxDD) |
| BM1-07 | Ulcer Index | 回撤深度和持续性的综合度量 |
| BM1-08 | Historical VaR/CVaR | 尾部损失；优先 CVaR，不假设正态 |
| BM1-09 | Worst Period Returns | 最差日/周/月/季度收益 |

输出要求：所有回撤必须同时包含开始、谷底、恢复日期；未恢复回撤标记为 open。

---

## 6. Phase BM2：交易统计与样本可信度

| ID | 工作项 | 输出 |
| --- | --- | --- |
| BM2-01 | 样本摘要 | fills、closed trades、wins、losses、breakeven |
| BM2-02 | 胜率区间 | Wilson 或 Beta 区间，默认 95% |
| BM2-03 | Bootstrap | Return、Sharpe、MDD、PF、Expectancy 分布与区间 |
| BM2-04 | Payoff/Break-even | Payoff Ratio、Break-even Win Rate |
| BM2-05 | Streaks | 最大连续盈利/亏损及持续时间 |
| BM2-06 | Holding Period | 平均、中位数、p90 持有时间 |
| BM2-07 | PSR | Sharpe 超过基准值的概率 |
| BM2-08 | DSR | 校正非正态和多次试验选择偏差 |
| BM2-09 | Minimum Sample Warning | 交易数或历史长度不足时降级结论 |
| BM2-10 | Profit Factor 状态 | 输出 loss count、有限值/不可定义状态和区间 |

建议默认参数：

```yaml
analytics:
  confidence_level: 0.95
  bootstrap_samples: 5000
  bootstrap_seed: 42
  minimum_closed_trades: 30
  sharpe_benchmark: 0.0
  trials_count: 1
```

报告规则：少于 `minimum_closed_trades` 时仍显示点估计，但必须显示 `INSUFFICIENT SAMPLE`，不得只展示胜率和 Profit Factor。

---

## 7. Phase BM3：暴露、容量和资金效率

扩展 `equity.csv`：

- gross_exposure；
- net_exposure；
- long_exposure；
- short_exposure；
- leverage；
- cash_utilization；
- largest_position_pct；
- active_positions；
- current_drawdown。

新增汇总：

- Time in Market；
- 平均/最大 Gross Exposure；
- 平均绝对 Net Exposure；
- 空仓比例；
- Turnover；
- 每单位换手净收益；
- 每个标的和策略的风险贡献；
- 容量估计：订单数量相对 bar volume 的分布。

验收门槛：`GrossExposure >= abs(NetExposure)`，利用率和换手不得为负；缺价格时不得静默使用未来价格。

---

## 8. Phase BM4：订单与执行质量

扩展订单/成交字段：

```text
order_id, signal_id, symbol, side, order_type, time_in_force
requested_qty, filled_qty, remaining_qty, status
signal_time, submit_time, first_eligible_fill_time, fill_time
decision_price, arrival_price, fill_price
participation_rate, bars_to_fill
commission, spread_cost, impact_cost, funding_fee, borrow_cost
implementation_shortfall, reject_reason, cancel_reason
```

新增指标：

- Fill Rate、Partial Fill Rate；
- Reject/Cancel/Expire Rate；
- 平均与 p95 `bars_to_fill`；
- Requested vs Filled Qty；
- Participation Rate 分布；
- Implementation Shortfall；
- 成本占 Gross PnL 比例；
- 缺 bar 导致的不可成交次数；
- IOC/FOK 未成交与取消数量。

建议参数：

```yaml
execution:
  max_participation_rate: 0.01
  spread_bps: 2
  impact_coefficient: 0.10
  funding_rate_daily: null
  borrow_rate_annual: null
  default_time_in_force: GTC
  max_order_age_bars: 10
```

`null` 表示未建模，不能在报告中伪装成零成本。

---

## 9. Phase BM5：策略和市场归因

统一按以下维度切片：

- strategy；
- symbol；
- long/short；
- entry/exit regime；
- year/month；
- entry reason/exit reason；
- holding-period bucket；
- volatility bucket。

每个切片输出：Closed Trades、Net PnL、Contribution、Win Rate、PF、Expectancy、Payoff、MDD、Exposure、Turnover、Average Holding Period 和 Cost Ratio。

增加交叉检查：各分组 PnL 与账户总 PnL 必须在容差内完全对上。

---

## 10. Phase BM6：交易路径质量

| 指标 | 用途 |
| --- | --- |
| Initial Risk | 入场至初始止损的货币风险 |
| R-Multiple | 净 PnL / Initial Risk |
| MAE | 持仓期间最大不利变化 |
| MFE | 持仓期间最大有利变化 |
| MFE Capture Ratio | 最终利润占最大有利变化比例 |
| Stop Efficiency | 止损位置与实际 MAE 的关系 |
| Exit Efficiency | 退出价格在持仓区间中的位置 |
| SQN | 基于 R-Multiple 的系统质量指标 |

前置要求：持仓期间必须保存真实可见 bar 路径；不得使用入场前或退出后的数据。

---

## 11. Phase BM7：稳健性与过拟合控制

| ID | 工作项 | 验收标准 |
| --- | --- | --- |
| BM7-01 | 固定样本重复性 | 同配置运行三次，订单、fill、权益、指标一致 |
| BM7-02 | Train/Test Split | 样本外区间不参与参数选择 |
| BM7-03 | Walk-Forward | 多窗口训练、验证、向前推进 |
| BM7-04 | 参数敏感性 | 输出邻域热图和稳定区域，不只保存最优点 |
| BM7-05 | 成本压力测试 | 手续费、spread、impact、funding 乘数情景 |
| BM7-06 | 流动性压力测试 | 参与率和成交量折扣情景 |
| BM7-07 | Block Bootstrap | 保留时间相关性的置信区间 |
| BM7-08 | 多重测试记录 | 保存 trials_count 并计算 DSR |
| BM7-09 | PBO/CSC-V | 参数搜索较多时估计回测过拟合概率 |

建议 CLI 参数：

```text
--train-start --train-end --test-start --test-end
--walk-forward --bootstrap-samples --trials-count
--spread-bps --funding-rate --borrow-rate
--max-participation-rate --cost-multiplier
```

---

## 12. 实施顺序与依赖

```text
BM0 口径修复
  -> BM1 风险回撤
  -> BM2 样本可信度
  -> BM3 暴露资金效率
  -> BM4 执行质量
  -> BM5 归因
  -> BM6 路径质量
  -> BM7 稳健性与过拟合
```

建议开发批次：

1. **批次 A：BM0 + BM1**——先修正 Sharpe、滑点和回撤口径。
2. **批次 B：BM2 + BM3**——回答结果是否可信、承担了多少风险。
3. **批次 C：BM4**——回答订单为什么成交、成本来自哪里。
4. **批次 D：BM5 + BM6**——回答利润由谁产生、退出是否有效。
5. **批次 E：BM7**——回答换数据、参数和成本后是否仍然成立。

---

## 13. 建议代码结构

```text
core/
  metrics.py              # 纯指标函数
  trade_ledger.py         # fill -> closed trade
  attribution.py          # 分组归因
  robustness.py           # bootstrap/PSR/DSR/PBO
backtest/
  reporting.py            # 编排与展示
  report_schema.py        # metrics.json schema
tests/
  test_metrics.py
  test_trade_ledger.py
  test_attribution.py
  test_execution_metrics.py
  test_robustness.py
```

禁止策略类自行维护另一套 Sharpe、PF 或回撤公式。

---

## 14. 总体验收标准

- 所有指标有公式版本、单位、样本数和状态。
- 日线与日内数据年化正确，不规则时间轴不会静默误算。
- 报告不修改输入数据，也不改变回测交易结果。
- 部分成交、取消、过期和未成交均可审计。
- PnL、成本和归因可以逐层汇总对账。
- 少样本结果明确降级，不显示虚假精确度。
- 固定回测连续三次完全一致。
- Walk-forward 与样本外结果独立保存。
- 所有新增公式和边界情况有单元测试。
- 完整测试保持全绿后，才可在 roadmap 中勾选对应阶段。

