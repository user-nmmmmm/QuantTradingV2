# P0：原始信号观察与门控诊断

本模块落实论文讨论中的数据与验证底座：先记录“哪些信号出现、为何没有交易、之后发生什么”。它不是新的交易策略，也不实现条件 EV 拟合、软状态模型、动态权重或仓位放大。

默认关闭；只在研究回测显式启用。没有修改生产路由、健康门控、组合风控、策略准入或实盘下单。共享运行内核提供观察接口，但本批不包含实盘观察日志持久化、断点恢复和实时部署。

## 已实现的契约

| 能力 | 当前行为 | 主要实现 |
| --- | --- | --- |
| 原始候选 | 四个已注册策略都有无副作用的 `raw_entry_signal`；观察不调用会推进健康状态的 `should_enter` | `strategies/` |
| 不可变事件 | 确定性候选 ID、策略源码/参数版本、配置与成本指纹；嵌套信号与上下文冻结，重复 symbol-bar 不重复计数 | `core/signal_observation_types.py` |
| 信号时点上下文 | 趋势效率、波动比、ATR%、ADX、动量、成交额、价差、市场状态、当时策略健康/账户风险、持仓/挂单；不拟合分桶 | `core/signal_observation.py` |
| 首次门控归因 | 原始候选在正式路由前收集；事后关联实际审计路径；未经过的后续门控一律未知，提交订单不等于成交 | `core/runtime.py` |
| 固定周期标签 | 默认 1/3/5/20 根；下一根连续 bar 开盘参考价至第 H 根收盘，记录双边执行成本、适用融资、MAE/MFE；到期前不输出已知收益 | `core/signal_outcomes.py` |
| 实际结果关联 | candidate → opening order → 部分成交 → FIFO 分段平仓；退出控制器、成本、已实现 R；期末零成本估值单列 | `core/signal_actuals.py` |
| Ghost 回放 | 独立 Broker；next-bar、部分成交、参与率、订单过期、借贷/资金费、占位阻断；独立轨道和决策组内共享有限本金两种模式 | `backtest/signal_ghost.py` |
| 可审计报告 | 原始事件 JSONL、17 张 CSV、中文诊断报告、版本与异常摘要；完整报告 profile 的 manifest 另行校验观察数据摘要 | `backtest/reporting/signal_observation.py` |

观察失败、未实现原始接口的第三方策略、无法解释的门控、实际开仓成交缺少候选关联、Ghost 执行错误，会令观察状态为 `incomplete`，不会被当成没有信号。正常的数据尾部删失不代表观察功能出错。

## 时间、成本与结果口径

- 输入 K 线时间戳标记开盘；`context.available_at` 为开盘加周期，即本根收盘。策略原始接口只能拿到当前及以前的行；私有缓存仅预计算已验证的向后滚动指标。输入带 `available_at` 时还检查其可用时间。
- 候选时点的 `estimated_round_trip_cost_bps` 只用已收盘信号 bar 估算执行成本，不预知之后的波动、成交量、资金费或借贷时长。
- 参考数量在信号时固定为 `reference_notional / signal_price`。下一根跳空可能改变实际参考名义金额，净 bp 用下一根开盘名义金额作分母。
- 固定周期结果是独立信号诊断：每个候选各自计算，可以重叠；不受账户占位约束。现货做空、参与率超限、借币缺失等记录为不可执行标志，不能当成可交易收益。非 market 信号不强行按市价标签处理。
- 缺 bar、必需资金费缺失、输入无效或数据尾部不足分别标记删失；未知收益为 `null`，绝不补零。只把 `status=matured` 的标签用于成熟结果汇总。
- 固定周期标签在第 H 根收盘到期。Ghost 则在第 H 根真实 bar 收盘决定退出，下一根真实 bar 开盘撮合；两者不是同一收益口径。
- Ghost 挂单和部分成交会占位，到期撤掉开仓剩余量，退出订单可继续部分成交。没有复制策略动态退出或保护性止损，也不是“关闭某个门控后的完整策略收益”。回放成本来自同一个 Broker，随机基础滑点使用确定性的配置费率，不消耗正式账户随机数。
- `isolated_track` 按决策组、策略、标的划分独立账户；`capital_constrained` 在决策组内共享本金，提交时限制已持仓加挂单预算不超过权益一倍。跳空后敞口、可成交性和保证金仍受 Broker 的账户规则约束；这不是持续再平衡的一倍杠杆组合。
- Ghost 回放异常会保留已发生的成交与融资事实，停止该研究账户；后续信号标为未知，不重置本金重新开始。保证金不足同样停止并标为未知，不虚构强平路径或继续计算资不抵债账户的收益。空仓期间不会跨交易累计借贷天数。模拟成交成本使用执行 bar 的区间和成交量，所以完成标签的可用时间保守地放在执行 bar 收盘。
- 实际 `realized_net_pnl_ex_carry` 扣双边手续费，滑点/冲击已体现在成交价中，不再重复扣。`realized_R` 为该净损益除以对应已退出数量的初始止损风险。账户融资单列，不把无法逐候选分摊的融资当成零。
- 所有门控表都是描述性结果：候选间相关、周期可能重叠、路由选择也并非随机。既不能把被挡信号的均值视为门控的因果效果，也不能把独立账户盈亏相加为组合收益。

