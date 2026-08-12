# 性能技术分析与问题清单（2026-08-12）

> 审查方式：对全部一方代码（`main.py`、`run_live.py`、`core/`、`backtest/`、`live_trading/`、`router/`、`strategies/`、`config/`、`dashboard/`、`analysis/`、`research/`）做全量阅读，聚焦两条热路径——回测引擎（每 bar × 每 symbol）与实盘主循环（每 tick）；关键条目已逐条对照源码复核行号与代码原文。`tests/`、`docs/`、`reports/`、`outputs/` 不在范围内。
>
> 结论一句话：**回测侧的瓶颈是逐 bar 的标签索引开销（`.loc`/`get_loc`/`.iloc` 链），实盘侧的瓶颈是每 tick 对全历史的重复归一化与指标全量重算，且多处状态无界增长——实盘进程会随运行时间持续变慢、内存持续上涨。** 另发现 1 个正确性 bug（附录 A）。

---

## 一、总体结论

### 1.1 热路径画像

| 路径 | 循环频率 | 当前主要开销 | 增长趋势 |
| --- | --- | --- | --- |
| 回测：`HistoricalMarketDataAdapter.stream` → `TradingRuntime.process/process_symbol` → Router → Strategy | 每 bar × 每 symbol（3864 bar × 23 symbol ≈ 8.9 万次起步） | 逐 bar `.loc[timestamp]` 构造 Series、`index.get_loc` 重复查找、`df[col].iloc[i]` 链式取值 | 随 bar×symbol 线性，常数项大 |
| 实盘：`LiveMarketDataAdapter.refresh/poll` → `closed_bars` → `DataHealthMonitor.assess` → `_export_state` | 每 tick（interval 秒）× 每 symbol | 全历史 3 次 `normalize_market_frame` 深拷贝 + 指标全量重算、逐元素时间戳解析、每 tick 多次 fsync | **超线性**：`data_map` 无界增长，每 tick 越来越慢 |
| 订单事件：`TradingEventPipeline._accept` / `Broker._set_status` | 每订单状态迁移 / 每成交 | canonical JSON 序列化 + uuid5 + Decimal 强转 | `_events`/`_by_id` 只增不减，回测长跑与实盘常驻均内存泄漏 |

### 1.2 修复优先级摘要

- **P1（立即修）**：实盘 `refresh()` 无界增长（P-01）、`closed_bars` 全帧扫描（P-02）、健康检查全历史解析（P-03）、每 tick fsync（P-04）。均为小改动、高收益，且直接影响实盘长期运行的稳定性。
- **P2（回测专项）**：逐 bar 标签索引（P-05 ~ P-07）、事件管道开销与内存（P-08）、router 日志缓冲（P-09）。需要把位置索引贯穿 `MarketDataSlice`/`process_symbol`，改动面较大，建议单独批次。
- **P3（顺手清理）**：权益重复计算（P-10）、时间戳重复包装（P-11）、对账查询重复（P-12）、优化器全量报告（P-13）、`iterrows`+`pop(0)`（P-14）、死代码（P-15）。

---

## 二、P1：实盘热循环（每 tick）

### P-01 `LiveMarketDataAdapter.refresh()`：无界增长 + 每 tick 全量重算【高】

**位置**：`core/market_data.py:95-115`

```python
current = normalize_market_frame(self.data_map.get(symbol, pd.DataFrame()))
combined = fetched if current.empty else pd.concat([current, fetched])
combined = normalize_market_frame(combined)
Indicators.calculate_all(combined)
self.data_map[symbol] = combined
```

**问题**：

1. 每 tick 每 symbol 调用 3 次 `normalize_market_frame`（`:98`、`:110`、`:112`），每次内部做 `df.copy()` + 掩码 `.copy()` + 去重 + 排序（`core/market_data.py:15-27`）；
2. `Indicators.calculate_all(combined)` 对**全部历史**重算所有指标，而指标计算本身是滚动窗口，只有尾部会变化；
3. `combined` 从不裁剪到 `lookback`——`fetch_ccxt` 每次拉 `limit=self.lookback` 根新数据拼上旧帧，`data_map` 随进程生命周期**无界增长**，每 tick 的拷贝、排序、指标计算成本随之上升。

