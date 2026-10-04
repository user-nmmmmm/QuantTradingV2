# core/ 模块说明

`core/` 提供行情适配、状态识别、共享决策运行时、经纪商接口及具体实现、订单风控与运维基础设施。回测（`backtest/`）和实盘（`live_trading/`）共用 `EventProcessor`、路由、策略和部分领域类型；各自的调度、执行、持久化和安全流程仍由对应模式实现。部分具体适配器也位于 `core/`，不能把整个 `core/` 视为纯领域层。

本文档按子系统说明职责、主要接口和边界行为。调度与启动流程分别见 [回测模块](backtest.md) 和 [实盘模块](live_trading.md)；此处重点解释共享组件之间的契约。涉及时间版本的数据接口应连同证据来源一起阅读，开启 `strict` 或保存快照本身不代表已通过研究准入。

---

## 一、数据与行情

### `core/data.py` — 数据校验与质量报告
- `DataHandler.validate(df)`：把索引转成 `DatetimeIndex`，列名转小写，要求 `open/high/low/close/volume` 齐全，数值化失败的转 NaN。**注意：该方法是原地修改（mutate）并返回同一个 DataFrame**。
- `load_csv(path)`：从 CSV 加载。
- `resample_ohlcv(df, rule)`：重采样，`closed='right', label='right'`，即时间戳代表 K 线**收盘**时刻；重采样产生的空缺行会被直接丢弃（缺口变成"缺失的 bar"，不会被填补）。
- `analyze_quality(df, symbol)` / `generate_quality_report(...)`：检测重复时间戳、缺口（间隔 > 1.5 倍中位数间隔）、异常波动（单根 bar 涨跌幅 > 20%）。

### `core/data_fetcher.py` — 统一历史数据获取

`DataFetcher` 是数据获取的统一入口，支持三种来源，返回统一格式的小写列名 OHLCV DataFrame：

- `fetch_yahoo(symbol, start_date, end_date)`：Yahoo Finance。
- `fetch_ccxt(...)`：未指定交易所时按 `binance → okx → kraken → coinbase` 尝试，失败后可回退到 Yahoo 加密货币代码；显式指定 `exchange_id` 时只使用该交易所，不切换来源。历史下载默认最多尝试 3 次，间隔为 2s/4s；`max_retries=1` 供实盘快照使用。
- 未传日期区间时，`fetch_ccxt(...)` 只读取最新一页；指定区间时分页抓取。分页达到 10,000 根安全阈值且尚未满足结束条件，默认 `strict=True` 抛 `IncompleteDataError`，避免把截断数据当作完整历史；`strict=False` 仅返回带 `pagination_termination` 标记的数据。
- `fetch_ccxt_since(...)`：从明确的时间游标读取一页，用于实盘缺口补取；页长限制为 1–1,000 根。
- `generate_scenario(symbol, start, end)`：合成三段式行情（趋势上涨→震荡→趋势下跌），用于无网络依赖的回测。

**需要注意**：日期字符串按 `data_timezone`（默认 `Asia/Shanghai`）解释后再换算成 UTC 毫秒边界，是 `[start, end_next_day)` 半开区间，不是字面 UTC 日期。未显式传入 `proxy_url` 时只读取 `QUANT_PROXY_URL`；环境变量未设置就不配置代理，传 `proxy_url=None` 也会禁用代理。

`fetch_ccxt` 的 `strict` 检查下载是否被安全阈值截断；`temporal_policy.mode="strict"` 则控制历史可用性，二者是不同的约束。启用时间版本采集后，完整响应的接收时间只能形成 `local_receipt` 证据，不能据此推断交易所过去的发布时间。

### `core/market_data.py` — 行情适配器（回测/实盘共用协议）

把原始数据转换成 `core/runtime.py` 定义的统一时间线 `MarketDataSlice`：

- `normalize_market_frame(df)`：复制输入、剔除无效时间、重复时间保留末行并排序；带时区索引转换成 naive UTC，无时区索引沿用 UTC 约定。
- `HistoricalMarketDataAdapter(...)`：`alignment_mode="union"` 默认使用真实 bar 并集，也支持 `"intersection"`；不填造缺失 bar。常规模式预计算指标，以分块行号查找控制稀疏多标的时间线的额外内存。`stream(fast_bars=True)` 可使用行快照加速标量访问，不适用的列类型仍回退到 pandas 行对象。
- 历史 `temporal_policy.mode="strict"`：决策截止时间为 bar 开盘时间 + 周期 + `decision_delay_seconds`，每个时点重新选择可见的原始版本并重算指标。事件 `bars` 保留已实现的撮合/估值行情，`histories` 只包含当时可见历史；当前 bar 无证据时不生成对应 `positions`。共享运行时通过 `strategy_market_view(...)` 隔离这两种输入。
- `LiveMarketDataAdapter.refresh()`：按 `lookback` 合并滚动行情，原始缓存与策略派生列分开保存。原始值和公开视图均未变化时复用指标；历史值被修订时重建派生视图。`max_workers` 控制并发抓取数，缓存提交与指标计算仍由调用线程串行完成。
- 实盘刷新失败保留旧缓存，并在 `failed_symbols`/`refresh_metrics` 记录原因；提供商最新时间倒退记入 `regressed_symbols`。缓存仍在不代表新一轮抓取成功，健康检查必须消费这些状态。
- `poll(now)` 按进程内标的水位线返回新收盘 bar；水位线不持久化。当前 `LiveTradingEngine` 通过 `tick_orchestrator.py` 调度刷新，再用 `closed_bars(...)` 和 `StateStore` 租约组织处理；启用可选运行控制后，限时抓取由 `RuntimeControls` 接管。`.stream()` 不适用于实盘轮询，会抛 `RuntimeError`。

