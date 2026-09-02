# analysis/ · dashboard/ · research/ · config/ 模块说明

这几个包体量较小、彼此独立，合并在一篇文档里说明。

## analysis/ — 离线分析工具

### `analysis/optimize.py`
参数网格搜索的批量驱动脚本，两种模式：

**全样本网格（默认）**：`run_grid_search(...)` 用 `core.data_fetcher.DataFetcher` 只拉一次数据，遍历 `ENTRY_WINDOWS=(20,30,50,100)` × `EXIT_WINDOWS=(5,10,15,20)`，每个组合实例化 `TrendBreakoutStrategy`/`TrendBreakdownStrategy` + 默认参数的 `RangeStrategy` / `VolatilityReversionStrategy`，跑 `BacktestEngine.run(...)` 并用 `ReportGenerator.generate(metrics_only=True)` 打分，按夏普排序存到 `reports/optimization_<timestamp>.csv`。

`--oos` 追加一份 `validate_parameter_candidates` 证据。**注意这是事后切分**：排序发生在全样本上，"测试"半段在选参时已经被看过。它现在会为每个候选算一个 bootstrap p 值喂给 BH 校正（此前传的是空列表，等于 16 组候选一个都没校正），产物里也写了这条 caveat，但口径上的弱点是无法靠 p 值补救的。

**Walk-forward（`--walk-forward`）**：调 `analysis/walk_forward.py`，每个窗口在自己的测试段之前选参。窗口几何由 `--wf-train / --wf-validation / --wf-test / --wf-purge` 控制。

`--jobs N` 用 `ProcessPoolExecutor` 并行跑全样本网格（各候选彼此独立）。worker 函数 `evaluate_one_candidate` 是模块级函数而非闭包——Windows 用 spawn 而不是 fork，可调用对象必须能按名字导入；它只回传标量与一条收益 Series，不回传引擎结果（那里面挂着整个事件流和订单簿）。结果按 `param_grid` 顺序重排，所以表格和 CSV 不受调度顺序影响；实测 `jobs=1` 与 `jobs=4` 输出逐位相同。

指标**没有**跨候选缓存：实测 5 年 3 标的下重算一次指标约 17ms，而一次 bar 循环约 3s（占 0.6%），为此把开关穿过两层抽象换不到可测量的收益，时间在循环里。

CLI（`python -m analysis.optimize`）：`--symbols`、`--days`、`--start`、`--end`、`--source {synthetic,yahoo,ccxt}`、`--capital`、`--oos`、`--jobs`、`--walk-forward` 及四个 `--wf-*`。`live_trading/` 不使用它。

### `analysis/walk_forward.py`
把 `analysis/research_validation.py` 的窗口几何真正绑到引擎重跑上——在此之前，`walk_forward_splits`、`benjamini_hochberg`、`deflated_sharpe_ratio` 全都只吃别人产出的收益序列，没有任何东西驱动回测。

`run_walk_forward(data_map, candidates, config)` 对每个 split、每个候选跑**两次**引擎：

1. `[train_start, validation_end)` —— 选参样本。返回的收益序列在 `validation_start` 处切开，train 与 validation 分别打分，**选参只用 validation 半段**（紧邻 purge 间隔、离测试段最近的那一半）；train 的最优者也一并报出，两者不一致（`selection_agrees=False`）就是数据不支持这个选择的信号。
2. `[test_start, test_end)` —— 选参从未触及。

`candidates` 的值必须是**零参工厂**而不是策略实例：策略带健康/冷却状态，复用实例会让上一个窗口的生命周期决定下一个窗口能不能开仓。

两条口径：`procedure` 把每个窗口胜出者的测试收益拼成一条序列（这是"跑这套选参规则实际能赚到的"，也是应该被引用的数字）；`candidates` 把**每个**候选在所有测试窗口的收益汇总，这样 `benjamini_hochberg` 才有 N 个假设可校正——N 个候选的搜索本来就做了 N 次检验。p 值用 `core.metrics.one_sided_bootstrap_p_value`（`(k+1)/(n+1)`，永远不会给出 0，否则任何 FDR 阈值都拦不住）。`deflated_sharpe_ratio` 的 `trials` 传候选数而不是 1。

三条必须知道的约定：每次运行都喂 `warmup_period` 根前置 bar 并把 warmup 设成同一个数，**路由从窗口第一根开始**；前置历史不够的窗口进 `skipped_windows` 而不是被缩短（缩短会让该窗口的指标与其他窗口不同口径）；每个窗口都从同一笔 `initial_capital` 起步，跨窗口不复利——复利会让总结果变成"第一个窗口的故事"。

> `analysis/plot_performance.py` 已删除：它画的权益曲线 + 回撤双联图，
> `backtest/reporting/render/charts.py` 产出的 `equity.png` 已完整覆盖（并额外含日收益与资金占用）。

## dashboard/ — 只读运维面板

`dashboard/__init__.py` 只有一行说明"只读操作面板消费者"。