**后果**：实盘进程越跑越慢、内存持续上涨；属于"跑得越久越接近不可用"的退化型缺陷。

**修复方向**：

- 合并后立即裁剪：`combined = combined.iloc[-self.lookback:]`（一行止血）；
- 中期：只合并增量 bar（按 watermark 过滤 `fetched` 后再 concat，避免对 `current` 重复 normalize）；
- 指标增量更新：只对尾部 `max(window_sizes)` 行重算并覆盖指标列，而非全帧。

**工作量**：止血半小时；增量合并 + 指标尾算约 1 天（需补指标列一致性测试）。

### P-02 `closed_bars()`：每 tick 全帧逐元素循环【高】

**位置**：`core/timeframes.py:45-46`（调用点 `live_trading/engine.py:350` 经 `LiveMarketDataAdapter.poll`，`core/market_data.py:122`）

```python
close_times = pd.DatetimeIndex([as_utc_timestamp(value) + delta for value in dataframe.index])
return dataframe.loc[close_times <= now_utc]
```

**问题**：对索引每个元素做 Python 级 `pd.Timestamp` 包装，再全帧布尔切片——每 tick × 每 symbol 一次 O(n)。而适配器边界已经过 `normalize_market_frame`（UTC-naive、已排序、去重），这里的逐元素转换纯属重复劳动；且只有尾部 1–2 根 bar 可能"新收盘"，只需检查尾部。

**修复方向**：向量化——`close_times = dataframe.index + delta`（前置一次 tz 断言）；进一步只取 `dataframe.index[-2:]` 判断是否新收盘。

**工作量**：半小时。

### P-03 `DataHealthMonitor.assess()`：每 tick 全历史解析与排序【高】

**位置**：`core/health.py:122-153`

**问题**：每 tick 每 symbol 执行：

1. `pd.to_datetime(frame.index, errors="coerce")`（`:122`）——索引在适配器边界已归一化，重复解析；
2. `pd.DatetimeIndex([as_utc_timestamp(value) for value in valid])`（`:128`）——又一次逐元素 Python 循环；
3. `index.sort_values().unique()`（`:151`）——对**全部历史**排序去重，只为取最后 `gap_lookback_bars` 根做缺口检查。

**修复方向**：直接复用已归一化的索引（tz 断言后 `frame.index` 即用）；`frame.index[-self.policy.gap_lookback_bars:]` 先切片再 diff，排序可整体删除。

**工作量**：半天（需补 health 测试断言现有 reason 不变）。

### P-04 每 tick 无条件磁盘写：SQLite × 2 + fsync JSON × 1【中高】

**位置**：`live_trading/engine.py:384-385`（`state_store.set` 熔断键）、`:441-482`（`_export_state` JSON dump + `os.fsync`）

**问题**：每 tick 至少 3 次同步磁盘写（其中一次带 fsync），无论值是否变化。在 interval 较短（分钟级）时构成固定 IO 底噪，且在崩溃语义上 fsync 每 tick 一次并无必要——状态文件的读者是运维/看板，秒级延迟可接受。

**修复方向**：熔断键仅在值变化时 `set()`；`_export_state` 按 N tick 或状态迁移节流，fsync 只在迁移时执行。

**工作量**：半天。

---

## 三、P2：回测热循环（每 bar × 每 symbol）

### P-05 逐 bar `.loc[timestamp]` 构造 Series【高】

**位置**：`core/market_data.py:57-70`（`HistoricalMarketDataAdapter.stream`）

```python
bars = {symbol: frame.loc[timestamp] for symbol, frame in self.data_map.items()
        if timestamp in frame.index}
```

**问题**：每个时间戳 × 每个 symbol 做一次标签索引成员判断 + `.loc` 取值并分配新 `Series`——这是回测全循环的主开销，百万级 bar×symbol 步数下常数项巨大。

**修复方向**：构建期预计算 `{symbol: {ts: positional_i}}`（或 `frame.index.get_indexer(timeline)`），流式产出时携带位置；进一步可将热列转 numpy 数组、bar 用轻量视图（如 `itertuples` 或自定义 record）替代 Series。

