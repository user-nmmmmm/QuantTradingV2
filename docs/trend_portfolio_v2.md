# TrendPortfolioV2：多周期趋势与波动率仓位研究

本版本落实 2026-09-20 的策略升级要求。它是显式启用的 `isolated_research` 候选，默认运行配置、正式策略路由和实盘准入没有改变。旧版同名策略使用 ATR 归一化等权排序、允许下跌状态开多；本版本替换这些行为。旧报告应按当时保存的代码和参数解释，不能套用本页规则。

本轮已完成 128/128 次日线历史回测、291 项专项与回归测试及 10 项子测试；工程验证通过，策略研究验收未通过。固定 V2-C 两交易所均亏损；六标的 V2-D 小幅盈利，但窗口比例、去头部盈利、PF 置信下界和近期贡献不满足要求。完整验收报告保存在本地 `reports/trend_portfolio_v2_20260920/REPORT.md`，不随源码发布。随后使用相同冻结代码、数据与配置完成第二轮 128/128 次回放，3,072 个稳定产物逐字节一致，收益和验收结论均一致；复跑报告保存在本地 `reports/trend_portfolio_v2_rerun_20260920/REPORT.md`。工程完成和可复现性均不改变策略准入。

## 入场与状态倍率

日线趋势分数为：

```text
score = 0.50 * sign(close[t] / close[t-20] - 1)
      + 0.30 * sign(close[t] / close[t-60] - 1)
      + 0.20 * sign(close[t] / close[t-120] - 1)
```

只有 `score > 0.3` 才允许新增多头；同时保留收盘突破前 20 根 bar 高点、原 OBV 确认与流动性/成交约束。完整 120 根历史不可用或价格无效时不交易。分数转负用于已有多头退出；负分不会生成空头订单。

| 市场状态 | 新仓风险倍率 | 额外要求 |
| --- | ---: | --- |
| TREND_UP | 1.00 | 趋势分数及突破条件 |
| SIDEWAYS | 0.25 | 分数至少 0.6，收盘至少高于前通道高点 0.5 ATR |
| VOLATILE | 0.50 | 有效实现波动率并执行仓位缩放 |
| TREND_DOWN | 0.00 | 多头不新增风险 |
| NO_TRADE / 未知 | 0.00 | 不交易 |

震荡强突破的两个阈值是固定的研究定义，不根据收益寻优。倍率版本把正常状态路由到同一个策略，所以普通状态变化不触发“切换策略”冷却；零倍率负责拒绝下跌状态的新仓，已有持仓仍由所属策略管理。硬门槛对照保留状态限制。Router 的原有切换、异常状态、时间安全阀及持仓权限仍适用。

## 仓位与风险

以最近 20 个已完成的日线对数收益率样本标准差乘 `sqrt(365)` 计算年化实现波动率。日线默认目标年化波动率为 10%，基础资产权重在双标的组合为 1/2，在六标的组合为 1/6。

```text
weight_before_regime = min(25%, base_weight * max(score, 0) * 10% / realized_vol)
quantity = min(equity * weight_before_regime / price,
               equity * min(1%, configured_risk_per_trade) / stop_distance)
           * existing_portfolio_risk_multiplier
           * strategy_health_multiplier
           * market_state_multiplier
```

零或无效波动率拒绝开仓，不以任意极小分母放大仓位。仓位建议随后经过原单标的/账户/杠杆/最小订单/组合风险/回撤治理和成交后复核。该模型是入场时的风险目标，不是每日再平衡，也不保证最终组合实现波动率恰好为 10%；持仓漂移、跳空和成本会造成差异。

双标的 A/B/C 保持原组合风险配置，新的 1% 单标的初始风险上限本身使两个新仓合计不超过 2%。六标的 D 通过原风险治理器的可选父簇预算收紧风险：同日新开初始风险最多 2%，任一子簇及整个 `crypto_beta` 父簇的初始止损风险最多 3%。BTC/ETH 保留 `major` 子簇，其他币仍归入加密相关簇。父簇预算同时计入开放 lot 与未成交订单的剩余风险，避免把六个币当作独立风险。父簇预算默认关闭，不改变旧配置的行为。

日线是本轮主研究频率。4h 必须显式声明频率及对应年化系数，独立运行和验收，不能混入日线 cohort 或收益统计。

## 三层退出

默认 V2-C/D 使用信号收盘价减 2 ATR 的初始灾难止损，成交后由原保护单系统立即维护。追踪保护价为：

```text
max(previous_stop, initial_stop, highest_since_entry - 2.5 * ATR)
```