### `core/public_data_clients.py` / `core/request_budget.py` — 公共客户端与共享请求预算

- `PublicDataClientPool` 按工作线程、交易所和市场类型复用公共 CCXT 客户端，避免在线程间共享可变 Session/timeout；`close()` 负责释放 Session。
- `SharedRequestBudget` 使用同一路径的 SQLite 数据库，按交易所协调已接入进程的请求间隔和窗口权重。`cost` 来自 CCXT 端点权重，行情、研究和账户请求共同计入预算；`critical` 保留部分容量，但仍受总预算与间隔约束。
- `request_scope(deadline=..., priority=...)` 传播基于单调时钟的截止时间；嵌套作用域只能收紧截止时间。`install_exchange_budget(...)` 把限流等待和 SDK 请求超时纳入剩余预算，请求结束后恢复原 timeout。
- 默认共享文件为 `reports/request_budget.sqlite3`，可通过 `QUANT_REQUEST_BUDGET_DB` 指定。跨进程协调要求使用同一数据库路径；未接入的外部程序不会自动纳入统计。

### `core/market_read_batch.py` — 有截止时间的只读批次

`BoundedMarketReads.read(symbols, fetch, timeout_seconds=...)` 去重标的，在统一截止时间内返回成功值与失败原因。线程池限制同时抓取数；同一标的的旧任务仍在执行时返回 `fetch_in_progress`，不会再提交重复任务。

超时只结束本轮等待：排队任务可以取消，已运行的网络调用可能继续。晚到结果不会被下一轮采用，`fetch` 也不应修改运行时状态；调用方根据批次结果统一提交。该组件用于显式启用的 `live_trading/runtime_controls.py`，不能把它的截止时间保证套到普通 `LiveMarketDataAdapter.refresh()` 上。

### `core/data_versions.py` / `core/temporal_data.py` — 不可变数据版本与历史可用性

`DataVersionStore` 保存原始对象、快照清单和不可变修订身份。相同 `record_id/revision_id` 对应不同内容会报错；内容哈希证明使用了哪些字节，不证明这些字节当时已公开。`TemporalOHLCV` 在此基础上追加 OHLCV 修订批次，并通过 `as_of(...)` 返回可见的原始历史。

| 时间或字段 | 契约 |
| --- | --- |
| OHLCV 索引 / `event_time` | bar 开盘时间；完整 bar 不能在收盘前可用 |
| `available_at` | 有证据支持的可用时刻；需配套 `availability_evidence.kind/reference` |
| `observed_at` | 本地观察到这个版本的时刻 |
| `knowledge="local"` | 在 strict 读取中，`available_at` 和 `observed_at` 均不能晚于截止时间 |
| `knowledge="published"` | 在 strict 读取中，要求 `source_publication` 证据；本地可以晚于该截止时间才收集到记录 |

默认 `mode="retrospective"` 保留最终数据回放兼容行为，并标记历史可用性未获证明。`mode="strict"` 只采用符合截止时间和知识口径的版本；缺少可用性证据时由 `unknown="exclude"` 或 `"raise"` 决定跳过或拒绝。单独的 CSV `available_at` 列不构成完整证据。

`import_identity(...)` 只恢复冻结身份指向的版本批次，核对 OHLCV 与摘要，不能自动跟随磁盘中新增加的版本。`retract(...)` 用 `tombstone` 显式撤回记录；读取先选最新版本，再移除已撤回行，因此不会回退到旧值。数据源本轮没有返回某行不等同撤回。

`as_of(...)` 的 `temporal_source_versions` 属性保留带哈希的版本证据，包括撤回事实。指标计算期间暂时移走这份大映射，策略计算视图也不携带它，避免 pandas 切列时反复复制；原始事件保留证据，供标签观察器核对可用性。

### `core/temporal_labels.py` / `core/temporal_financing.py` — 版本化标签与融资证据

- `VersionedOutcomeTracker` 接收 strict 历史，核对源记录哈希、身份、可用时间和完整持有窗口，复用既有收益计算器生成固定名义金额诊断。`results` 提供各候选/持有期的最新结果，`revisions` 保留全部历史。
- 标签 `available_at` 不能早于候选可用时间、源版本可用时间（local 还包括观察时间）、窗口收盘和首次实际计算的观察时刻。缺失证据需等待补齐，`finish(...)` 不能用文件结束或墙钟时间创造可用性。
- 训练消费方先选择训练截止时间**之前**可用的最新标签版本，再检查 `training_eligible`。最新版本失效或被撤回时，不能跳回较旧的成熟结果。延迟候选可按可用时间顺延模拟入场，但当前 v1 不给予这类候选训练资格。
- `TemporalFinancing` 保存资金费、借款、条款/许可及完整性声明的修订；`financing_cost(...)` 核对持有窗口所需证据。永续合约需要版本化线性合约条款和完整结算事件；`spot_margin` 需要决策时已知的借款许可、连续计息区间及显式费用。
- 缺失或撤回的融资证据返回不完整状态，不能假定零成本；格式、哈希或身份冲突直接报错。资金费按结算事件计算，借款按固定本金、ACT/365F、非复利口径计算。这些输出属于独立模拟诊断，不是实际成交记录或账户债务账本。

