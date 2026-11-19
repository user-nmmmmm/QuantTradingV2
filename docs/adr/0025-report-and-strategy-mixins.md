# 0025 报告生成与 Donchian 策略里剩余的 mixin（B7）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B7，[ADR 0018](0018-mixins-to-collaborators.md)，[ADR 0022](0022-mypy-hard-gate-list.md)

## 背景
ADR 0018 把四个宿主类的 13 个 mixin 改成了协作类，明确留下两处没做：`backtest/reporting/` 的 4 个报告 mixin（`ReportMetricsMixin`、`TradeReconstructionMixin`、`ReportWritersMixin`、`ReportChartsMixin`），以及 `strategies/trend_breakout.py` 的 3 个私有 mixin（`_PersistentHealthMixin`、`_ProtectiveStopMixin`、`_CandidateScoringMixin`）。实测这两处的性质不同：

- **报告 mixin 几乎没有状态。** `ReportGenerator` 自己只有 `output_dir`；`metrics` 完全不用 `self`，`trades` 只在内部互相调用，`text` 与 `charts` 只读 `output_dir`、`METRIC_NAMES` 和两个格式化钩子。其中 `TradeReconstructionMixin` 还被当作无状态方法包单独实例化（ML 管线、若干测试）。历史脚本（哈希固定，不能改）用 `ReportGenerator.__new__(ReportGenerator)` 创建对象再调用 `_reconstruct_closed_trades`、`_aggregate_round_trips` 等私有方法。
- **策略 mixin 是同一个策略家族的共享行为，状态都在策略实例上。** 两个具体策略（`TrendBreakoutStrategy`、`TrendBreakdownStrategy`，以及继承它们的 V2/V3）共用三组逻辑，它们读写 `self.health`、`self.health_stats`、`self.stop_policy`、`self.score_policy`、`self.context`、`self.entry_window`……，而引擎、补帧回放（`live_trading/catchup.py`）、报告会直接读这些属性。棘轮里 `strategies/trend_breakout.py` 的 13 项 mypy 发现（11 项 `attr-defined`、2 项 `misc`）都来自 mixin 看不到宿主属性。

## 决定
- **报告：改成协作类，沿用 ADR 0018 的做法。** `ReportMetrics`（无状态）、`TradeReconstruction`（只注入一个回调：宿主的 `_extended_trade_analytics`，单独使用时为 `None`，行为同原来的占位实现）、`ReportWriters(host)`、`ReportCharts(host)`。`ReportGenerator` 不再继承任何类，用 `cached_property` 惰性创建它们（所以 `__new__` 创建的对象继续可用），并为协作类的**每个**方法保留同签名的转发方法或 `staticmethod`，转发块由脚本从协作类的 AST 生成。`host` 上的格式化钩子仍由宿主解析，因此子类覆盖仍然生效。`research/ml_selection/pipeline.py` 与两个测试改用 `TradeReconstruction()`。
- **策略：合并成一个显式的基类 `DonchianTrendStrategy(Strategy)`，不做组合。** 三个 mixin 的方法按原顺序放进同一个类（三组方法名互不重叠，原来的 `super()` 调用只可能到达 `Strategy`，解析结果不变），在类上声明 `entry_window`、`exit_window`、`use_obv` 并把 `_raw_entry_signal` 声明为抽象方法。具体策略改为继承它。这样隐式的"混入"变成显式的单继承，mypy 的 13 项发现全部消失，行为与方法解析顺序不变。
- **不做的：状态所有权迁移。** 策略的状态是被外部直接读写的公共接口，把它搬进协作类属于 B6，和宿主类一起设计。

## 备选方案
- 策略也改成组合：要为约 25 个方法/属性写转发，并先决定 `health`、`context`、`trade_state` 归谁，这正是 B6 的内容；而这个家族本来就是 `Strategy` 插件层级，单继承更自然。
- 把三个策略 mixin 保留、只补类型声明：能消掉 mypy 发现，但"通过 `self` 隐式依赖具体策略"的结构还在。
- 报告也改成纯函数：转发层要保留方法名（历史脚本调用它们），收益不大，且 `text`/`charts` 需要宿主的 `output_dir` 与钩子。

## 后果
- 代码里不再有任何 `*Mixin` 类；mypy 存量 326 → 313，`strategies/trend_breakout.py` 零错误并进入硬门槛清单（128 → 129 个模块）。
- `ReportGenerator` 多了约 30 个转发方法；调用多一层转发，不影响结果（全量测试、回测基线与文本报告不变）。
- 协作类被缓存在报告对象的 `__dict__` 里；对 `ReportGenerator` 做浅拷贝会让副本共用指向原对象的协作类，需要复制时用 `deepcopy`（同 ADR 0018）。
- 策略基类的公共状态属性仍由策略实例持有；迁移它们是 B6。