### P-06 `process_symbol` 重复 `index.get_loc`【高】

**位置**：`core/runtime.py:162`

**问题**：`df.index.get_loc(event.timestamp)` 对每 bar × 每 symbol 重复标签→位置查找，而该位置在 P-05 的流式产出阶段天然已知。

**修复方向**：与 P-05 联动——`MarketDataSlice` 增加 `positions: Mapping[str, int]`（或由 bar 视图自带位置），`process_symbol` 零查找。注意 `get_loc` 在重复索引下返回切片，当前 `isinstance(location, int)` 守卫（`:165`）依赖索引唯一性，重构时需保持该语义。

### P-07 策略/状态机内 `df[col].iloc[i]` 链式取值【高（聚合）】

**位置**：`strategies/trend_following.py:82-107,139-164`、`mean_reversion.py:80-103,123-148`、`trend_breakout.py:138-149`、`strategies/base.py:147,177,201`

**问题**：形如 `df[self.col_sma].iloc[i]`、`df["close"].iloc[i]` 的链式取值每 bar 出现 15–30 次（状态机 + router + 策略叠加）。每次 `df[col]` 重新解析列、`.iloc[i]` 约 1 µs，累计可观。

**修复方向**：每 bar 一次提升到局部变量，并用 `.iat[i]`（比 `df[col].iloc[i]` 快约 3–5 倍）；或将热列一次性转 numpy 数组按位置索引。属机械性改造，配合 P-05/P-06 的位置贯穿一并做。

### P-08 订单事件管道：序列化开销 + 内存只增不减【高】

**位置**：`core/events.py:542-543,576-742`；调用点 `core/broker.py:266-297,462-485`

**问题**：

1. 每次订单状态迁移/成交走 `publish` → `event_id_for(*identity)` → `canonical_json`（深度 `_normalize_value` + 排序 `json.dumps`）+ uuid5 + 全部 qty/price 的 Decimal 强转——订单密集的回撤中每单数毫秒；
2. `_accept` 将每个 envelope 追加进 `self._events` 与 `self._by_id`（`core/events.py:739-740`），**从不释放**——回测长跑与实盘常驻均为内存泄漏。

**修复方向**：无订阅者/无 event store 时跳过幂等 JSON 哈希（回测路径轻量模式）；`_events` 的保留改为按需（有读取方时保留）或以 `deque(maxlen=N)` 设上限。改动前需确认 `events()` 访问器（`:548`）的全部调用方。

### P-09 Router `log_buffer` 无界累积【中】

**位置**：`router/router.py:31`（定义）、`:139-150`（`_log_routing` 追加）

**问题**：每 bar × 每 symbol × 每个路由分支构造一个 dict 追加进 `log_buffer`，直到回测结束 `save_log()` 才落盘。3864 bar × 23 symbol 约 9 万 dict 起步，优化器（多轮回测）场景成倍放大。

**修复方向**：流式写 CSV（打开文件逐行写，或 1 万行一批 flush）；优化运行（`analysis/optimize.py`）加开关关闭路由日志。

**工作量**：半天。

---

## 四、P3：低优先级与顺手清理

| 编号 | 位置 | 问题 | 修复方向 |
| --- | --- | --- | --- |
| P-10 | `core/runtime.py:121,146,176` | 每事件两次 `get_total_value`；`process_symbol` 每 symbol 复制 `dict(self.last_prices)`（router 不修改它） | 权益在路由后算一次（熔断用路由前值）；价格 dict 传引用 |
| P-11 | `core/broker.py:325-331` | 每活动订单每 bar `pd.Timestamp(current_time).date()` 重复包装（`current_time` 已是 Timestamp） | 提交时预归一化并缓存 |
| P-12 | `live_trading/engine.py:289,299,427,468` + `core/live_broker.py:400-404` | `has_unresolved_unknown()` 每 tick 最多 4 次，每次一次 SQLite 扫描 | 每 tick 查一次复用结果 |
| P-13 | `analysis/optimize.py:94-99` | 网格搜索每组参数生成完整报告（CSV + dpi=300 四面板图），只为取 6 个指标 | `ReportGenerator` 增加 `metrics_only` 路径 |
| P-14 | `backtest/reporting.py:124,141,169` | FIFO 成交重建用 `iterrows()` + list `pop(0)`/`insert(0)`（O(n²) 最坏） | `collections.deque` + `itertuples()`；每报告一次，优先级低 |
| P-15 | `backtest/engine.py:56-63` | `_looks_daily_or_slower` 死代码（无调用方），内部本身有逐 symbol `diff()` | 删除 |

