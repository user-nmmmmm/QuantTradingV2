# R 系列指标与产物契约（2026-09-20）

适用本次 SYS-03 / FIX-19–20 的当前代码，历史原始报告保持原有身份。本文件补充 [统一路线图](unified_roadmap.md)，不构成策略准入。

## 时间、状态和标准产物

`MetricsFormulaVersion=2.0`。权益输入必须为唯一、递增、无 NaT 的时间索引及有限数值；不再通过排序、去重或删除坏值修饰输入。naive 历史时间明确解释为 UTC，aware 时间转换为 UTC。只有严格等间隔才能自动推断年化尺度；缺 bar / 混频返回不可计算的 Sharpe，并说明需要显式 `periods_per_year`。调用者可在指标入口及 `ReportGenerator.generate()` 显式提供该尺度。

`MonthlyReturns` 保留每个自然月、观测数、完整覆盖标记及不可计算原因。缺月不会把相隔数月的端点伪装成一个月收益。已有月收益序列仍保留相邻有效月份的收益；首月没有上月端点时不产生虚构收益。

所有落盘报告 profile 均保留 `metrics.json`、`closed_trades.csv`、`reconciliation.json`、`execution_quality.json`。前者的 `metrics` 为兼容字段，`metric_results` 为五态适配；JSON 不允许 NaN/Infinity。`reconciliation.json` 验证 closed lot 分片 → position 往返 → 报告净 PnL；这不是全账户资本/外部转账桥接，后者缺输入时显式 `not_modeled`。

## 交易与执行统计

- 最大回撤事件表按回撤深度选 Top-5，并独立报告最长水下期、未恢复数。最长持续与最深回撤不是同一选取条件。
- 交易分布包括平均/极值/分位数/标准差、盈亏持有期、最长连续盈利/亏损及该段 PnL、Wilson 胜率区间；连续段使用提供的关闭事实顺序，同长取首段。百分比期望以原入场名义金额为分母，缺分母则 `not_modeled`。
- R 倍数仅使用原批准风险的分片份额；没有原批准风险时 `not_modeled`，不依据后来的权益补造。
- 执行质量按 `(account_id, client_order_id)` 聚合，成交按 `(account_id, fill_id)` 去重。冲突重复、超量成交、先成交后下单、订单累计成交大于成交事实总量均为 `invalid_input`，不会给出有效总体比率。
- 完全成交率 = 完全成交订单数 / 已观察订单数；部分成交率 = 期末成交量大于零但不足原请求量的订单数 / 已观察订单数。曾经部分成交但最终全成单独保留 `had_partial_fill`。拒绝、取消、过期按最后订单状态计数；unknown 单独可见，不视为拒绝或取消。
- 首次成交及完成耗时用 UTC 事件时间相对原 intent，单位秒。未完成订单不混入完成耗时；缺 intent 时间则明确无法建模。
- 执行价偏差（bps）= `sum(side_sign * (fill_price - reference_price) * qty) / sum(reference_price * qty) * 10000`；buy/cover 的 sign 为 +1，sell/short 为 -1。该字段仅覆盖已执行数量的价格差，不重复扣佣金；未成交机会成本使用下面独立的可选指标，缺参考价时为 `not_modeled`。费用及借币另按权威成交/融资事实报告。

## 本轮追加的可选事实输入

[ReportGenerator.generate()](../backtest/reporting/__init__.py) 已接入以下可选输入，并将结果写入标准报告；计算实现及正反例分别见 [attribution.py](../core/metrics/attribution.py)、[execution.py](../core/metrics/execution.py) 和 [test_metrics_fact_completion.py](../tests/test_metrics_fact_completion.py)。这些是事后诊断接口，不改变交易决策。

- `group_equity`、`group_cashflows`、`external_cashflows`：同一时钟、同一报告币种的完整分组估值及显式资金流。分组包括分配的现金、持仓和负债，必须覆盖全账户并与账户权益、外部资金流对账。当前只支持 `cashflow_timing="end_of_interval"`；各组贡献在同一个账户资金流中性回撤峰谷间链接，可加总为账户回撤。没有同步路径时不按已关闭 PnL 伪造贡献。
- `terminal_valuations` 与 `execution_as_of`：以 `(account_id, client_order_id)` 关联真实已终结订单的独立期末估值。估值须带价格、币对、报价币、实际时间、可得时间及来源记录，且在订单终态之后、报告截止前已可得。以剩余未成交数量计算相对原意图价格的有符号机会成本；订单未终结或覆盖不足不能产生完整总体值。
- `independent_quotes`：以 `(account_id, fill_id)` 关联独立 bid/ask 及来源、实际时间、可得时间。报价在成交时必须已可得，默认最大陈旧时间 `max_quote_age_seconds=1.0`。分别计算相对中间价和可执行侧报价的成交偏差；不含手续费，也不把观察到的价差解释为因果市场冲击。

上述来源字段及 `independent=True` 只是输入契约，不构成对调用方真实性的独立认证。未提供事实保留 `null + not_modeled`，覆盖不足或非法输入按对应状态拒绝有效总体值。真实分组路径、完整机会成本估值和独立报价采集尚未验收；实现和合成手算通过不代表这些真实事实已经取得。

## 基准身份与迁移

| benchmark_id | 政策 |
| --- | --- |
| `equal_weight_initial_close_buy_hold/v1` | 起点可见资产按首个 close 等权买入，不加入后来上市资产，不再平衡 |
| `equal_weight_event_rebalanced/v2` | 每事件按有实际 bar 的资产等权调整；先计算价格变动造成的实际权重漂移，再以含现金的单边换手计费 |
| `btc_eth_50_50_first_open_buy_hold_gross/2026-09-14` | BTC/ETH 各固定 50% 资金，在每个评价窗的首个可见日线 open 买入，不再平衡，上市前该袖套保留现金；无手续费的未融资参考 |

v2 动态基准修正了旧算法按“上次目标权重”计算换手而漏掉价格漂移的问题。手算：初始 1000、两资产 100/100、次日 110/90、费率 10bps；首次费用 1，次日漂移权重 55%/45%、单边换手 5%、费用 0.04995，期末 998.95005。冻结的 9/14 BTC/ETH 基准没有更换政策。

基准 metadata 写入曲线 attrs 并进入比较结果；无身份的外部/旧曲线明确 `identity_status=not_modeled`，不会自动认作某个冻结政策。报告时不得用新的通用基准重算并覆盖已封存研究结论。

## 验收边界

历史专项证据位于 `reports/roadmap_v3/SYS-03/20260920-r-series/`，后续事实输入与报告接线见[本轮完成记录](followup_completion_20260920.md)；旧回执的源码身份和当时未建模边界不改写。纯工程通过不代表 BM8 的独立研究成立。当前全账户真实资本桥接、缺原始输入的历史迁移、真实分组估值、独立报价/期末估值采集及未见研究仍分别待证据；因果市场冲击模型不由上述报价偏差指标替代。SYS-03 整体状态以任务登记表和逐项矩阵为准。