## 使用

普通回测中增加 `--observe-signals` 即可。以下示例只读取本地缓存，不访问交易所：

```powershell
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --observe-signals --report-profile full --seed 20260918
```

配置中的 `signal_observation` 只控制研究测量：默认期限 `[1, 3, 5, 20]`、参考名义金额 `1000`、Ghost 持有期 `5`、每个研究账户初始本金 `10000`。这些不是正式订单预算。Python 调用可使用 `BacktestEngine(signal_observation={"enabled": True})`，结果在 `result["signal_observation"]`。

`full` profile 保留行情快照与 `run_manifest.json`，其复现命令同时校验正式结果和 P0 观察结果。`workbook` / `compact` 也导出观察文件，但不提供完整行情快照和复现 manifest。观察不完整时 CLI 返回非零退出码，不表示正式账户结果被改变。

## 验收与证据

专项测试覆盖：四个接口的无副作用、深层不可变、重复事件、时间截断与未来扰动、未成熟标签、缺 bar / 缺资金费、成本不重扣、next-bar 双腿成交、部分成交、占位、本金竞争、异常不重启、实际部分平仓与退出归因、随机滑点下正式结果等价、确定性研究摘要。

复现还保存输入标的顺序并保留初始资金的原始数值类型，避免快照键名排序或整数/浮点转换导致审计摘要发生非交易层面的变化；新增测试覆盖顺序恢复与非法顺序拒绝。

```powershell
.\.venv\Scripts\python.exe scripts/run_portable_tests.py tests/test_signal_observation.py tests/test_refactor_contracts.py -q
.\.venv\Scripts\python.exe scripts/validate_signal_observation.py
```

验收脚本对同一段本地行情、配置、初始本金和随机种子分别运行观察开/关，比较正式成交、权益、基准、会计/风控摘要、策略健康及分配结果；生成新的、不会覆盖旧证据的报告目录和行情快照。默认使用 2019–2020 年 BTC、ETH、BNB 日线，是历史工程验证，不是新样本外收益证据，也没有重新使用保留期调参。

本次已保存的验收包：`reports/p0_signal_observation_20260918_final/`。先读 `acceptance.json` 和 `gate_effectiveness.md`；逐笔查 `signal_candidates.csv`、`signal_outcomes.csv`、`signal_actual_closes.csv`、`signal_ghosts.csv`。完整性、原始策略覆盖与版本见 `signal_observation_summary.json`。

2026-09-18 的本地验收结果：

| 检查 | 结果 |
| --- | --- |
| 数据范围 | BTC/USDT、ETH/USDT、BNB/USDT，2019-01-01 至 2020-12-31，各 731 根日线 |
| 原始候选覆盖 | TrendBreakout 187；TrendBreakdown 42；RangeMeanReversion 10；VolatilityReversion 228；合计 467 |
| 首次门控分区 | 接受 12；路由/状态 310；已有持仓 111；路由冷静期 27；最低名义金额 2；预热 5 |
| 固定周期标签 | 成熟 1,826；尾部删失 42；合计为 467 × 4 |
| 正式账户等价 | 观察开/关的成交、权益、基准、会计/风控摘要、策略健康、分配结果完全一致 |
| 逐笔关联 | 无缺失原始候选的正式开仓成交；候选唯一性与决策分区检查通过 |
| 快照复现 | 正式结果与观察结果的摘要均完全一致，复现退出码 0 |
| 全量回归 | 777 passed、1 skipped，另 46 subtests passed；新增 P0 专项 29 个测试用例 |
| 静态检查 | 本批新增/修改的观察、回放、报告、验收代码通过 Ruff；改动通过 diff 空白检查 |

两种 Ghost 模式合计 380 条完成平仓记录、538 条占位阻断、6 条尾部未完成记录；这是不同模拟账户的记录数，不能解释为一个账户的交易数或收益。

```powershell
.\.venv\Scripts\python.exe main.py --replay-manifest reports/p0_signal_observation_20260918_final/run_manifest.json
```

验收通过只说明 P0 测量底座成立，不改变当前策略治理状态。后续 P1 的条件 EV 账本、有效样本量/收缩、保守研究下界和严格滚动验证已经独立实现，见 [P1 契约与证据](p1_signal_meta_layer.md)。上表和旧验收包保留 P0 完成时的历史数据；新增配置后旧 manifest 的配置摘要可能不再匹配，不应跳过校验，应使用 P1 的新联合验收包复现当前版本。
