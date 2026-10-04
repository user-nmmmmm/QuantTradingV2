# QuantTrading 公式、监控与风险分析专项 Roadmap

> 文档状态：Draft  
> 适用项目：Still Water QuantTrading  
> 目标：在不破坏现有回测、路由和实盘原型的前提下，建立统一、可验证、可解释的量化指标与监控体系。  
> 关联文档：`docs/roadmap.md`、`docs/roadmap_detailed.md`、`docs/backtest_assumptions.md`

---

## 1. 文档目的

现有项目已经具备 OHLCV 数据、市场状态识别、策略路由、风险定仓、回测撮合、手续费/滑点、实盘轮询和基础报告，但“指标计算、监控、告警、风险动作”仍分散在不同模块中。

本 Roadmap 不以增加更多入场指标为第一目标，而是建立以下能力：

1. 使用同一套公式计算回测、模拟盘和实盘指标；
2. 区分“观测指标”“告警判断”“风险动作”，避免指标直接控制交易；
3. 对账户、组合、策略、市场、执行、数据和系统分别监控；
4. 通过滚动统计和分布漂移识别策略失效；
5. 让每个告警都能追溯到输入数据、公式、阈值和触发时间；
6. 为未来资金费率、期现基差、订单簿和跨交易所监控预留接口。

这是一份专项 Roadmap。系统安全、订单持久化、已收盘 K 线、部分成交和重启恢复等基础问题，仍以现有总体 Roadmap 为准。

---

## 2. 核心结论

### 2.1 当前最需要的不是更多 Alpha 公式

项目的首要缺口是：

- 指标计算没有统一入口，`core/metrics.py` 仍为空壳；
- Sharpe 固定使用 `sqrt(252)`，无法正确支持币圈日线和日内周期；
- 实盘状态文件只包含基础账户快照，没有策略健康、执行质量和数据质量；
- `TrendBreakout` 中所谓的 “Rolling Sharpe” 实际只是最近 20 笔 PnL 均值判断；
- 市场冲击成本虽然预留了平方根模型说明，实际仍是任意线性系数；
- Dashboard 只有样式工具，没有完整指标数据契约和可运行入口；
- 当前数据源主要是 OHLCV，不能可信计算资金费率、订单簿和逐笔成交类指标。

因此正确顺序应当是：

```text
统一时间与收益口径
  -> 纯函数指标内核
  -> 回测报告指标
  -> 实盘快照与监控
  -> 告警状态机
  -> 风险动作
  -> 策略漂移检测
  -> 衍生品与微观结构扩展
```

### 2.2 首批必须落地的公式

第一批只选择对现有 OHLCV、权益曲线和成交记录直接可用的指标：

1. 简单收益率、对数收益率和自动年化因子；
2. 当前回撤、最大回撤、回撤持续时间；
3. Rolling Sharpe、Sortino、Calmar、Ulcer Index；
4. VaR、CVaR；
5. Gross Exposure、Net Exposure、杠杆、资金使用率；
6. Rolling Expectancy、Profit Factor、Payoff Ratio、R-Multiple、SQN；
7. EWMA 波动率、Volatility Ratio、Volatility of Volatility；
8. Implementation Shortfall、滑点 Z-score、Participation Rate；
9. 数据陈旧度、缺失率、异常价格 Robust Z-score；
10. CUSUM/Page-Hinkley 策略衰减检测。

### 2.3 首批明确不做

在数据和执行架构未扩展前，不把以下公式列入首批实现：

- Order Book Imbalance、Microprice、VPIN、Kyle Lambda；
- 资金费率套利、期现基差和 Delta-neutral 收益归因；
- 强平价格通用模型；
- 全 Kelly 仓位；
- 直接使用 Hurst 或熵作为开仓信号；
- 未经样本外验证的自动参数优化。

---

## 3. 当前项目能力审计

| 领域 | 已有能力 | 主要缺口 | 结论 |
| --- | --- | --- | --- |
| 数据 | CCXT/Yahoo/Synthetic OHLCV、质量检查 | 未完成 K 线过滤、统一时区、衍生品字段、订单簿和逐笔成交 | 首批仅依赖 OHLCV |
| 指标 | SMA、EMA、ATR、ADX、布林带 | 没有统一指标注册、元数据、窗口和单位契约 | 扩展 `core/metrics.py` |
| 状态 | SMA + ADX + ATR% 路由 | 状态维度少，缺乏波动变化、随机性和流动性判断 | 后续增加观察型特征 |
| 策略 | 趋势、突破、均值回归 | 健康检测分散，统计口径不严谨 | 统一策略健康引擎 |
| 风控 | 止损距离定仓、杠杆、集中度、成交量限制、日内熔断 | 无波动率目标、CVaR、组合相关性和分级风险动作 | 分阶段增强 |
| 回测 | Next-Bar、费用、滑点、基础冲击成本 | 年化错误、缺少暴露/换手/风险贡献/执行质量 | 优先修复报告 |
| 实盘 | 账户同步、下单、基础状态导出 | 状态持久化、订单生命周期、指标快照、延迟和告警不足 | 先扩展遥测，不直接自动处置 |
| Dashboard | 样式工具 | 无稳定数据契约、无完整入口 | 等状态快照 v2 后开发 |

### 3.1 需要保留的现有实现

- `core/indicators.py`：继续负责面向 K 线的技术指标；
- `core/state.py`：继续负责市场状态分类；
- `core/risk.py`：继续负责交易前风险判断；
- `backtest/reporting.py`：保留为报告编排层，但不再自行重复实现公式；
- `live_trading/engine.py`：保留状态导出职责，改为消费统一指标快照；
- `core/portfolio.py`：继续作为现金、持仓和权益的基础模型；
- `core/broker.py` 与 `core/live_broker.py`：继续产生订单与成交事实。

### 3.2 应避免的职责混合

以下逻辑不应继续写在策略或报告文件中：

- 策略类自己计算 Sharpe、Profit Factor 或漂移统计；
- 报告模块自己定义年化和回撤口径；
- Dashboard 从原始成交数据临时拼装核心风险指标；
- 告警逻辑直接散落在 `LiveTradingEngine._tick()`；
- 指标函数内部直接执行减仓、平仓或关闭策略。