### `core/indicators.py` — 指标库

`Indicators` 静态类，原地给 DataFrame 添加指标列：`SMA_10/30/120`、`ATR_14`、`BB_UPPER/MIDDLE/LOWER`、`ADX_14`。ATR/ADX 用 Wilder 平滑（`ewm(alpha=1/n)`），并**故意**把前 `n-1` 个值强制设为 NaN（尽管 `ewm` 本身会更早给出数值），避免用不可靠的早期值。

### `core/timeframes.py` — 时间周期工具

`timeframe_delta(tf)` 解析 `"1m/5m/1h/1d/1w"` 等固定周期；`as_utc_timestamp`/`as_utc_datetime` 统一转 UTC。`closed_bars(...)` 使用开盘时间语义，返回满足 **开盘时间 + 周期 + 非负宽限期 ≤ now** 的 bar，包含恰好到达截止边界的记录；已排序索引用二分查找截取。`observed_bars_after(...)` 按实际观察到的 bar 计数，避免滚动窗口行号移动导致策略冷却期无法结束。

---

## 二、状态机与运行时（回测/实盘共享的核心决策链路）

### `core/state.py` — 市场状态机
`MarketStateMachine` 把每根 bar 分类为 `MarketState` 枚举：`TREND_UP`（=`BULL_TREND`，值 1）、`TREND_DOWN`（=`BEAR_TREND`，值 2）、`SIDEWAYS`（=`RANGE`，值 3）、`NO_TRADE`（4）、`VOLATILE`（5）。**注意 TREND_UP/BULL_TREND 等是同一个值的别名，不是两个不同状态**。
- `calculate_states(df)`：基于快/慢 SMA 交叉结构 + ADX 强度阈值判断趋势方向；当 `ADX > 阈值` 且 `ATR/close > 阈值` 时覆盖为 `VOLATILE`。
- `_apply_stability_filter(...)`：去抖动——候选状态需连续 `stability_period` 根 bar 确认才真正切换。
- `get_state(df, i)` 会把结果缓存进 `df["market_state"]` 列（原地修改，副作用）。
- `align_state_to_lower_tf(...)`：把高周期状态前向填充对齐到低周期索引，避免未来函数。

### `core/runtime.py` — 事件处理核心（EventProcessor）
**这是整个系统里回测和实盘真正共用的枢纽**，串联行情、状态机、路由、组合、执行、风控。
- `MarketDataSlice`（frozen dataclass）：标准行情事件，包含 timestamp/bars/histories/positions/timeframe/source；历史适配器直接构建，实盘调度器也会构建。
- strict 事件的 `bars` 与 `histories` 用途不同：`EventProcessor` 先通过 `strategy_market_view(...)` 和 `strategy_market_prices(...)` 构造决策输入，防止撮合视图中的事后修订价格影响信号、风险额度或下单参数。缺少策略可见的持仓标记价时禁止新增风险。
- `MarketDataAdapter`（Protocol，`.stream()`）定义历史时间线接口；实盘适配器提供 `.poll(now)`，当前实盘调度器则用 `.refresh()` 和 `StateStore` 组织新 bar。`RuntimeExecutionAdapter`（`ExecutionPort` + `.on_market_data()`）定义共享决策链路所需的执行端接口。
- `EventProcessor` 构造时必须注入 `portfolio`、`execution`、`risk_manager`、`state_machine`、`router` 和 `allocator`；`warmup_period`、`initial_equity` 等参数可选。
  - `.process(event, execute_market_event=True, ...)`：按需先调用执行适配器的 `on_market_data`，更新价格与风控状态；逐标的先处理已有仓位，再收集新开仓候选，最后统一调用 `allocator.allocate(...)`。回测引擎会先自行执行撮合与保护单，再以 `execute_market_event=False` 调用此方法。
  - `._collect_symbol_candidate(...)`：按预热期、持仓、风险决策分开控制仓位管理和新增风险；调用路由器的 `process_position_management(...)` 与 `collect_entry_candidate(...)`，并保留旧 `collect_candidate(...)` 兼容路径。
  - `.process_symbol(event, symbol)`：单标的入口；收集候选后调用 allocator。实盘调度器会批量收集同一收盘时刻的候选，再统一分配。

### `core/clock.py` — 时间抽象
`Clock` 协议 + `SystemClock`（真实 UTC）+ `CallableClock`（包装一个返回时间的可调用对象），让实盘调度/健康检查逻辑可以在测试中做到确定性可控。

---

## 三、经纪商与执行