---

## 五、Problem List（汇总）

| 编号 | 优先级 | 位置 | 一句话描述 | 预估工作量 |
| --- | --- | --- | --- | --- |
| P-01 | **P1** | `core/market_data.py:95-115` | 实盘 `refresh()` 数据无界增长 + 每 tick 全量指标重算 + 3 次 normalize 深拷贝 | 止血 0.5h；根治 1d |
| P-02 | **P1** | `core/timeframes.py:45-46` | `closed_bars` 每 tick 逐元素循环 + 全帧切片，只需查尾部 | 0.5h |
| P-03 | **P1** | `core/health.py:122-153` | 健康检查每 tick 重复解析索引 + 全历史排序，只为看尾部 200 根 | 0.5d |
| P-04 | **P1** | `live_trading/engine.py:384-385,441-482` | 每 tick 无条件 2 次 SQLite 写 + 1 次 fsync JSON | 0.5d |
| P-05 | **P2** | `core/market_data.py:57-70` | 回测逐 bar `.loc[timestamp]` 构造 Series，主开销 | 与 P-06/P-07 合计 2–3d |
| P-06 | **P2** | `core/runtime.py:162` | 每 bar×symbol 重复 `index.get_loc` 标签查找 | 同上 |
| P-07 | **P2** | `strategies/*.py` 多处 | `df[col].iloc[i]` 链式取值每 bar 15–30 次 | 同上 |
| P-08 | **P2** | `core/events.py:542-543,576-742` | 事件管道 canonical JSON+uuid5 每迁移一次；`_events`/`_by_id` 内存只增不减 | 1d |
| P-09 | **P2** | `router/router.py:31,139-150` | `log_buffer` 无界累积至回测结束 | 0.5d |
| P-10 | P3 | `core/runtime.py:121,146,176` | 权益每事件算两次；价格 dict 每 symbol 复制 | 0.5h |
| P-11 | P3 | `core/broker.py:325-331` | 时间戳每订单每 bar 重复包装 | 0.5h |
| P-12 | P3 | `live_trading/engine.py:289,299,427,468` | 对账 SQLite 查询每 tick 最多 4 次 | 0.5h |
| P-13 | P3 | `analysis/optimize.py:94-99` | 优化器每组参数渲染完整报告与图 | 0.5d |
| P-14 | P3 | `backtest/reporting.py:124,141,169` | FIFO 重建 O(n²) 模式 | 0.5h |
| P-15 | P3 | `backtest/engine.py:56-63` | 死代码 `_looks_daily_or_slower` | 5min |

**建议批次**：第一批 P-01（止血行）+ P-02 + P-04 + P-10 ~ P-12 + P-15（全部小改动，一个 PR）；第二批 P-03 + P-01 根治（指标增量）+ P-08；第三批回测专项 P-05 ~ P-07 + P-09 + P-13（需以 `tests/test_backtest_regression.py` 基线做等价性验收）。

---

## 附录 A：审计中发现的正确性 bug（非性能项）

- `core/broker.py:409` 使用 `random.uniform(0, base_slip)`，但**该文件从未 `import random`**（全文仅 `:111`、`:129` 两处形参/属性赋值）。开启 `random_slip=True` 时首次撮合即抛 `NameError`。修复：文件头补 `import random`；建议补一条 `random_slip=True` 的撮合单测。

## 附录 B：审查中确认无问题的常见模式（排除项）

- 回测权益曲线累积：`backtest/engine.py:122-152` 先 list 后一次性转 DataFrame，是正确模式（P-09 应反向效仿——流式写出而非累积）；
- 实盘主循环休眠：`live_trading/engine.py:267-268` `time.sleep(self.interval)`，无忙等；
- `main.py`/`run_live.py` 入口无重复加载、无热路径 I/O。