---

## 4. 设计原则

### 4.1 三层分离

```text
Metric：发生了什么
  -> Health/Alert：是否异常
  -> Policy/Action：允许做什么
```

示例：

- Metric：当前回撤为 `-8.3%`；
- Alert：超过 Warning 阈值 `-8%`；
- Policy：新仓风险预算降低为原来的 50%；
- Action：RiskManager 对新订单应用新的风险预算。

指标本身不应直接平仓。

### 4.2 纯函数优先

公式函数应满足：

- 输入显式；
- 输出确定；
- 不读全局配置；
- 不写文件；
- 不修改输入 DataFrame；
- 能对边界条件单元测试；
- 回测与实盘调用同一个函数。

### 4.3 时间语义必须显式

每个指标必须声明：

- 输入频率；
- 窗口长度；
- 是否只使用已收盘数据；
- 最小有效样本数；
- 年化方式；
- 时区；
- 输出时间戳代表窗口结束还是计算时间。

禁止默认假设所有数据都是 252 个交易日的股票日线。

### 4.4 指标值、阈值和动作分别配置

指标计算不包含业务阈值。例如 `rolling_sharpe()` 只返回数值，不判断是否停用策略。阈值放入监控配置：

```yaml
monitoring:
  strategy:
    rolling_sharpe:
      window: 90
      warn_below: 0.5
      critical_below: 0.0
      consecutive_breaches: 3
  portfolio:
    drawdown:
      warn_below: -0.08
      critical_below: -0.12
      halt_below: -0.18
```

### 4.5 使用滞回和连续确认

如果告警阈值为 \(h\)，不应在指标围绕 \(h\) 波动时反复开关。

建议：

- 连续 \(k\) 次超限才升级；
- 恢复阈值与触发阈值不同；
- Critical 降级需要人工确认或更长恢复期；
- Halt 不自动恢复。

示例：

```text
WARN 触发：Drawdown <= -8%
WARN 恢复：Drawdown >= -6%
确认次数：连续 3 个已收盘 bar
```

### 4.6 缺失值不是零

以下情况必须输出 `NaN` 或 `INSUFFICIENT_DATA`，不能输出 0：

- 窗口未预热；
- 标准差为零导致 Sharpe 不可定义；
- 无亏损样本导致 Profit Factor 分母为零；
- 数据频率无法推断；
- 价格或权益无效；
- 当前周期没有成交。

监控层必须区分：

- 正常值为零；
- 指标不可定义；
- 数据未就绪；
- 数据源故障。

---

## 5. 目标架构

### 5.1 数据流

```text
Bar / Fill / PortfolioSnapshot / SystemEvent
                 |
                 v
          Metric Engine
                 |
                 v
          MetricSnapshot
                 |
        +--------+---------+
        |                  |
        v                  v
  Report/Research     Health Evaluator
                           |
                           v
                     Alert State Machine
                           |
                           v
                       Risk Policy
```

### 5.2 建议新增模块

```text
core/
  metrics.py                 # 通用纯函数：收益、回撤、风险、交易统计
  monitoring/
    __init__.py
    contracts.py             # MetricPoint、MetricSnapshot、AlertEvent
    engine.py                # 按账户/策略/标的聚合指标
    health.py                # 阈值、连续确认、滞回、状态机
    policy.py                # 告警到风险预算/交易权限的映射
    serialization.py         # JSON 安全序列化与 schema_version

analysis/
  drift.py                   # PSI、JS、CUSUM、Page-Hinkley、IC
  portfolio_risk.py          # 协方差、风险贡献、压力测试

tests/
  test_metrics.py
  test_monitoring_engine.py
  test_health_state_machine.py
  test_monitoring_parity.py
```

### 5.3 核心数据契约

建议所有指标输出统一结构：

```python
MetricPoint(
    name="portfolio.drawdown.current",
    value=-0.083,
    timestamp="2026-07-28T00:00:00Z",
    scope_type="portfolio",
    scope_id="main",
    unit="ratio",
    window="expanding",
    status="valid",
    sample_count=420,
    metadata={"source": "equity_curve"},
)
```

每次计算生成一个 `MetricSnapshot`：

```json
{
  "schema_version": "2.0",
  "timestamp": "2026-07-28T00:00:00Z",
  "engine_status": "running",
  "metrics": {},
  "alerts": [],
  "data_quality": {},
  "positions": {},
  "orders": {}
}
```

---

## 6. 公式目录与技术原理

## 6.1 收益与年化基础

### M-001 简单收益率

\[
r_t=\frac{P_t}{P_{t-1}}-1
\]

用途：

- 账户净值变化；
- 组合收益；
- 报告展示；
- 与真实资金盈亏直观对应。

边界：

- \(P_{t-1}\le0\) 时无效；
- 缺失时间点不能默认收益为零；
- 多周期累计必须使用复利。

\[
R_{1:T}=\prod_{t=1}^{T}(1+r_t)-1
\]

### M-002 对数收益率

\[
\ell_t=\ln\frac{P_t}{P_{t-1}}
\]

用途：

- 波动率估计；
- 回归、协方差和漂移检测；
- 多周期可加总。

账户最终收益展示仍应转换回简单收益率：

\[
R=e^{\sum_t\ell_t}-1
\]

### M-003 自动年化因子

设相邻有效时间戳的中位间隔为 \(\Delta t\) 秒：

\[
A=\frac{365.25\times24\times3600}{\Delta t}
\]

候选值应映射到已知频率，例如：

| 周期 | 年化因子 |
| --- | ---: |
| 1d | 365.25 |
| 4h | 2191.5 |
| 1h | 8766 |
| 15m | 35064 |
| 5m | 105192 |

注意：

- 只有收益近似独立同分布时，\(\sqrt A\) 波动率年化才严格成立；
- 对不规则时间序列，应拒绝自动年化或显式重采样；
- 年化因子必须写入报告元数据。

---

## 6.2 回撤与账户风险

### M-010 当前回撤与最大回撤

\[
H_t=\max_{i\le t}E_i
\]

\[
DD_t=\frac{E_t-H_t}{H_t}
\]

\[
MDD=\min_t DD_t
\]