### `core/broker/` — 回测撮合引擎（"虚拟交易所"）
`Broker` 用后续 K 线撮合策略订单，模拟滑点、maker/taker 手续费、可选冲击成本，并更新 `Portfolio` 和成交记录。
- `Order`（dataclass）：`.accepted` 除非状态是 REJECTED/EXPIRED/CANCELED 都为真。
- `submit_order(...)`：构建 `OrderIntent`，通过 `ensure_opening_reservation` 预占风险额度，进入 `pending_orders` 队列。
- `submit_intent(intent)`：保留 intent 身份的规范入口（供路由/风控层调用）。
- `process_orders(current_bar)`：每根 bar 调用一次，按 OHLC 撮合 Market/Limit/Stop 订单，遵守按标的共享的成交量预算（`max_participation_rate`）和 `TimeInForce`（GTC/DAY/IOC/FOK）。
- `_execute_trade(...)`：施加滑点/冲击成本，把平仓数量夹在可用持仓范围内，更新组合，追加成交记录，发布 `FillEvent`。

**关键防未来函数机制**：bar *i* 提交的订单只有在 bar *i+1* 才有资格成交（`current_time <= order.timestamp` 会被跳过）。所有订单和成交都发布到共享的 `TradingEventPipeline` 上，供 `RiskReservationProjection` 消费。

### `core/live_broker/` — 实盘经纪商（CCXT）
`LiveBroker` 以 `OrderStore` 作为唯一真相源，设计目标是**进程崩溃重启后能安全恢复，不会重复下单**。
- `submit_intent(intent)`：核心写路径——先查是否已有记录（有则走 `reconcile_order` 重放）→ 检查健康评估是否允许新风险 → 校验 intent → 经 `ExchangeBoundary.prepare` 规范化 → 预占风险 → 在 `OrderStore` 落地 `SUBMITTING` 记录 → 调用 `ccxt.create_order`。
- `reconcile_order(client_order_id)`：幂等地重新拉取交易所订单状态，解决 `UNKNOWN`/不确定的结果。
- `recover_open_orders()`：启动时重放所有非终态的 `OrderStore` 记录。
- `sync()`：从 `fetch_balance`/`fetch_positions` 刷新 `portfolio.cash`/`positions`。

**关键行为**：`create_order` 被当作**非幂等**操作——任何不确定的失败（网络/超时/限流）都会被标记为 `OrderStatus.UNKNOWN`，而不是直接重试（重试可能导致重复下单），只能靠 `reconcile_order` 的幂等查询来解决。衍生品持仓同步（`_sync_derivatives_positions`）在交易所不支持查询持仓时**失败关闭**（抛异常），不会假设"空仓"。每个标的同时只允许一笔活跃的开仓订单。

### `core/live_broker/safe.py` — 带安全闸门的实盘经纪商
`SafeLiveBroker` 是 `LiveBroker` 的薄子类，强制所有实盘下单先过 `safety_guard`（如 `PersistentOrderSafetyGuard`）。构造函数不接受明文 API 凭据（必须通过父类的环境变量路径传入）。安全检查只在**全新** intent 时执行（`order_store.get(...) is None`）；幂等重放会跳过检查，避免重复占用当日风险预算。

### `core/execution_port.py` — 执行端协议
`ExecutionPort` Protocol（`submit_intent`/`submit_order`/`cancel_symbol_orders`/`has_active_open_order`/`pending_open_notional`），`Broker`/`LiveBroker`/`SafeLiveBroker` 都满足这个接口——这是策略/路由代码能在回测和实盘间无缝切换的结构化类型基础。

### `core/exchange/` — CCXT 边界隔离层
负责下单前的市场元数据、能力约束、订单校验与归一化、CCXT 请求映射，以及订单/持仓响应解析。`core/live_broker/` 仍负责交易所调用和账户同步，`core/data_fetcher.py` 仍负责行情抓取；这些职责没有全部迁入此包。

公共门面在 `core/exchange/__init__.py`，下列实现分别落在 `metadata.py`、`validation.py`、`normalization.py`、`ccxt_mapper.py`、`parsers.py`：
- `ExchangeCapabilities`：交易所能力标志（订单类型、TIF、reduce-only、对冲模式）。
- `MarketSpecification`：单标的约束（数量/价格步进、最小/最大数量/价格/名义价值、合约乘数），`market_type` 默认 `"spot"`；`metadata.py` 的 `DERIVATIVE_MARKET_TYPES` 包含 `future`、`futures`、`swap`、`perpetual`，不包含 `margin`。具体入口是否允许某种市场类型，还需经过各自的配置与启动校验。
- `MarketMetadataLoader`：线程安全的 TTL 缓存包装 `load_markets()`；运行期元数据发生变化时，`MetadataChangeHaltPolicy` 可以直接 HALT（失败关闭），需要操作员显式调用 `acknowledge_change()` 才能恢复。
- `OrderValidator.validate(...)` / `OrderNormalizer.normalize(...)`：前者校验数量/方向/订单类型/TIF/reduce-only/对冲模式及最小最大限制；后者把数量/价格向下取整到步进单位（`ROUND_DOWN`），取整后归零则报错。
- `CCXTRequestMapper.map(...)`：唯一把规范化 intent 转成 CCXT `create_order` 参数的地方。
- `OrderParser`/`PositionParser`：解析形形色色的 CCXT 报文为规范化的 `CanonicalOrder`/`CanonicalPosition`。
- `ExchangeBoundary.prepare(intent, reference_price)`：编排整个流程——校验 → 加载元数据 → 归一化 → 因取整可能越界所以再校验一次 → 映射成 CCXT 请求；是 `LiveBroker.submit_intent` 调用的中心入口。