只允许上移；完成 bar 后更新的追踪价从后续 bar 生效。现有跳空成交、先走不利极值的盘中路径、滑点和成交后风险复核保持原样。分数严格小于零或收盘跌破此前 60 根 bar 低点时发出趋势退出。365 天时间退出保留为安全阀，并单独报告其净收益。现有止损距离上下限仍约束最终保护价，极端情况下实际距离可能被裁剪。

`exit_mode=baseline` 保留 10 根 Donchian 初始止损和退出；`atr` 使用上述三层退出；`hybrid` 允许 Donchian/ATR 混合初始止损。混合模式是可解释备选实现，不能在看到回测结果后追加搜索而不登记新实验。

## 固定对照与验收

| 版本 | 标的 | 趋势/状态 | 仓位 | 退出 |
| --- | --- | --- | --- | --- |
| V1 | BTC、ETH | 原 20/10 + OBV、硬门槛 | 原风险定仓 | 原退出 |
| V2-A | BTC、ETH | 多周期分数、硬门槛 | 波动率目标 | 基准退出 |
| V2-B | BTC、ETH | 多周期分数、风险倍率 | 波动率目标 | 基准退出 |
| V2-C | BTC、ETH | 同 B | 波动率目标 | 2 ATR / 2.5 ATR + 趋势退出 |
| V2-D | 六标的 | 同 C | 六份基础权重及父簇上限 | 同 C |

V1→A 同时包含分数、仓位模型和预登记的状态确认 3 / 冷却 0 设置变化；A→B 隔离状态处理；B→C 隔离退出；C→D 同时包含资产覆盖与已登记的更紧组合风险。报告不得把这些差值误称为每个单独变量的因果贡献。

账户模型、手续费/滑点公式、跳空、健康政策、风险治理机制均复用既有实现。成本实验仅按预先登记的 1/1.5/2 倍放大成本。状态邻域限定确认期 2、3、5 与冷却 0、2，固定主要候选为 3/0，不按收益挑赢家。

验收模块按每个完整主运行独立聚合 UTC 退出日 cohort，同日跨币/控制器平仓合并；不同交易所、重复回放和重叠滚动窗口不可合并来凑样本。它检查净收益、风险匹配买入持有超额、至少 30 个 cohort、滚动窗口至少 2/3 盈利、剔除最优 1/3/5/10 cohort、剔除时间退出、成本压力、参数平台、交易所一致性、2022 后及近期活动、PF 分块 bootstrap 95% 下界与 DSR。

历史试验总数缺失时，完整选择偏差检验必须标记为证据不足；本轮 DSR 只能作为本轮诊断。两倍成本的灾难阈值和参数平台定义随协议一起封存，不能结果出来后修改。退出日分组是较保守的独立事件代理，仍不构成独立性的数学证明。

## 运行与复现

既有两组本地比较接口仍保留。完整研究套件通过 `scripts/run_trend_portfolio_v2.py --suite` 执行，先 `--register-only` 保存研究协议、配置、代码与数据哈希，再使用相同参数 `--resume`。输出包括尝试登记、逐运行权益/成交/平仓/cohort、有效策略配置、状态与成本审计、统计验收和整体摘要。执行中断或失败会保留记录，恢复时校验已冻结身份。

示例（日线已校验的公共数据）：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_trend_portfolio_v2 --suite --data-dir reports/strategy_review_20260919/public_data_validated --venues binance okx --start 2022-01-01 --end 2026-09-18 --output-dir reports/trend_portfolio_v2_20260920 --register-only
.\.venv\Scripts\python.exe -m scripts.run_trend_portfolio_v2 --suite --data-dir reports/strategy_review_20260919/public_data_validated --venues binance okx --start 2022-01-01 --end 2026-09-18 --output-dir reports/trend_portfolio_v2_20260920 --resume
```

六币名单固定为 BTC、ETH、SOL、XRP、ADA、LTC，只使用当时实际存在的行情与完整预热历史，不向上市前回填。行情缓存起点不是交易所真实上市日；未完整重建所有退市币历史时，固定六币选择仍有幸存者/事后选择偏差，报告必须保留该限制。

## 证据边界

2020—2026 已查阅数据只用于描述性研究和验证。独立未见市场/时段或现在以后累积的 shadow/paper 数据需另行冻结策略、代码、数据边界、成本及成熟期。工程完成和历史正收益都不能自动变成实盘准入。此任务不启用机器学习、空头、震荡均值回归或波动率反转策略。

研究依据：[Volatility Managed Portfolios](https://www.nber.org/papers/w22208) 支持研究波动率缩放的动机，不能直接证明它适用于加密资产；[A Century of Evidence on Trend-Following Investing](https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing) 讨论跨市场趋势与相关性；[Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) 用于处理多次选择及非正态收益造成的统计膨胀。