监控含义：

- `current_drawdown`：当前离高点多远；
- `max_drawdown`：历史最严重损失；
- `drawdown_velocity`：风险恶化速度；
- `drawdown_duration`：多久没有创新高。

### M-011 回撤持续时间

\[
Duration_t=t-\max\{i\le t:E_i=H_i\}
\]

输出应同时包含：

- bar 数；
- 墙上时间，例如小时或天；
- 当前持续时间；
- 历史最长持续时间。

### M-012 回撤速度

\[
DDVelocity_{t,k}=DD_t-DD_{t-k}
\]

它适合提前预警。账户尚未达到熔断线，但如果回撤在短窗口快速扩大，应减少风险预算。

### M-013 Ulcer Index

\[
UI=\sqrt{\frac1N\sum_{t=1}^{N}DD_t^2}
\]

原理：

- 最大回撤只观察最坏点；
- Ulcer Index 同时惩罚回撤深度和停留时间；
- 对“长期缓慢亏损”的策略比 MDD 更敏感。

### M-014 Calmar Ratio

\[
Calmar=\frac{CAGR}{|MDD|}
\]

当 \(MDD=0\) 时不可定义，不应返回无穷大作为正常值。

### M-015 VaR 与 CVaR

历史 VaR：

\[
VaR_\alpha=-Q_\alpha(r)
\]

历史 CVaR：

\[
CVaR_\alpha=-E[r\mid r\le Q_\alpha(r)]
\]

建议：

- 监控优先使用 CVaR；
- 同时输出 95% 和 99%；
- 样本不足时明确标记；
- 不把正态分布 VaR 作为币圈唯一尾部风险度量。

---

## 6.3 风险调整绩效

### M-020 Rolling Sharpe

\[
Sharpe_t=
\frac{\bar r_{t,N}-r_f/A}
{s(r_{t,N})}\sqrt A
\]

币圈短周期监控中可先令 \(r_f=0\)，但接口应保留无风险利率。

推荐窗口：

- 短期：30 个自然日等价窗口；
- 中期：90 个自然日等价窗口；
- 长期：365 个自然日等价窗口。

不能直接固定为 30/90/365 根 bar，因为 1h 与 1d 含义不同。

### M-021 Rolling Sortino

\[
DownsideDeviation=
\sqrt{\frac1N\sum_{i=1}^{N}\min(r_i-T,0)^2}
\]

\[
Sortino=
\frac{\bar r-T}{DownsideDeviation}\sqrt A
\]

其中 \(T\) 是最小可接受收益。它只惩罚下行波动，更适合偏态策略。

### M-022 Recovery Factor

\[
RecoveryFactor=
\frac{NetProfit}{|MDDAmount|}
\]

用于评价为了获得当前利润付出了多大的历史回撤代价。

### M-023 资金使用效率

\[
Utilization_t=
\frac{GrossExposure_t}{Equity_t}
\]

\[
CapitalEfficiency=
\frac{AnnualizedReturn}{Mean(Utilization)}
\]

避免把高杠杆带来的收益误认为高质量 Alpha。

---

## 6.4 组合暴露与风险贡献

### M-030 Gross / Net Exposure

\[
GrossExposure_t=
\frac{\sum_i|q_{i,t}P_{i,t}|}{E_t}
\]

\[
NetExposure_t=
\frac{\sum_iq_{i,t}P_{i,t}}{E_t}
\]

二者必须同时存在：

- Net 接近零不代表风险小；
- 多空对冲组合可能具有很高 Gross；
- 方向风险、杠杆风险和基差风险不能混为一谈。

### M-031 换手率

\[
Turnover_t=
\frac{\sum_i|\Delta Notional_{i,t}|}{E_t}
\]

输出：

- 单周期换手；
- 日均换手；
- 年化换手；
- 按策略和标的拆分。

换手是费用、滑点和容量风险的核心解释变量。

### M-032 组合波动率

\[
\sigma_p=\sqrt{w^\top\Sigma w}
\]

必须使用滚动协方差，并对缺失值、不同上市时间和协方差不稳定进行处理。

### M-033 风险贡献

\[
MRC_i=\frac{(\Sigma w)_i}{\sqrt{w^\top\Sigma w}}
\]

\[
RC_i=w_iMRC_i
\]

用途：

- 判断单个币、策略或方向是否贡献了过多组合风险；
- 对高度相关的 BTC、ETH、SOL 多头进行聚合风险控制；
- 为后续风险预算定仓提供基础。

### M-034 有效独立头寸数量

若 \(p_i\) 为归一化风险贡献：

\[
N_{\mathrm{eff}}=\frac{1}{\sum_i p_i^2}
\]

持有 10 个币不代表有 10 个独立风险来源。

### M-035 压力损失

\[
StressLoss_s=\sum_i w_iR_{i,s}-CostShock_s
\]

首批场景建议：

1. BTC -10%，ETH -14%，其他币 -25%；
2. 相关性提高到 0.9；
3. 滑点扩大 3 倍；
4. 成交量下降 70%；
5. 所有空头回补成本上升；
6. 组合无法在当前 bar 全部平仓。

---

## 6.5 策略健康指标

### M-040 Rolling Expectancy

\[
Expectancy=p\cdot AvgWin-(1-p)\cdot AvgLoss
\]

更稳健的实现直接使用每笔净收益均值：

\[
Expectancy_N=\frac1N\sum_{i=1}^{N}PnL_i
\]

应同时提供：

- 金额期望；
- 收益率期望；
- R-Multiple 期望。

### M-041 Profit Factor

\[
PF=\frac{\sum_i\max(PnL_i,0)}
{\left|\sum_i\min(PnL_i,0)\right|}
\]

边界：

- 没有亏损样本时不能直接解释为“无限优秀”；
- 样本少于最小交易数时状态为 `INSUFFICIENT_DATA`；
- 必须使用扣除手续费和滑点后的净 PnL。

### M-042 Payoff Ratio 与盈亏平衡胜率

\[
Payoff=\frac{AvgWin}{AvgLoss}
\]

\[
BreakEvenWinRate=\frac{1}{1+Payoff}
\]

监控实际胜率与盈亏平衡胜率之差：