---

## 四、订单、状态机与风控

### `core/orders.py` — 订单状态机与错误分类
- `normalize_exchange_status(payload)`：把原始 CCXT 报文映射到规范化 `OrderStatus`。
- `classify_order_exception(exc)`：按异常类型/信息启发式分类（超时/限流/鉴权/资金不足/网络等）。
- `is_ambiguous_error(code)`：`NETWORK/TIMEOUT/RATE_LIMIT/EXCHANGE_UNAVAILABLE/UNKNOWN` 返回真——这些**绝不能**触发盲目重发。
- `validate_transition(current, target)`：状态迁移合法性表。`TERMINAL_STATUSES = {FILLED, CANCELED, REJECTED, EXPIRED}`。**注意**：迁移表允许 `CANCELED`/`EXPIRED` → `PARTIALLY_FILLED`/`FILLED`——延迟成交可能与撤单/过期确认竞速，所以"终态"在迁移层面并非绝对不可变（虽然在对账等场景仍按终态处理）。

### `core/order_store.py` — 订单持久化（SQLite）
`OrderStore(path)` 是 `LiveBroker`/`SafeLiveBroker` 的持久化底座：
- `create_intent`/`create`：`INSERT OR IGNORE`，重复的 `client_order_id` 创建是空操作（幂等）。
- `transition(...)`：先经 `orders.validate_transition` 校验再更新。
- `add_fill(fill)`：按 `fill_id` 幂等插入 `fills` 表。
- `list_non_terminal()`：排除终态订单，供启动恢复使用。

生产路径（如 `reports/live_orders.db`）跨进程重启持久化；`_migrate_orders()` 支持就地增列做 schema 演进。

### `core/risk/` — 风控与仓位计算
`RiskManager(risk_per_trade, max_leverage, max_drawdown_limit, liquidity_limit_pct, max_pos_size_pct)`：
- `calculate_position_size(equity, entry_price, stop_loss_price)`：按风险比例算数量，`qty = equity*risk_per_trade / |entry-stop|`。
- `calculate_position_size_fixed_pct(equity, entry_price, pct=0.10)`：按名义资金比例算数量。
- `check_entry_risk(portfolio, symbol, qty, price, current_volume, current_prices, pending_open_notional, reservation_projection)`：检查流动性上限（≤单根 bar 成交量的 1%）、杠杆上限（含已预留/在途名义值）、单标的集中度上限。**若 `current_prices` 为 `None` 但已有持仓，直接失败关闭（拒绝），不会用过期均价估算敞口**。
- `approve_and_create_intent(...)`：在 `reservation_projection.transaction()` 事务内原子性地评估风险，通过则一起发布 `RiskDecision` + `RiskReservation` + 增强后的 `OrderIntent`。
- `check_circuit_breaker(current_equity, daily_start_equity)`：当日回撤超过 `max_drawdown_limit` 触发熔断；触发后所有仓位计算/入场检查返回 0/False，直到 `reset_daily_breaker()`。

### `core/risk/reservation.py` — 在途风险预占投影
`RiskReservationProjection` 订阅 `TradingEventPipeline`：收到 `RiskReservation` 就登记预占；收到 `FillEvent` 就扣减 `remaining_qty`；收到状态属于 `RELEASING_STATUSES`（CANCELED/REJECTED/EXPIRED/FILLED）的 `OrderEvent` 就释放预占。`pending_notional(current_prices)` 按标的汇总 `remaining_qty * max(参考价, 市价)`。**`OrderStatus.UNKNOWN` 故意不释放预占**——不确定的交易所状态必须继续占用风险额度，直到出现权威的终态事实，这与 `LiveBroker` 对 UNKNOWN 的处理直接呼应。

### `core/risk/persistent_guard.py` — 重启安全的实盘风险闸门
`PersistentOrderSafetyGuard(policy, path, clock)`：`assert_order_allowed(...)` 在以下情况抛 `SafetyConfigurationError`：一键停机开关激活、标的不在白名单、价格缺失或非正、单笔名义值超过 `max_order_notional`、或（仅买入/做空时）当日累计预留名义值 + 本笔超过 `max_daily_new_risk`。通过后原子性地累加当天的 SQLite 计数行。**该计数按 ISO 日期存 SQLite，跨进程重启依然生效**——这是它能真正限制"每日"风险的关键。

---

## 五、账本、组合与快照

### `core/portfolio.py` — 轻量组合账本
`Portfolio(initial_capital)`：`cash` + `positions: Dict[symbol, {"qty","avg_price"}]`（有符号数量，正=多头，负=空头）。`update_position(...)` 在开仓/加仓时按加权平均重算成本，减仓时均价不变，**如果一笔成交让仓位穿越零点（多翻空或空翻多），均价会重置为成交价**。`get_equity`/`get_total_exposure` 在价格缺失时回退用 `avg_price`（这一点和 `core/risk/`、`Broker` 的失败关闭策略不同，调用方需注意可能因此误算权益）。