### `dashboard/__main__.py`
只读 CLI 面板，展示实盘系统的运行状态，不产生任何交易副作用。
- `recent_alerts(path, limit)`：读取 JSONL 告警日志（有界 deque）。
- `load_dashboard(status_path, alerts_path, alert_limit=10)`：加载 `live_status.json`，校验 schema（必须是含布尔 `healthy` 字段的字典），返回归一化后的字典（权益、现金、持仓、健康原因、`operational_state`、近期告警）；状态文件缺失/格式错误时回退到失败关闭的 `_invalid_dashboard(...)`（`operational_state="RISK_HALTED"`，不提供任何财务数据）。
- `render_text(data)`：格式化为人类可读的文本报告。
- `main()` CLI 参数：`--status`（默认 `reports/live_status.json`）、`--alerts`（默认 `reports/live_alerts.jsonl`）、`--alert-limit`（10）、`--json`（输出 JSON 而非文本）；有效时退出码 0，无效时退出码 2。运行方式 `python -m dashboard`。

它间接连到 `live_trading/`：`live_status.json`/`live_alerts.jsonl` 是实盘运行期间由 `live_trading/engine.py` 和 `core/alerting.py` 写出的，面板只读取这些文件，从不直接触碰 `core/` 的交易逻辑。**设计上刻意失败关闭**：任何缺失/损坏的状态数据都视为不健康，而不是悄悄展示过期的"健康"状态。

## research/ — 研究/重放工具

### `research/replay.py`
用于离线重放已记录的交易所执行事件（成交/订单），不会重新提交任何实盘订单——适合离线重建组合状态或用于研究/测试。唯一函数 `replay_execution_events(adapter, events, *, apply_fills=True)`，直接委托给 `adapter.replay(events, apply_fills=apply_fills)`。包装 `live_trading.execution_adapter.RecordedExecutionAdapter`（可不带 broker/portfolio 构造，默认用空的 `Portfolio(0)`）和 `core.events.EventEnvelope`。没有 `__main__` 入口——设计为被导入使用（例如测试或 notebook），不是独立运行的脚本。是 `core/`（Portfolio、EventEnvelope）和 `live_trading/`（RecordedExecutionAdapter）之间的桥梁，用于确定性、无副作用的事件重放。

## config/ — 配置加载

### `config/config.py`
针对权威配置文件 `config/params.yaml` 的失败关闭式单例 YAML 加载器。`ConfigLoader`（通过 `__new__`/`_instance` 实现单例，除非显式传入 `config_path`，此时绕过单例缓存以便测试隔离）。构造时立即加载并校验 YAML；文件缺失、无法解析、或不是映射类型时抛 `ConfigLoadError`（`RuntimeError` 子类）——**没有静默兜底默认值**。加载时通过 `core.logger.get_logger` 记录关键参数（`commission_rate_taker/maker`、`max_leverage`、`max_drawdown_limit`、`routing`）。公开方法 `get(section, key=None)` 返回整个配置段或某个具体键的值（不存在则为 `None`）。模块在导入时就创建好现成的单例实例 `config = ConfigLoader()`。这是 `core/`、`backtest/`、`live_trading/` 共同依赖的基础设施，集中管理手续费率、最大杠杆/回撤限制等在回测和实盘风控中都会用到的参数。

## 顶层入口脚本

- **`main.py`**（仓库根目录）：实际的回测 CLI（没有单独的 `run_backtest.py`）。支持命令行参数模式（`--days`、`--start`、`--end`、`--capital`、`--symbols`、`--source {synthetic,yahoo,ccxt}`、`--seed`、`--slippage`、`--random_slip`）和不带参数时的交互式提问模式。流程：`core.data_fetcher.DataFetcher` 拉数/生成数据 → `core.data.DataHandler.generate_quality_report` 数据质量报告 → `backtest.engine.BacktestEngine.run(...)` → `backtest.reporting.ReportGenerator.generate(...)` → 写出带时间戳的 `reports/<时间戳>_<天数>d_<N>Syms_<收益率>pct/` 目录（equity.csv、指标、routing_log.csv、data_quality_report.json）。
- **`run_live.py`**（仓库根目录）：实盘/沙盒交易 CLI。参数：`--symbols`（默认 `BTC/USDT ETH/USDT`）、`--interval`（60 秒）、`--sandbox`/`--live`（互斥，默认 sandbox）、`--exchange`（binance）、`--market-type {spot,future,futures,swap,margin}`、`--base-currency`（USDT）、`--preflight-only`、`--preflight-report`。需要从环境变量读取交易所凭据，构建 `StartupSafetyPolicy`、`PersistentOrderSafetyGuard`、`SafeLiveBroker`（均来自 `core/`），`--live` 模式下会校验实盘交易权限，然后驱动 `live_trading.engine.LiveTradingEngine`（初始化 → 启动前检查报告写入 `reports/startup_preflight.json` → 主循环 `engine.run()`）。它是最终产出 `live_status.json`/`live_alerts.jsonl` 的模块，供 `dashboard/__main__.py` 消费。