\[
WinRateEdge=ObservedWinRate-BreakEvenWinRate
\]

### M-043 R-Multiple

\[
R_i=\frac{NetPnL_i}{InitialRisk_i}
\]

其中：

\[
InitialRisk_i=|EntryPrice_i-StopPrice_i|\times Qty_i
\]

必须在下单时记录初始风险，不能在平仓后反推。

### M-044 SQN

\[
SQN=\sqrt N\frac{\bar R}{s_R}
\]

适合比较不同标的、不同仓位和不同价格水平下的策略质量，但仍需防止小样本误判。

### M-045 胜率偏移 Z-score

假设长期基准胜率为 \(p_0\)，最近 \(N\) 笔胜利次数为 \(W\)：

\[
Z=\frac{W-Np_0}{\sqrt{Np_0(1-p_0)}}
\]

它用于判断近期胜率下降是否可能超出随机波动。

### M-046 连续亏损异常度

若单笔亏损概率为 \(q\)，连续亏损 \(k\) 次的局部近似概率为：

\[
P_k\approx q^k
\]

仅用于提示，不作为精确的全样本“至少出现一次连续亏损”的概率。

---

## 6.6 策略漂移与失效检测

### M-050 CUSUM

\[
S_t^+=\max(0,S_{t-1}^++x_t-\mu_0-k)
\]

\[
S_t^-=\min(0,S_{t-1}^-+x_t-\mu_0+k)
\]

当：

\[
S_t^+>h\quad\text{或}\quad S_t^-<-h
\]

认为均值出现结构变化。

用途：

- 监控策略净收益；
- 监控滑点；
- 监控成交率；
- 监控信号后未来收益。

### M-051 Page-Hinkley

\[
m_t=m_{t-1}+x_t-\bar x_t-\delta
\]

\[
PH_t=m_t-\min_{i\le t}m_i
\]

当 \(PH_t>\lambda\) 时触发漂移告警。相比简单滚动均值，它对持续的小幅恶化更敏感。

### M-052 PSI

\[
PSI=\sum_i(p_i-q_i)\ln\frac{p_i}{q_i}
\]

其中 \(p_i\) 为基准期分布占比，\(q_i\) 为当前期分布占比。

适合监控：

- ATR%；
- ADX；
- Z-score；
- 成交量；
- 持仓时间；
- 滑点；
- 入场时市场状态分布。

### M-053 Jensen-Shannon Divergence

\[
M=\frac12(P+Q)
\]

\[
JS(P,Q)=\frac12KL(P\|M)+\frac12KL(Q\|M)
\]

JS 对称且有界，比 KL 更适合作为 Dashboard 的分布漂移指标。

### M-054 信号 IC 与 ICIR

\[
IC_t=Corr(signal_t,r_{t+h})
\]

\[
ICIR=\frac{Mean(IC)}{Std(IC)}
\]

前提是策略输出连续信号强度，而不仅是 `buy/short/none`。因此应先扩展 `Signal` 数据契约，再实现 IC。

---

## 6.7 市场状态观察指标

### M-060 EWMA 波动率

\[
\sigma_t^2=\lambda\sigma_{t-1}^2+(1-\lambda)r_{t-1}^2
\]

用途：

- 风险目标仓位；
- 当前波动水平；
- 滑点和冲击成本输入；
- 市场状态辅助。

参数不应对所有周期固定使用同一 \(\lambda\)，应通过半衰期定义：

\[
\lambda=2^{-1/H}
\]

其中 \(H\) 是半衰期对应的 bar 数。

### M-061 Volatility Ratio

\[
VolRatio_t=
\frac{\sigma_{\mathrm{short},t}}
{\sigma_{\mathrm{long},t}}
\]

解释：

- \(>1\)：波动扩张；
- \(<1\)：波动压缩；
- 极端上升：降低均值回归风险；
- 长期压缩：观察突破风险，但不直接开仓。

### M-062 Volatility of Volatility

\[
VoV_t=Std(\hat\sigma_{t-N:t})
\]

高 VoV 说明风险环境变化快，即使平均波动率尚未极端，也可能需要降低仓位。

### M-063 Parkinson 波动率

\[
\sigma_P^2=
\frac{1}{4N\ln2}
\sum_{t=1}^{N}
\left(\ln\frac{H_t}{L_t}\right)^2
\]

优点：充分利用高低价。  
限制：不处理跳跃和漂移，对异常针形 K 线敏感。

### M-064 Garman-Klass 波动率

\[
\sigma_{GK}^2=
\frac1N\sum_{t=1}^{N}
\left[
\frac12\left(\ln\frac{H_t}{L_t}\right)^2
-(2\ln2-1)\left(\ln\frac{C_t}{O_t}\right)^2
\right]
\]

建议与 ATR%、EWMA 并列展示，不立即替代现有状态机。

### M-065 Variance Ratio

\[
VR(q)=
\frac{Var(r_t+\cdots+r_{t-q+1})}
{qVar(r_t)}
\]

观察含义：

- \(VR>1\)：正自相关倾向；
- \(VR<1\)：负自相关倾向；
- \(VR\approx1\)：接近随机游走。

必须配合统计显著性和多个 \(q\) 值，不能用单一阈值决定策略。

### M-066 Hurst 指数

\[
E[R(n)/S(n)]\propto n^H
\]

使用限制：

- 对窗口、估计方法和非平稳性敏感；
- 小样本误差大；
- 只作为慢速观察指标；
- 不直接用于开仓。

### M-067 Permutation Entropy

\[
PE=-\sum_\pi p(\pi)\ln p(\pi)
\]

归一化后可用于观察序列复杂度。高熵不等于一定不能交易，低熵也不保证未来结构持续。

### M-068 市场宽度和横截面离散度