### `core/valuation.py` — 组合快照
`build_portfolio_snapshot(portfolio, prices, price_times, synced_at)`：任何持仓缺少新鲜的正价格就抛 `ValueError`，否则计算权益/总敞口/净敞口，生成不可变的 `PortfolioSnapshot`。

### `research/audit/ledger.py` — 离线事件账本与对账研究
这是用于离线重放和对账研究的**平行**实现，全程用 `Decimal`，采用均价成本法处理跨零点成交。它未接入实盘或回测的下单路径；交易时的持仓与账户事实仍由 `core/portfolio.py` 的 `Portfolio` 和 `core/lots.py` 的 `LotBook` 维护。类名 `AuthoritativeLedger` 表示该离线事件流内部以持久化事件为依据，不表示它是实盘账户的事实源。详见 [`../authoritative_ledger.md`](../authoritative_ledger.md)。
- `AverageCostPositionReducer.reduce(...)`：处理同向加仓（加权均价）、部分平仓（均价不变）、穿越零点的平仓（剩余部分按成交价重新开仓），返回已实现盈亏增量。
- `FillLedger`/`FeeLedger`/`CashLedger`：按 `event_id` 去重的幂等记账。
- `PortfolioProjection(base_currency)`：`apply(event, sequence)` 要求序列号严格递增；`snapshot(...)` 把多币种余额/持仓折算成基准货币，若持仓缺少标记价格默认抛错（除非 `require_marks=False`）；`reconcile(external_cash, external_positions, ...)` 比较本地与外部（交易所）状态，标记 `ReconciliationDiscrepancy`（偏差超过 10 倍容差为 critical）。
- `AuthoritativeLedger`：把 `SQLiteEventStore` + `TradingEventPipeline` + `PortfolioProjection` 组装成离线研究门面。

### `core/domain.py` — 核心领域类型
- `OrderStatus`：注意 `SUBMITTED` 是 `SUBMITTING` 的别名，`OPEN` 是 `ACCEPTED` 的别名（同值不同名，方便可读性）。
- `OrderIntent`：规范化的下单指令；`.client_order_id` 是对 `identity` 字段（exchange/account/symbol/timeframe/bar_time/strategy_id/action/sequence）做 SHA-256 后取前 24 位十六进制、加 `qt_` 前缀——**这是全系统幂等重发设计的关键**，相同身份字段永远产生相同 ID。
- `RiskDecision`/`RiskReservation`/`OrderSubmissionResult`/`FillRecord`/`SyncResult`/`PortfolioSnapshot`：其余不可变数据类，多有 `__post_init__` 校验。

### `core/events/` — 事件溯源基础设施
- `EventEnvelope`：每个事件的统一外壳（event_id/correlation_id/causation_id/run_id/account_id/source/occurred_at/observed_at，时间字段必须带时区）。
- `EventCodec`：确定性 JSON 编解码，通过 `__qt_type__` 标签保留 Decimal/datetime/UUID/Enum/dataclass 类型。
- `stable_uuid5`/`event_id_for`/`correlation_id_for`/`causation_id_for`：确定性 UUID5 生成，保证相同逻辑事件永远拿到相同 ID（去重基石）。
- `TradingEventPipeline`：进程内同步事件总线。`publish(...)` 依据 `idempotency_key`（或完整内容）计算确定性 `event_id`；`_accept()` 遇到重复 `event_id` 且内容一致就静默返回已有事件，**内容不一致则直接报错**（"Idempotency conflict"）——这能在同一个 key 被误用于不同事实时立刻暴露 bug。`publish_approved_intent` 原子性地发布因果相连的 RiskDecision+RiskReservation+OrderIntent 三元组。

**这个模块是 `OrderStore`、`RiskReservationProjection`、`PortfolioProjection` 共同依赖的去重引擎。**

---

## 六、存储与持久化

### `core/state_store_v2.py` — 实盘 bar 处理租约
`StateStore`：事务性 SQLite 键值存储 + 带租约的 bar 处理认领机制，用于实盘"近似恰好一次"处理。`claim_bar(bar_key, now, lease_seconds=300)`：若无既有认领则插入"processing"状态；若已是"processed"则拒绝；若既有"processing"认领已超过租约时长则可重新认领（处理崩溃恢复）。`complete_bar`/`release_bar` 配套。线程安全（`RLock`）。

### `core/events/store.py` — 事件持久化
`SQLiteEventStore`：只追加、按 `event_id` 幂等去重的事件日志；若已存在事件的内容（除 `observed_at` 外）与新事件不同则抛 `ValueError`。用 SQLite 触发器（`events_no_update`/`events_no_delete`）在数据库层面强制"只追加"，防止直接 SQL 篡改。`InMemoryEventStore` 是 `:memory:` 变体，供测试用。