\[
Breadth_t=
\frac{\#\{i:r_{i,t}>0\}}{N}
\]

\[
Dispersion_t=Std(r_{1,t},\ldots,r_{N,t})
\]

需要稳定的交易标的池，避免存活者偏差和币种频繁更换导致指标失真。

---

## 6.8 流动性与执行质量

### M-070 Implementation Shortfall

买入：

\[
IS_{buy}=\frac{P_{fill}-P_{decision}}{P_{decision}}
\]

卖出：

\[
IS_{sell}=\frac{P_{decision}-P_{fill}}{P_{decision}}
\]

完整实现应拆分：

\[
IS_{total}=DelayCost+SpreadCost+ImpactCost+Fee+OpportunityCost
\]

首批先实现决策价到成交价的净偏差，并保存：

- signal price；
- submit price；
- arrival price；
- fill price；
- fill time。

### M-071 滑点 Z-score

\[
SlippageZ_t=
\frac{Slippage_t-Median(Slippage)}
{1.4826\times MAD(Slippage)}
\]

首选稳健版本，避免极端滑点污染均值和标准差。

### M-072 Participation Rate

\[
Participation_t=
\frac{OrderQty_t}{BarVolume_t}
\]

对于金额口径：

\[
NotionalParticipation_t=
\frac{OrderNotional_t}{BarDollarVolume_t}
\]

当前 OHLCV 只能做粗略估计，不能代表真实可成交深度。

### M-073 平方根市场冲击

\[
Impact_t=\eta\sigma_t\sqrt{\frac{Q_t}{ADV_t}}
\]

开发要求：

- \(\eta\) 可配置；
- \(\sigma_t\) 使用统一波动率；
- \(Q/ADV\) 单位一致；
- 记录冲击成本而非只修改成交价；
- 对极小成交量设置上限和拒单策略；
- 用实盘成交数据逐步校准。

### M-074 成交率、拒单率和取消率

\[
QtyFillRate=\frac{FilledQty}{SubmittedQty}
\]

\[
OrderFillRate=\frac{FilledOrders}{SubmittedOrders}
\]

\[
RejectRate=\frac{RejectedOrders}{SubmittedOrders}
\]

\[
CancelRate=\frac{CancelledOrders}{SubmittedOrders}
\]

必须按以下维度拆分：

- 交易所；
- 标的；
- 策略；
- 订单类型；
- 时间段；
- 市场状态。

### M-075 延迟分位数

监控：

\[
Latency_{p50},\ Latency_{p95},\ Latency_{p99}
\]

拆分：

- market data latency；
- strategy compute latency；
- order submit latency；
- exchange acknowledgement latency；
- fill latency；
- state export latency。

平均延迟不能代替尾部延迟。

---

## 6.9 数据与系统健康

### M-080 数据陈旧度

\[
Staleness_t=Now-LastClosedBarTime
\]

阈值必须相对 timeframe：

\[
StalenessRatio=
\frac{Staleness}{BarInterval}
\]

例如 1d 数据延迟 2 分钟正常，1m 数据延迟 2 分钟则严重异常。

### M-081 缺失率

\[
MissingRate=
\frac{ExpectedBars-ActualBars}{ExpectedBars}
\]

需要区分：

- 交易所未返回；
- 标的未上市；
- 本地拉取失败；
- 数据被质量规则剔除。

### M-082 Robust Z-score

\[
RobustZ_t=
\frac{x_t-Median(x)}
{1.4826\times MAD(x)}
\]

可应用于：

- 收益跳变；
- 成交量异常；
- 高低价振幅；
- 滑点；
- 延迟；
- 订单数量。

### M-083 跨交易所价格偏离

\[
Deviation_t=
\frac{P_{A,t}-P_{B,t}}
{(P_{A,t}+P_{B,t})/2}
\]

这是未来多数据源阶段的质量监控指标，不应在首批单交易所版本中模拟。

### M-084 心跳成功率与错误率

\[
HeartbeatSuccessRate=
\frac{SuccessfulTicks}{ExpectedTicks}
\]

\[
ErrorRate=
\frac{FailedOperations}{TotalOperations}
\]

按滚动窗口统计，并对行情、账户同步、下单、状态导出分别计算。

---

## 6.10 衍生品与币圈专项指标

本节需要扩展 CCXT 数据源、账户模型和成本归因后才能进入正式开发。

### M-090 资金费率 Z-score

\[
FundingZ_t=
\frac{f_t-Mean(f_{t-N:t})}
{Std(f_{t-N:t})}
\]

用途：观察永续合约拥挤程度，不直接等同于反向信号。

### M-091 年化资金费率

若每 8 小时结算：

\[
FundingAnnualized=(1+f)^{3\times365}-1
\]

报告必须同时展示简单年化与复利年化，并明确结算频率。

### M-092 基差

\[
Basis_t=\frac{F_t-S_t}{S_t}
\]

交割合约年化：

\[
BasisAnnualized=
\left(\frac{F_t}{S_t}-1\right)\frac{365}{D}
\]

永续合约不能使用固定到期日年化公式。

### M-093 标记价与指数价偏离

\[
MarkDeviation_t=
\frac{MarkPrice_t-IndexPrice_t}{IndexPrice_t}
\]

适合监控强平风险和交易所局部异常。

### M-094 Open Interest 变化

\[
OIChange_t=\frac{OI_t-OI_{t-1}}{OI_{t-1}}
\]

与价格、成交量和资金费率联合观察：

- 价格上涨 + OI 上升：新增仓位推动；
- 价格上涨 + OI 下降：空头回补可能性；
- 高资金费率 + OI 快速上升：拥挤风险。

### M-095 清算强度

\[
LiquidationIntensity_t=
\frac{LiquidationNotional_t}
{MarketDollarVolume_t}
\]

需要可靠的清算数据源，不能从普通 OHLCV 推断。

---

## 7. 指标优先级矩阵

| 级别 | 指标 | 当前数据可用 | 主要用途 | 是否影响交易 |
| --- | --- | --- | --- | --- |
| P0 | 自动年化、收益、回撤 | 是 | 修正报告口径 | 否 |
| P0 | Gross/Net Exposure | 是 | 组合风险 | 仅观察 |
| P0 | 数据陈旧度、缺失率 | 是 | 防止错误数据交易 | 可阻止新仓 |
| P1 | Sharpe/Sortino/Calmar/UI | 是 | 账户与策略健康 | 先观察 |
| P1 | Expectancy/PF/R/SQN | 是，但需补初始风险字段 | 策略健康 | 先观察 |
| P1 | EWMA/VolRatio/VoV | 是 | 市场风险与动态仓位 | 后续影响仓位 |
| P1 | IS/滑点 Z/参与率 | 部分可用 | 执行质量 | 后续限制订单 |
| P1 | VaR/CVaR/压力损失 | 是 | 尾部风险 | 后续风险预算 |
| P2 | CUSUM/PH/PSI/JS | 是 | 漂移检测 | 分级降仓 |
| P2 | 风险贡献/有效头寸数 | 是 | 组合集中度 | 组合预算 |
| P2 | VR/Hurst/Entropy | 是 | 市场观察 | 不直接开仓 |
| P3 | Funding/Basis/OI | 否 | 衍生品监控 | 扩展后决定 |
| P4 | OBI/Microprice/VPIN | 否 | 高频微观结构 | 当前不做 |

---

## 8. 告警等级和风险动作

### 8.1 等级定义

| 等级 | 含义 | 默认动作 |
| --- | --- | --- |
| NORMAL | 正常 | 不处理 |
| OBSERVE | 轻微偏离 | 记录、展示 |
| WARNING | 持续异常 | 通知、降低新仓风险预算 |
| CRITICAL | 高风险 | 禁止部分策略或标的新开仓 |
| HALT | 系统/账户安全风险 | 撤销挂单、禁止新仓，是否平仓由独立策略决定 |

### 8.2 指标到动作示例

| 指标 | Warning | Critical | Halt |
| --- | --- | --- | --- |
| 当前回撤 | \(\le-8\%\) | \(\le-12\%\) | \(\le-18\%\) |
| 数据陈旧度 | \(>1.5\) bar | \(>2\) bar | \(>3\) bar |
| Rolling Sharpe | \(<0.5\) | \(<0\) | 不单独 Halt |
| 滑点 Robust Z | \(>2\) | \(>3\) | 持续 \(>4\) |
| Reject Rate | \(>2\%\) | \(>5\%\) | \(>20\%\) |
| 账户对账差异 | 非零小差异 | 超过配置容差 | 无法确认真实仓位 |

这些仅是初始模板，不是最终生产阈值。最终阈值必须通过历史分布、sandbox 和故障演练校准。

### 8.3 风险预算乘数

建议告警层输出风险预算乘数，而不是直接修改策略：

\[
AdjustedRiskBudget=
BaseRiskBudget
\times M_{portfolio}
\times M_{strategy}
\times M_{market}
\times M_{execution}
\]

每个乘数限定在 \([0,1]\)：

| 状态 | 乘数示例 |
| --- | ---: |
| NORMAL | 1.00 |
| OBSERVE | 1.00 |
| WARNING | 0.50 |
| CRITICAL | 0.00 |
| HALT | 0.00 |

必须设置最严格状态优先，禁止多个指标各自独立下单或平仓。

---

## 9. 分阶段开发 Roadmap

## Phase FM0：指标正确性基线

目标：先修复统计口径，建立后续开发的可信基础。

### 工作项

| ID | 工作项 | 主要文件 | 交付物 |
| --- | --- | --- | --- |
| FM0-01 | 定义收益率和时间频率契约 | `core/metrics.py` | 简单/对数收益率、频率推断 |
| FM0-02 | 修正 Sharpe 年化 | `backtest/reporting.py` | 不再固定 `sqrt(252)` |
| FM0-03 | 统一回撤实现 | `core/metrics.py` | 当前/MDD/持续时间/速度 |
| FM0-04 | 明确 NaN 和最小样本语义 | `core/metrics.py` | 指标状态约定 |
| FM0-05 | 建立公式单测 | `tests/test_metrics.py` | 固定样本精确断言 |

### 验收标准

- 1d、4h、1h、15m 数据得到正确年化因子；
- 不规则时间序列不会静默使用错误年化；
- 回撤结果与手工样本一致；
- 空序列、常数序列、单点序列和 NaN 均有定义；
- 原有回测基线变化能由“统计口径修正”解释。

---

## Phase FM1：统一指标内核

目标：让回测报告、实盘监控和研究脚本共享同一套纯函数。

### 工作项

| ID | 工作项 | 公式 |
| --- | --- | --- |
| FM1-01 | 风险调整绩效 | Sharpe、Sortino、Calmar、UI |
| FM1-02 | 尾部风险 | VaR、CVaR |
| FM1-03 | 交易统计 | Expectancy、PF、Payoff、Break-even Win Rate |
| FM1-04 | 风险单位 | Initial Risk、R-Multiple、SQN |
| FM1-05 | 暴露与换手 | Gross、Net、Utilization、Turnover |
| FM1-06 | 波动率 | EWMA、VolRatio、VoV、Parkinson、GK |

### 验收标准

- 所有函数无文件 IO、无全局配置、无输入原地修改；
- 每个公式有单位、窗口、最小样本和异常值测试；
- 报告层只负责调用和展示；
- 同样输入在研究、回测和实盘回放中结果一致。

---

## Phase FM2：回测可观测性

目标：让每次回测不仅输出收益，还解释风险和成本来源。

### 工作项

1. 扩展 `equity.csv`：
   - gross exposure；
   - net exposure；
   - leverage；
   - current drawdown；
   - risk budget multiplier；
   - active alert count。
2. 扩展 `trades.csv`：
   - decision price；
   - arrival price；
   - initial risk；
   - R-Multiple；
   - participation rate；
   - implementation shortfall；
   - market state at entry/exit。
3. 新增 `metrics.csv`：
   - 长表结构；
   - timestamp、scope、metric、value、status、window。
4. 新增 `alerts.csv`：
   - 触发、升级、恢复时间；
   - 阈值和实际值；
   - 对应策略/标的/账户。
5. 报告增加：
   - 回撤持续时间；
   - Rolling Sharpe/Sortino；
   - 策略 Expectancy/PF/SQN；
   - 费用与换手；
   - 市场状态归因；
   - 风险贡献。

### 验收标准

- 任意报告数值可追溯到公式和输入；
- 报告不修改原始 `equity_curve`；
- 同一指标只存在一个实现；
- 可按策略、标的、市场状态和时间窗口切片。

---

## Phase FM3：实盘遥测与状态快照 v2

目标：把实盘引擎从“基础状态导出”升级为可观测系统。

### 工作项

| ID | 工作项 | 说明 |
| --- | --- | --- |
| FM3-01 | `live_status.json` schema v2 | 加入版本号、指标、告警、数据质量 |
| FM3-02 | 心跳和数据陈旧度 | 以 timeframe 归一化 |
| FM3-03 | 账户与组合指标 | Equity、Gross/Net、DD、Utilization |
| FM3-04 | 订单执行指标 | Fill、Reject、Cancel、IS、Latency |
| FM3-05 | 原子写入 | 临时文件写完后原子替换 |
| FM3-06 | 快照历史 | 追加式 JSONL 或轻量数据库 |

### 验收标准

- Dashboard 只读取稳定 schema，不读取内部 Python 对象；
- 状态写入失败不会影响交易主循环；
- JSON 中不存在 NaN/Infinity 等非法值；
- 重启后能继续计算需要状态的滚动指标；
- 指标计算延迟有预算和监控。

---

## Phase FM4：告警状态机与风险策略

目标：将指标异常转化为可控、可恢复、可审计的风险动作。

### 工作项

1. 实现 NORMAL/OBSERVE/WARNING/CRITICAL/HALT；
2. 支持连续确认和滞回；
3. 支持账户、策略、标的、数据源四种 scope；
4. 输出统一风险预算乘数；
5. 将风险预算注入 `RiskManager`；
6. Critical/Halt 状态持久化；
7. 人工解除 Halt；
8. 所有状态转换写入审计日志。

### 验收标准

- 指标在阈值附近不会反复抖动；
- 同一告警重启后不会丢失；
- 策略只能读取风险预算结果，不能自行绕过；
- HALT 不会自动恢复；
- 故障注入测试覆盖数据中断、账户不同步和拒单风暴。

---

## Phase FM5：策略漂移与组合风险

目标：识别策略逐步失效和表面分散、实际集中的组合风险。

### 工作项

| ID | 工作项 | 公式/能力 |
| --- | --- | --- |
| FM5-01 | 策略收益漂移 | CUSUM、Page-Hinkley |
| FM5-02 | 特征分布漂移 | PSI、JS |
| FM5-03 | 信号质量 | IC、ICIR |
| FM5-04 | 相关性风险 | 滚动协方差、平均相关性 |
| FM5-05 | 风险贡献 | MRC、RC、有效独立头寸 |
| FM5-06 | 压力测试 | 价格、相关性、流动性、成本场景 |

### 验收标准

- 漂移检测只使用当时可见数据；
- 参考分布版本化并记录训练区间；
- 组合风险对缺失币种和新上市币种有明确处理；
- 告警能解释是收益、特征、执行还是市场结构发生漂移；
- 不因单个小窗口异常永久关闭策略。

---

## Phase FM6：Dashboard

目标：提供观察、诊断和审计界面，不在第一版中直接提供高风险交易按钮。

### 页面规划

1. **总览**
   - Equity、PnL、Drawdown、Gross/Net、CVaR；
   - 当前最高告警；
   - 数据与系统心跳。
2. **策略健康**
   - Rolling Sharpe/Sortino；
   - Expectancy/PF/SQN；
   - CUSUM/PH；
   - 策略状态与风险预算。
3. **市场状态**
   - ATR%、EWMA、VolRatio、VoV；
   - Breadth、Dispersion、Correlation；
   - Regime 时间线。
4. **执行质量**
   - IS、滑点、参与率；
   - Fill/Reject/Cancel；
   - p50/p95/p99 延迟。
5. **数据质量**
   - Staleness、Missing、Duplicate；
   - Robust Z 异常；
   - 数据源错误历史。
6. **审计**
   - Signal -> RiskDecision -> Order -> Fill -> Position；
   - 告警触发、恢复和人工处理。

### 验收标准

- Dashboard 不承担核心指标计算；
- 所有页面能区分数据不足、正常零值和系统故障；
- 告警可追溯；
- 页面刷新不会阻塞交易引擎；
- 默认只读。

---

## Phase FM7：衍生品与微观结构扩展

目标：在基础监控稳定后增加币圈专项数据。

### 数据层待开发

- funding rate history；
- mark/index price；
- open interest；
- spot/perpetual 双市场行情；
- borrow rate；
- liquidation feed；
- trades；
- order book snapshot/delta。

### 账户与成本层待开发

- funding cash flow；
- borrow cost；
- realized/unrealized PnL 分离；
- initial/maintenance margin；
- cross/isolated margin；
- exchange-specific liquidation rules；
- hedge leg 与原子执行状态。

### 公式开发顺序

```text
Funding / Mark Deviation / OI
  -> Basis / Carry Attribution
  -> Spot-Perp Hedge Monitoring
  -> Liquidation Intensity
  -> Order Book Imbalance / Microprice
  -> VPIN / Kyle Lambda
```

---

## 10. 测试策略

### 10.1 公式单元测试

每个指标至少覆盖：

- 手工可验证的小样本；
- 空输入；
- 单点输入；
- 常数输入；
- 含 NaN；
- 含无穷值；
- 零价格/零权益；
- 极端收益；
- 窗口未预热；
- 不规则时间戳。

### 10.2 性质测试

示例：

- 所有正比例缩放后的价格，对数收益率不变；
- Drawdown 必须 \(\le0\)；
- Gross Exposure 必须 \(\ge|NetExposure|\)；
- CVaR 应不小于对应 VaR 的损失幅度；
- Turnover 不应为负；
- `N_eff` 应在 \([1,N]\) 内；
- 关闭费用后，Net PnL 应等于 Gross PnL；
- 相同输入在回测与回放模式下指标一致。

### 10.3 无未来数据测试

对任意时间 \(t\)：

\[
Metric_t=f(Data_{\le t})
\]

在 \(t\) 之后追加数据，不得改变 \(t\) 时刻已经生成的历史指标值，除非指标明确标记为事后修订型。

### 10.4 告警状态机测试

覆盖：

- 单次越界不触发；
- 连续越界触发；
- 滞回恢复；
- Warning 升级 Critical；
- Critical 重启恢复；
- Halt 必须人工解除；
- 多 scope 告警优先级；
- 多个风险乘数合并。

### 10.5 故障注入测试

- 行情停止更新；
- 时间戳倒退；
- 重复 bar；
- 账户同步失败；
- 下单超时；
- 拒单率暴增；
- 滑点极端扩大；
- JSON 状态写入失败；
- 单标的价格缺失；
- 交易所返回非法数值。

---

## 11. 配置规划

建议在 `config/params.yaml` 新增：

```yaml
metrics:
  annualization:
    mode: infer
    calendar_days: 365.25
  rolling_windows:
    short_days: 30
    medium_days: 90
    long_days: 365
  min_samples:
    equity_returns: 30
    closed_trades: 20

monitoring:
  persistence:
    state_file: reports/live_status.json
    history_file: reports/live_metrics.jsonl
  confirmation:
    default_consecutive_breaches: 3
  portfolio:
    drawdown:
      warn: -0.08
      critical: -0.12
      halt: -0.18
      recover_warn: -0.06
    cvar_99:
      warn: 0.05
      critical: 0.08
  data:
    staleness_bars:
      warn: 1.5
      critical: 2.0
      halt: 3.0
  execution:
    reject_rate:
      warn: 0.02
      critical: 0.05
      halt: 0.20

risk_policy:
  multipliers:
    normal: 1.0
    observe: 1.0
    warning: 0.5
    critical: 0.0
    halt: 0.0
```

配置加载时必须验证：

- 比例范围；
- 阈值单调性；
- 恢复阈值方向；
- 窗口大于最小样本；
- timeframe 与数据频率兼容；
- Halt 阈值不能比 Warning 更宽松。

---

## 12. 数据保留与性能

### 12.1 采样层级

不是所有指标都需要每个 tick 保存。

| 指标类型 | 建议频率 |
| --- | --- |
| 心跳、延迟、订单状态 | 每 tick/事件 |
| 行情陈旧度 | 每 tick |
| 账户权益、暴露 | 每 tick 或每 bar |
| 策略绩效 | 每次成交后和每个已收盘 bar |
| 分布漂移 | 每日或足够样本后 |
| 风险贡献、压力测试 | 每个已收盘 bar或仓位变化后 |

### 12.2 计算缓存

- 滚动指标使用增量状态或限定窗口；
- 不在每个 tick 对全部历史重新计算；
- 指标缓存键包含 scope、metric、window、last_timestamp；
- 历史修订时显式失效缓存；
- 计算失败不能污染上一份有效快照。

### 12.3 数据保留

建议：

- `live_status.json`：只保留最新快照；
- `live_metrics.jsonl`：追加事件和指标；
- 日终压缩为 Parquet/CSV；
- 告警和订单审计长期保留；
- 高频原始订单簿采用独立存储，不写入普通 JSONL。

---

## 13. 风险与常见误区

### 13.1 指标越多不代表监控越好

多个高度相关指标会制造重复告警。例如 Sharpe、Sortino、Expectancy 和 Profit Factor 可能同时因同一轮亏损恶化。告警层应支持归因和去重。

### 13.2 阈值不能直接照搬经验数字

经验阈值仅用于初始模板。最终阈值需要：

- 历史分位数；
- 样本外数据；
- sandbox 运行；
- 故障演练；
- 交易频率和策略类型校准。

### 13.3 监控数据不能反向污染策略

如果策略开发时不断根据监控指标调参，会产生二次过拟合。观察指标进入交易决策前必须完成独立研究和样本外验证。

### 13.4 滚动指标存在窗口错觉

短窗口敏感但噪声大，长窗口稳定但反应慢。建议同一指标至少展示短、中、长期三个尺度，并明确样本数量。

### 13.5 回测执行质量不等于实盘执行质量

OHLCV 无法还原真实盘口、排队顺序和部分成交。回测中的 Implementation Shortfall 和 Impact 只能是模型估计，实盘成交数据才是校准依据。

### 13.6 相关性在危机时变化

历史低相关不能保证压力期分散。风险贡献必须配合相关性冲击场景，而不是只使用平稳期协方差。

### 13.7 自动 Halt 必须谨慎

数据异常可以安全地阻止新仓，但是否自动平仓需要独立判断。行情错误时自动市价平仓可能放大损失，因此：

- 数据不可信：默认禁止新仓、保留人工处置；
- 仓位无法确认：进入 Halt；
- 账户权益真实突破硬限制：按预先验证的紧急流程执行。

---

## 14. 建议的首个开发迭代

首个迭代只做“可验证、无外部数据依赖、不会改变交易行为”的内容：

### 范围

1. 实现 `core/metrics.py`：
   - simple/log returns；
   - annualization inference；
   - drawdown/current/max/duration；
   - Sharpe/Sortino/Calmar/UI；
   - VaR/CVaR；
   - Gross/Net Exposure；
   - Turnover。
2. 改造 `backtest/reporting.py` 消费统一公式；
3. 新增 `tests/test_metrics.py`；
4. 报告写入：
   - annualization factor；
   - current/max drawdown；
   - drawdown duration；
   - Sharpe、Sortino、Calmar、UI；
   - VaR、CVaR；
5. 暂不改变 RiskManager 和策略行为。

### 退出条件

- 所有公式测试通过；
- 回测报告不再固定使用 252；
- 旧报告与新报告的差异有书面解释；
- 日线和小时线报告口径正确；
- 不影响原有信号和成交序列；
- 新指标可被实盘状态快照复用。

---

## 15. 最终完成定义

本专项 Roadmap 完成时，系统应满足：

1. 所有核心公式具有唯一实现、明确单位和测试；
2. 回测、回放、模拟盘和实盘对相同事件产生相同指标；
3. 每个告警都有来源、阈值、确认次数、状态转换和恢复记录；
4. 风险动作不由单个指标函数直接执行；
5. 数据异常、策略失效、执行恶化和账户风险可以被区分；
6. Dashboard 只读取版本化快照；
7. 新增公式必须先注册元数据和测试，再进入报告或风控；
8. 衍生品和订单簿指标只有在对应原始数据可审计后才启用；
9. 任意关键风险事件可以从指标追溯到数据、订单、成交和仓位；
10. 在完成总体 Roadmap 的实盘安全门槛前，本系统仍仅用于研究、回测和 sandbox。