### `core/sqlite_backup.py` / `core/sqlite_utils.py` — 数据库运维
`open_durable_connection(path, busy_timeout_ms)`：共享的失败关闭式连接打开器——设置忙等超时、执行 `PRAGMA integrity_check`（不是 "ok" 就抛 `DatabaseIntegrityError`）、开启 WAL 模式。`SQLiteSnapshotManager`：基于 `sqlite3` 原生 `.backup()` 的滚动备份，`run_if_due` 控制备份间隔（默认 3600 秒），`restore_snapshot` 原子恢复（校验快照 → 备份当前文件到临时路径 → `os.replace` → 清理陈旧的 `-wal`/`-shm` → 再校验）。

---

## 七、可观测性、运维安全与验收

`core/operations/` 保存事故记录、只读状态快照和启动前检查的实现。
`core/incident_journal.py`、`core/status_snapshot.py`、`core/startup_preflight.py`
保留为旧导入路径的兼容入口；新代码可直接从 `core.operations` 的对应模块导入。

### `core/alerting.py` — 告警
`AlertSink` 协议 + 多种实现：`LoggingAlertSink`、`JsonlAlertSink`（本地持久追踪，fsync）、`WebhookAlertSink`、`TelegramAlertSink`（纯文本，不用 `parse_mode`，避免 Markdown 注入）。`CompositeAlertSink` 扇出并隔离各 sink 的失败。`HysteresisAlertSink` 用"稳定上下文哈希"（排除 timestamp、retry_attempts 等易变字段）对重复告警去重，按 trigger/suppress/ack 状态机运作，每 `summary_every`（默认 9）次发一次抑制汇总。`build_default_alert_sink(...)` 按环境变量自动组装 Logging+Jsonl+可选 Webhook（`LIVE_ALERT_WEBHOOK_URL`）+可选 Telegram（`TELEGRAM_BOT_TOKEN`/`CHAT_ID`），外层包一层 Hysteresis。

### `core/health.py` — 数据/同步健康监控
`DataHealthMonitor`：失败关闭式评估行情新鲜度、完整性，以及账户/订单同步的陈旧程度，决定是否允许实盘承担新风险。`DataHealthPolicy` 定义各类阈值（默认：行情最大滞后 1.5 倍周期、同步最大陈旧 2 分钟、缺口容差 1.5 倍周期等）。`.assess(...)` 逐标的检查缺失数据、未来时间戳、无已收盘 bar、时间戳倒退（有状态追踪）、近期窗口缺口、陈旧；再检查账户/订单同步的陈旧/缺失/未来时间戳。**任何非空的 reasons 列表都判定为不健康**。

### `core/operations/incident_journal.py` — 事故处置记录
`record_incident(...)` 追加操作员对 R7 熔断/告警事件的处置记录（已解决/已说明）到只追加的 JSONL 日志（fsync）。`event_key` 必须是 `"<timestamp>|<event>"` 格式，被 `r7_acceptance.py` 用来检查未解决的熔断。

### `core/operations/status_snapshot.py` — 只读实盘状态快照
`load_dashboard(...)` 读取 `live_status.json` 与告警记录，供 Dashboard 等展示层消费；状态文件缺失、损坏或结构无效时返回 `RISK_HALTED`，不返回未经验证的权益和持仓数值。`recent_alerts(...)` 从 JSONL 告警中读取最近的有效记录。

### `core/logger.py` — 日志
`configure_logging(level)`（读 `QUANT_LOG_LEVEL`）、`get_logger(name)` 首次使用自动配置。`SensitiveDataFilter` 在日志输出前脱敏 `EXCHANGE_API_KEY/SECRET/PASSWORD` 等环境变量值及 Authorization/api-key/secret/signature 相关模式。

### `core/metrics/` — 回测指标库
大型纯函数库：夏普比率、回撤、盈亏比、交易质量、敞口、信号漏斗、成本敏感性、归因、基准对比、滚动/分段收益、R 倍数/SQN、训练测试切分、滚动窗口、bootstrap 置信区间、蒙特卡洛交易序列重排、Benjamini-Hochberg FDR 校正等。多数函数返回带 `status` 字段（`ok`/`insufficient`/`undefined`）的字典而非直接抛错，便于小样本下优雅降级。`walk_forward_windows`/`train_test_split_returns` 严格按时间顺序切分，避免未来函数泄漏；`bootstrap_return_distribution`/`monte_carlo_trade_sequence` 用固定种子 42 保证可复现。

### `core/telegram_heartbeat.py` — 定期心跳报告
和 `alerting.py` 的事件触发型告警不同，这是**周期性**（由 cron 驱动）的"系统仍在运行"状态汇报。`build_heartbeat_message(dashboard)` 汇总状态/健康/权益/现金/持仓/告警；若状态文件本身无效会明确报告 `RISK_HALTED`。`send_heartbeat(...)` 通过 `core.status_snapshot.load_dashboard` 兼容入口加载数据，经 `alerting.send_telegram_message` 发送。

### `core/live_broker/retry.py` — 重试包装器
`with_retry(fn, max_attempts=3, base_delay=0.5, max_delay=8.0, retryable=is_ambiguous_error)`：带指数退避的有界重试，默认只重试"不确定"类错误（网络超时等），延迟为 `min(base_delay * 2**attempt, max_delay)`。

### `research/audit/reconciliation_job.py` — 日终对账
`EODReconciliationJob` 调用 `research/audit/ledger.py` 的 `PortfolioProjection.reconcile`，对比外部交易所现金/持仓，原子性地把 JSON 报告落盘到 `<output_dir>/<日期>_<账户>.json`（临时文件 + `os.replace`）。要求 `checked_at` 带时区。产出被 `r7_acceptance.py` 消费，要求整个 soak 期内每天零偏差。

### `core/operations/startup_preflight.py` — 启动前检查
`build_startup_report(policy, credentials, engine)` 生成不含密钥明文的 JSON"证据"报告（凭据是否存在、sandbox/live 模式、一键停机是否未激活、账户/订单同步基线、健康基线、熔断状态）。`write_startup_report(...)` 原子写入（临时文件 + `os.replace` + fsync）。

### `composition/factory.py` — 组件装配工厂
`build_strategy_registry()`（组装 TrendBreakout/TrendBreakdown/RangeMeanReversion 策略）、`build_risk_manager()`、`build_state_machine()`、`build_router(strategies, log_path, allow_short)`、`market_type_supports_shorts(market_type)`（future/futures/swap/margin 返回真）。**注意**：`build_router` 在 `allow_short=False` 时会把 `regime_map["TREND_DOWN"]` 强制改成 `"Cash"`，在路由层面彻底禁用做空策略，与策略对象本身是否存在无关。

### `core/live_safety.py` — 实盘安全闸门
`StartupSafetyPolicy.from_environment(...)` 从 `QUANT_ALLOWED_EXCHANGES/ACCOUNT_TYPES/SYMBOLS` 和 `QUANT_{SANDBOX,LIVE}_MAX_ORDER_NOTIONAL/MAX_DAILY_NEW_RISK` 读取配置（sandbox 有默认值 1000/5000；**live 模式没有默认值，必须显式配置**，否则大声报错而不是静默套用 sandbox 的宽松限制）。账户类型白名单默认只含 `"spot"`。`OrderSafetyGuard.assert_order_allowed(...)` 强制一键停机、标的白名单、正价格、单笔名义上限、按自身 clock 每日重置的新增风险预算。`verify_live_permissions(exchange, account_type)` 要求交易所暴露权限查询接口，并断言提现权限已关闭、交易权限已开启。

### `core/r7_acceptance.py` — R7 沙盒验收
`audit_r7(...)` 失败关闭式审计工具，验证退出 R7 沙盒 soak 测试所需的全部证据：区间内每天的对账报告零偏差、所有熔断/告警都已解决或说明（交叉核对 `incident_journal.py`）、必需的 P0/P1 任务（`G11`–`G16`）均已关闭。CLI 输出 JSON 报告，按 `ok` 决定退出码 0/2。

---

## 八、模块关系与核对入口

```
data_fetcher/data → market_data(适配器) → runtime.EventProcessor
                                                  │
                                    state.py（识别状态）
                                                  │
                                          router.Router
                                                  │
                                     strategies.Strategy.on_bar
                                            │            │
                                     risk.RiskManager   execution_port(Broker / LiveBroker)
                                            │                    │
                                risk/reservation 投影      orders/order_store/exchange_boundary
                                            │                    │
                                       events.TradingEventPipeline（去重与事件总线）
                                            │
                          portfolio/LotBook（交易持仓事实）

    research/audit/ledger.PortfolioProjection（独立的离线重放与对账研究）
```

`live_safety.py`、`risk/persistent_guard.py`、`health.py`、`alerting.py`、`operations/`、`core/account_reconciliation.py`、`r7_acceptance.py` 共同构成实盘运行的"安全护栏"，回测路径基本不涉及这些模块。`research/audit/reconciliation_job.py` 是离线日终证据工具，未接入实盘订单路径。

时间版本链路附着在行情与研究观察路径上：`DataVersionStore → TemporalOHLCV → strict histories → VersionedOutcomeTracker`；带融资成本的标签同时依赖 `TemporalFinancing`。撮合使用的已实现行情与策略当时可见历史分开保留，不能用最终回测结果替代训练截止时刻的证据选择。

维护这些契约时，可优先核对以下已有测试：

| 契约 | 测试入口 |
| --- | --- |
| 行情修订、指标缓存、并发抓取与失败保留 | [`test_live_market_performance.py`](../../tests/test_live_market_performance.py) |
| 稀疏时间线、行快照与额外内存 | [`test_market_data_memory.py`](../../tests/test_market_data_memory.py) |
| 截止时间、迟到请求隔离与跨进程请求预算 | [`test_runtime_roadmap_followup.py`](../../tests/test_runtime_roadmap_followup.py) |
| 决策可见历史、冻结身份和实际接收时间 | [`test_temporal_data_integration.py`](../../tests/test_temporal_data_integration.py) |
| 标签版本、晚到修订和训练截止时间 | [`test_temporal_labels.py`](../../tests/test_temporal_labels.py) |
| 显式撤回与禁止回退到旧版本 | [`test_temporal_retractions.py`](../../tests/test_temporal_retractions.py) |
| 资金费/借款证据完整性与修订重放 | [`test_temporal_financing.py`](../../tests/test_temporal_financing.py) |
