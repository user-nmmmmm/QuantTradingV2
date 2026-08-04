# QuantTrading 当前系统整改 Roadmap

> 文档状态：Draft v1.0  
> 审计基线：2026-08-01  
> 适用范围：当前仓库中的回测、实盘、风控、策略路由、报告和运维模块  
> 最终目标：将当前研究原型升级为“回测可信、模拟盘安全、可恢复、可审计”的交易系统；达到全部生产门槛前，不使用真实资金。

---

## 1. 当前结论

当前系统已经具备 OHLCV 数据获取、市场状态识别、策略路由、基础风险限制、Next-Bar 回测、CCXT 下单和状态导出等基础能力，但交易安全链路尚未闭环。

当前最重要的问题不是继续增加策略或优化历史收益，而是解决以下四类基础问题：

1. **实盘幂等与恢复缺失**：同一根 K 线可能重复处理，程序重启后也可能重复执行旧信号。
2. **订单事实不可靠**：接口返回不等于成交，系统没有完整处理部分成交、未知状态和超时后的订单查询。
3. **组合风控不完整**：实盘逐标的估值，日内熔断没有真正接入交易循环。
4. **回测可信度不足**：关键时序测试失败，多标的时间轴和成交模型仍过度理想化。

截至审计日，自带 `unittest` 共运行 41 项，40 项通过，1 项失败：`tests/test_no_lookahead.py::test_execution_timing` 未产生预期交易。该失败不直接证明已经发生前视偏差，但意味着防前视时序的回归保护目前不可用。

---

## 2. 优先级与放行原则

| 优先级 | 定义 | 处理原则 |
| --- | --- | --- |
| P0 | 可能造成重复订单、错误方向、失效风控或未知持仓 | 阻止任何真实资金运行，必须首先修复 |
| P1 | 可能令回测结果失真、风险估计错误或实盘行为与回测分叉 | sandbox 长期运行前完成 |
| P2 | 影响审计、可观测性、维护性和研究质量 | 小额实盘前完成 |
| P3 | 策略扩展、机器学习、性能和体验优化 | 不得抢占 P0/P1 资源 |

放行必须逐级进行：

```text
单元测试全绿
  -> 固定数据回归测试
  -> 历史事件回放
  -> 交易所 sandbox 故障注入
  -> paper trading 连续运行
  -> 小额、单交易所、少标的灰度
```

任何一级不满足退出条件，都不得进入下一级。

---

## 3. 问题—文件—解决方法总表

| 状态 | ID | 优先级 | 问题 | 当前文件 | 主要修改/新增文件 | 核心解决方法 |
| --- | --- | --- | --- | --- | --- | --- |
| ✅ | LIVE-01 | P0 | 使用未收盘 K 线 | `live_trading/engine.py`, `core/data_fetcher.py` | 新增 `core/timeframes.py` | 按 timeframe 计算 close time，只向策略发布已收盘 bar |
| ✅ | LIVE-02 | P0 | 同一 bar 重复处理 | `live_trading/engine.py` | 新增 `core/state_store.py` | 持久化 `(symbol, timeframe, close_time)` 水位线，提交信号前原子去重 |
| ✅ | LIVE-03 | P0 | 重启后重复旧信号 | `live_trading/engine.py`, `run_live.py` | `core/state_store.py`, `core/domain.py` | 保存已处理 bar、信号、订单意图和熔断状态；启动时先恢复再交易 |
| ✅ | LIVE-04 | P0 | 逐标的组合估值 | `live_trading/engine.py`, `core/risk.py`, `core/portfolio.py` | 新增 `core/valuation.py` | 每次 tick 先构建全组合价格快照，再统一计算权益、暴露和集中度 |
| ✅ | RISK-01 | P0 | 实盘熔断未接入 | `live_trading/engine.py`, `core/risk.py` | `core/state_store.py`, `tests/test_live_risk.py` | 持久化每日基准权益；路由前检查；触发后撤单、禁止开仓并生成一次平仓意图 |
| ✅ | ORD-01 | P0 | 下单超时可能重复下单 | `core/live_broker.py` | 新增 `core/order_store.py`, `core/domain.py` | 使用确定性 client order id；超时先按 ID 查询，再决定是否重试 |
| ✅ | ORD-02 | P0 | 缺少订单状态机 | `core/live_broker.py` | 新增 `core/orders.py`, `core/order_store.py` | 明确定义 submitted/accepted/partial/filled/canceled/rejected/unknown |
| ✅ | ORD-03 | P0 | 合约平仓无 reduceOnly | `core/live_broker.py`, `router/router.py` | 新增 `core/exchange_rules.py` | 平仓意图显式标记 reduce-only，并限制数量不超过当前可平仓位 |
| ✅ | ORD-04 | P0 | 同步失败后继续交易 | `core/live_broker.py`, `live_trading/engine.py` | 新增健康状态类型 | `sync()` 返回结构化结果；失败或数据陈旧时进入 fail-closed，禁止新订单 |
| ⬜ | EXCH-01 | P1 | 未校验最小数量、名义金额和精度 | `core/live_broker.py` | `core/exchange_rules.py` | 启动时加载 markets；下单前统一 round/validate，拒绝不合法订单 |
| ✅ | BT-01 | P1 | 防前视测试失败 | `tests/test_no_lookahead.py`, `backtest/engine.py` | 拆分时序夹具 | 使用不依赖路由状态的最小夹具，明确断言 signal time 与 fill time |
| ✅ | BT-02 | P1 | 多标的时间轴取交集 | `backtest/engine.py` | 新增 `core/calendar.py` | 使用统一事件时间轴；缺 bar 标的只可估值，不可凭空成交 |
| ✅ | BT-03 | P1 | 无部分成交和订单过期 | `core/broker.py` | 扩展订单模型和测试 | 依据 bar volume 限制可成交量；保存 remaining qty、TIF 和撤单状态 |
| ⬜ | BT-04 | P1 | 成本模型不完整 | `core/broker.py`, `backtest/reporting.py`, `config/params.yaml` | 新增成本模型模块 | 分离手续费、滑点、冲击、funding 和借币成本并逐笔归因 |
| ⬜ | ARCH-01 | P1 | 回测/实盘处理链路分叉 | `backtest/engine.py`, `live_trading/engine.py` | 新增 `core/events.py`, `core/pipeline.py` | 两个引擎共享 signal→risk→order intent 流程，仅替换行情与执行适配器 |
| ⬜ | OBS-01 | P2 | 状态 JSON 非原子写入、字段不足 | `live_trading/engine.py` | 新增 `core/snapshot.py` | 临时文件写入后原子替换；加入状态版本、数据延迟、订单和熔断信息 |
| ⬜ | OPS-01 | P2 | 无告警、对账和守护 | `run_live.py`, `docs/deployment.md` | 新增 `live_trading/reconcile.py`, `live_trading/health.py` | 心跳、异常阈值、日终对账、优雅停机和启动前检查 |
| ⬜ | ENG-01 | P2 | 测试依赖和生成物管理不足 | `requirements*.txt`, `.gitignore` | 新增开发依赖文件 | 固定测试工具；忽略临时报告、日志和实时状态文件 |
| ⬜ | DOC-01 | P2 | 中文乱码和文档与实现偏差 | `README.md`, `docs/*.md`, 源码注释 | 统一 UTF-8 | 分批修复编码；增加文档验证清单，避免一次性覆盖用户改动 |
| ⬜ | RES-01 | P2 | 缺少严格样本外验证 | `analysis/optimize.py`, `research/*` | 新增 `research/walk_forward.py` | 时间序列切分、walk-forward、参数稳定性和成本敏感性分析 |
| ⬜ | ML-01 | P3 | `models/` 是空壳 | `models/*.py` | 删除、隔离或完整实现 | 在数据、标签、训练、验证和推理契约齐备前不接入交易主链路 |

---

### 3.1 P0 完成状态说明（2026-08-01）

P0 条目已按当前整改里程碑标记完成。P0 完成不等于允许直接使用真实资金；生产放行仍须满足完整测试全绿、故障注入、sandbox 回放、重启恢复验证和连续 paper trading。

### 3.2 P1 任务分析（2026-08-01）

| 顺序 | ID | 当前判断 | 主要风险 | 完成定义 |
| --- | --- | --- | --- | --- |
| 1 | BT-01 | 已完成；防前视回归已恢复 | 已消除：三类订单均强制下一真实 bar 成交 | 最小时序夹具与完整回归 50/50 通过 |
| 2 | BT-02 | 已完成；已改为真实 bar 事件并集 | 已消除：缺 bar 不再生成合成 OHLCV | 缺 bar 标的不路由、不成交，仅使用最近可信 close 估值 |
| 3 | BT-03 | 已完成；支持部分成交、共享成交量和 TIF | 已降低：成交量参与率仍需按研究场景校准 | remaining qty、共享预算、GTC/DAY/IOC/FOK 和过期测试通过 |
| 4 | BT-04 | 已有手续费、滑点和简单冲击，完整归因仍缺失 | 净收益可能被系统性高估 | 逐笔拆分 commission、spread、impact、funding、borrow，并标记未建模项 |
| 5 | EXCH-01 | 未完成 | 实盘订单可能因精度和最小金额被拒绝 | 加载 markets；统一 round/validate；提供结构化拒绝原因 |
| 6 | ARCH-01 | 未开始，依赖前述语义稳定 | 回测和实盘继续分叉 | 最后抽取共享 signal → risk → order intent 管线 |

#### P1 实施批次

1. 批次 A：BT-01，先恢复全绿时序基线。
2. 批次 B：BT-02 + BT-03，统一事件时间轴与成交生命周期。
3. 批次 C：BT-04 + EXCH-01，统一成本口径与交易所合法性规则。
4. 批次 D：ARCH-01，在稳定契约上合并回测与实盘管线。

#### P1 关键判断

- BT-01 必须最先处理，它是其他回测整改的回归安全网。
- BT-02 与 BT-03 应共同设计；时间轴决定订单在哪些事件上有资格成交。
- BT-04 必须区分“成本确实为零”和“数据不足、未建模”。
- EXCH-01 应复用统一订单意图校验接口，避免回测与实盘两套规则。
- ARCH-01 最后实施，避免把尚未稳定的时序和成交缺陷固化到共享管线。

#### P1 总体验收门槛

- 完整测试已达到 50/50（2026-08-01）。
- 增加多资产缺 bar、部分成交、订单过期、成本归因和交易所规则边界测试。
- 固定数据集连续运行三次，订单、成交、权益和费用逐项一致。
- 任意 fill 可追溯到 signal、order intent、order、bar event 和成本明细。
- 回测与历史事件回放产生一致的 signal 和 order intent 序列。

---
## 4. Phase 0：保护基线与明确范围

### 4.1 目标

在修改交易逻辑前建立可重复的工程基线，并保护当前工作区中已有的用户文件和历史结果。

### 4.2 涉及文件

- `.gitignore`
- `requirements.txt`
- `requirements.lock.txt`
- 建议新增 `requirements-dev.txt`
- `tests/`
- `docs/phase0_baseline.md`
- `docs/phase0_baseline_results.md`

### 4.3 实施方法

1. 记录 Python、依赖、操作系统、配置哈希和测试结果。
2. 将测试依赖与运行依赖分离，至少固定 `pytest` 或明确只使用 `unittest`。
3. 保存一套固定 seed、固定日期、固定标的的 synthetic 回测基线。
4. 将临时日志、`reports/live_status.json`、默认路由日志和新生成报告加入忽略规则；已跟踪历史报告先保留，后续单独决定是否迁移。
5. 不自动删除 `archive/`、历史报告、虚拟环境或未跟踪文档。

### 4.4 测试与退出条件

- 干净环境可以安装依赖并运行测试。
- 固定 synthetic 回测连续运行三次，交易数和主要指标完全一致。
- 测试结果为 41/41 或者在修复测试结构后形成新的、明确记录的总数。
- 工作区中的现有未提交文件没有被覆盖或删除。

---

## 5. Phase 1：K 线时间语义与幂等

### 5.1 问题分析

`LiveTradingEngine._tick()` 当前直接选择 DataFrame 最后一行。对于 CCXT，最后一根 OHLCV 可能仍在形成中；同一根 bar 还会在每次轮询中反复进入路由。若信号函数不是严格依赖当前仓位，或账户同步存在延迟，可能产生重复订单。

### 5.2 涉及文件

现有：

- `core/data_fetcher.py`
- `live_trading/engine.py`
- `run_live.py`
- `tests/test_p6_live.py`

建议新增：

- `core/timeframes.py`：解析 CCXT timeframe，计算 bar 的理论关闭时间。
- `core/state_store.py`：持久化引擎水位线和风险状态。
- `tests/test_live_bar_processing.py`：未收盘、重复、乱序和重启场景。

### 5.3 解决思路

定义唯一 bar key：

```text
bar_key = exchange + market_type + symbol + timeframe + close_time_utc
```

处理规则：

1. 所有内部时间转换为 timezone-aware UTC。
2. 只有 `now >= close_time + close_grace_period` 的 bar 才可发布。
3. 发布前检查持久化水位线；已经成功处理的 key 直接跳过。
4. 信号和订单意图持久化成功后，再推进水位线。
5. 若处理过程失败，不推进水位线；下次依靠信号 ID 和订单 ID 恢复，而不是盲目重复下单。

### 5.4 方法细节

- 日线关闭时间不能简单假设为本地午夜，应以交易所 OHLCV 时间戳和 timeframe 规则为准。
- 增加 2—10 秒可配置 grace period，避免刚收盘时交易所数据尚未最终落定。
- 近期 bar 可继续重复拉取用于数据修订，但只有新关闭且未处理的 bar 进入策略。
- 状态文件第一阶段可以使用 SQLite；它支持事务和唯一约束，比单一 JSON 更适合订单及水位线。

### 5.5 验收场景

- 同一根已收盘 K 线连续调用 `_tick()` 10 次，只产生一次信号处理记录。
- 最后一根未收盘 K 线价格反复变化，不产生策略订单。
- 进程在“信号已生成、订单未知”时终止，重启后先查询订单，不重复创建。
- 混合时区和夏令时输入不会改变 bar key。

---

## 6. Phase 2：订单状态机、幂等提交与交易所规则

### 6.1 问题分析

当前 `LiveBroker.submit_order()` 将 `create_order()` 的返回结果直接记为交易，但真实订单可能只是被接受、部分成交，甚至请求超时而交易所已经收到。合约平仓也没有启用 `reduceOnly`。

### 6.2 涉及文件

现有：

- `core/live_broker.py`
- `router/router.py`
- `strategies/base.py`
- `tests/test_p6_live.py`

建议新增：

- `core/domain.py`：`Signal`、`OrderIntent`、`OrderRecord`、`Fill`。
- `core/orders.py`：订单状态机及状态转换校验。
- `core/order_store.py`：订单、成交和幂等键持久化。
- `core/exchange_rules.py`：精度、步长、最小数量、最小名义金额和 reduce-only 适配。
- `tests/test_order_lifecycle.py`
- `tests/test_exchange_rules.py`

### 6.3 目标状态机

```text
CREATED -> SUBMITTING -> ACCEPTED -> PARTIALLY_FILLED -> FILLED
                     \-> REJECTED
ACCEPTED/PARTIAL -> CANCEL_PENDING -> CANCELED
SUBMITTING -> UNKNOWN -> 查询交易所 -> ACCEPTED/FILLED/REJECTED
```

状态只能沿允许的方向转换；任何逆序或无法解释的状态都进入人工检查队列。

### 6.4 幂等方法

确定性客户端订单 ID 示例：

```text
hash(strategy_id, symbol, timeframe, bar_close_time, action, intent_version)
```

提交顺序：

1. 在本地事务中写入 `OrderIntent`，唯一键冲突则读取已有订单。
2. 标记为 `SUBMITTING`。
3. 携带 client order id 调用交易所。
4. 正常返回后保存 exchange order id 和真实状态。
5. 超时或连接中断时标记为 `UNKNOWN`，按 client order id 或时间窗口查询。
6. 只有交易所明确不存在该订单，才允许有限次数重试。

### 6.5 交易所规则

每笔订单提交前必须执行：

- symbol 是否存在且市场状态允许交易；
- 数量按 amount precision 向安全方向取整；
- 价格按 price precision 处理；
- 满足 min amount、min cost 和合约张数限制；
- spot 禁止裸卖空；
- futures 平仓必须 `reduceOnly=True`，数量不得超过可平数量；
- 拒绝 NaN、负价格、陈旧价格或超出价格保护区间的订单。

### 6.6 验收场景

- 请求超时但交易所已创建订单：系统只保留一笔订单。
- 30% 部分成交后重启：恢复 remaining qty，不把请求数量当成交数量。
- 重复处理相同订单意图：不再次调用 `create_order()`。
- 合约平仓方向错误或数量超限：本地下单前拒绝。
- 精度和最小名义金额不合格：产生结构化拒绝原因。

---

## 7. Phase 3：全组合估值与实盘熔断

### 7.1 问题分析

实盘循环当前为每个 symbol 构造单元素价格字典。这样计算总权益、杠杆和集中度时可能遗漏其他持仓。同时实盘只重置熔断标志，没有使用每日起始权益调用熔断检查。

### 7.2 涉及文件

- `live_trading/engine.py`
- `core/portfolio.py`
- `core/risk.py`
- `core/live_broker.py`
- `config/params.yaml`
- 建议新增 `core/valuation.py`
- 建议新增 `tests/test_live_risk.py`

### 7.3 解决方法

将每次 tick 分成明确阶段：

```text
更新全部行情
  -> 验证价格新鲜度
  -> 同步账户、仓位和未结订单
  -> 构造不可变 PortfolioSnapshot
  -> 检查组合级熔断和限制
  -> 为所有标的生成信号
  -> 统一风险审批
  -> 提交订单意图
  -> 再同步并导出状态
```

`PortfolioSnapshot` 至少包含：cash、equity、gross exposure、net exposure、各标的市值、价格时间、账户同步时间、未完成订单风险占用。

### 7.4 熔断设计

- 每个交易日首次成功同步账户时保存 `daily_start_equity`。
- 熔断状态必须持久化，重启不得自动解除。
- 触发动作分级配置：
  - `BLOCK_ENTRY`：禁止新开仓；
  - `CANCEL_OPEN_ORDERS`：撤销可能增加风险的未完成订单；
  - `FLATTEN`：为现有持仓创建一次 reduce-only 平仓意图；
  - `HALT`：停止自动交易，等待人工确认。
- 平仓失败不得无限重试；必须回到订单状态机并告警。

### 7.5 验收场景

- 持有 BTC 与 ETH 时，任何一笔新订单的风险检查都使用两者共同估值。
- 某个持仓缺少新鲜价格时，系统 fail-closed，不允许增加风险。
- 熔断触发后重复 tick 不重复提交平仓意图。
- 重启后熔断仍有效，直到新的交易日策略或人工解锁条件满足。

---

## 8. Phase 4：可信回测与成交现实性

### 8.1 修复防前视测试

涉及：`tests/test_no_lookahead.py`、`backtest/engine.py`、`router/router.py`。

方法：

1. 将“测试是否产生交易”和“测试是否下一根 bar 成交”拆成两个断言明确的测试。
2. 测试夹具绕开非目标因素，例如状态路由、集中度限制或流动性限制。
3. 记录 `signal_time`、`submitted_time`、`first_eligible_fill_time` 和 `fill_time`。
4. 对 market、limit、stop 分别验证下一根 bar 规则和跳空规则。

### 8.2 多标的时间轴

当前交集时间轴会因为单标的缺失 bar 删除整个组合时间点。目标方法是事件时间轴：

- 全局时间轴采用所有有效事件的并集或显式交易日历；
- 某标的有新 bar 时才允许该标的产生信号和成交；
- 缺 bar 时可使用最近可信价格做估值，但必须标记 stale；
- 不得用前向填充出来的 OHLCV 判断触价或成交；
- 报告记录每个标的的数据覆盖率和不可交易时段。

涉及：`backtest/engine.py`、`core/data.py`、建议新增 `core/calendar.py` 与 `tests/test_multi_asset_timeline.py`。

### 8.3 部分成交与订单有效期

扩展 `core/broker.py`：

- Order 增加 `remaining_qty`、`time_in_force`、`expire_time`、`filled_qty`。
- 每根 bar 的最大成交量受 participation rate 限制。
- 同一 bar 多订单共享可用成交量，不能分别使用整根 bar volume。
- GTC 订单延续，DAY/IOC/FOK 按定义处理。
- 部分成交按实际数量更新现金、持仓、费用与平均价。

### 8.4 完整成本

建议将成本拆成独立字段：

```text
gross_pnl
- commission
- spread_slippage
- market_impact
- funding_fee
- borrow_cost
= net_pnl
```

修改 `core/broker.py`、`backtest/reporting.py`、`config/params.yaml`，并新增成本单元测试。数据不足时必须明确显示“未建模”，不能默认为零后仍声称结果真实。

### 8.5 退出条件

- 所有时序、跳空、缺 bar、部分成交和费用测试通过。
- 任意成交可追踪到 signal、order intent、order、fill 和费用。
- 固定数据集的结果可重复，且报告包含配置哈希和数据范围。
- 回测与事件回放产生相同的信号和订单意图序列。

---

## 9. Phase 5：统一回测与实盘处理管线

### 9.1 目标架构

```text
MarketDataAdapter
  -> BarClosedEvent
  -> StateMachine
  -> Strategy Signal
  -> Portfolio Snapshot
  -> Risk Decision
  -> OrderIntent
  -> ExecutionAdapter
  -> Order/Fill Event
  -> Portfolio + Audit + Monitoring
```

### 9.2 文件方案

建议新增：

- `core/events.py`：领域事件定义。
- `core/pipeline.py`：共享事件处理管线。
- `core/domain.py`：领域对象。
- `backtest/execution_adapter.py`：历史撮合适配器。
- `live_trading/execution_adapter.py`：CCXT 适配器。
- `research/replay.py`：历史数据按事件回放。

保留但瘦身：

- `backtest/engine.py`：负责历史时间推进和历史执行器。
- `live_trading/engine.py`：负责轮询、恢复和真实执行器。
- 策略只输出 Signal，不直接承担交易所规则或持久化职责。

### 9.3 实施顺序

不要一次性重写两个引擎。先把领域对象与订单存储接入现有实现，然后提取公共函数，最后替换成共享 pipeline。每一步都使用固定回测基线比较交易序列。

### 9.4 退出条件

- 同一份历史数据输入回测和 replay，两者的状态、信号、风险决策和订单意图完全一致。
- 新增策略无需修改引擎核心。
- 实盘和回测只在行情适配器与执行适配器上存在预期差异。

---

## 10. Phase 6：可观测性、对账与运维

### 10.1 状态快照 v2

替换当前简单 JSON 内容，至少包括：

- schema version、instance id、代码版本和配置哈希；
- 最后心跳、最后成功账户同步、最后处理 bar；
- 数据延迟、缺失价格和 stale 标记；
- cash、equity、gross/net exposure、positions；
- open/unknown/rejected orders 和最近 fills；
- circuit breaker、risk halt 和人工锁定状态；
- 最近异常及连续失败次数。

写入方法采用同目录临时文件、flush/fsync、原子 replace。SQLite 作为事实存储，JSON 只作为只读快照。

### 10.2 对账

建议新增 `live_trading/reconcile.py`：

1. 查询交易所余额、仓位、未结订单和近期成交。
2. 与本地订单和仓位逐项比较。
3. 差异分为可自动修复、需停止交易和需人工处理。
4. 未完成对账时不得新增风险。

### 10.3 告警

首批告警：

- 账户同步连续失败；
- 行情超时或 bar 延迟；
- unknown/rejected order；
- 本地与交易所仓位不一致；
- 熔断触发；
- 订单长时间未成交；
- 状态快照无法写入；
- 主循环异常退出。

告警必须去重和分级，避免每个轮询周期重复发送。

### 10.4 启动与停止

修改 `run_live.py`：

- 默认应要求显式选择 sandbox/live；真实环境增加二次保护参数。
- API key 不再建议通过命令行传入，优先使用环境变量或密钥管理器。
- 启动前执行 markets、权限、时钟、余额、仓位、未结订单和状态存储检查。
- SIGINT/SIGTERM 时停止接收新信号，保存状态后退出；是否撤单由配置决定。

### 10.5 退出条件

- 任意一个交易日可以完整回放行情、信号、风险决定、订单、成交和账户变化。
- 断网、限流、API 超时、进程终止和快照写入失败都有测试或演练记录。
- sandbox 连续运行至少 14 天，无重复订单、未知持仓或无法解释的对账差异。

---

## 11. Phase 7：策略研究与小额实盘门槛

### 11.1 研究方法

先冻结执行和风险模型，再评价策略：

1. 时间序列 train/validation/test 切分，禁止随机打乱。
2. 使用 expanding 或 rolling walk-forward。
3. 参数选择只看训练与验证集，最终测试集只使用一次。
4. 对手续费、滑点、延迟和流动性做敏感性分析。
5. 分市场状态、年份、标的和交易方向进行归因。
6. 比较买入持有、现金、简单趋势等基准。
7. 报告参数稳定区域，而不是只报告最佳参数点。

涉及文件：`analysis/optimize.py`、`backtest/reporting.py`、`research/*`；建议新增 `research/walk_forward.py` 和相应测试。

### 11.2 小额实盘硬门槛

全部满足后才可评估小额实盘：

- 全部自动化测试通过；
- 无 P0/P1 未解决问题；
- sandbox/paper trading 连续 2—4 周；
- 零重复订单，零无法解释的仓位差异；
- 所有 unknown order 最终完成对账；
- 最大数据延迟和订单错误率低于预设阈值；
- 策略在未参与调参的样本外数据中通过最低收益/风险门槛；
- 有人工紧急停止、每日风险上限和交易所 API 权限限制；
- 首次只允许单交易所、少量标的、低资金上限和低杠杆。

---

## 12. 建议的迭代与提交拆分

| 迭代 | 范围 | 建议提交拆分 | 预计工作量 |
| --- | --- | --- | ---: |
| I0 | 基线与失败测试 | 开发依赖；基线记录；修复防前视测试 | 2—4 天 |
| I1 | 已收盘 bar 与幂等 | timeframe 工具；bar 过滤；状态存储；重启测试 | 4—7 天 |
| I2 | 订单状态机 | 领域对象；订单存储；client id；超时查询 | 1—2 周 |
| I3 | 交易所规则 | precision/min cost/reduce-only；故障测试 | 4—7 天 |
| I4 | 组合风控 | 全价格快照；实盘熔断；陈旧数据 fail-closed | 4—7 天 |
| I5 | 回测可信度 | 并集时间轴；部分成交；成本；审计链 | 1—2 周 |
| I6 | 共享管线 | events/pipeline；backtest/replay/live 适配 | 1—2 周 |
| I7 | 运维与对账 | 状态 v2；告警；对账；启动/停止检查 | 1—2 周 |
| I8 | 研究验证 | walk-forward；敏感性；样本外报告 | 1—2 周 |

每个迭代遵循“小提交、先测试、再实现、最后回归”的原则。避免把编码清理、架构重写和交易逻辑改变塞入同一个提交。

---

## 13. 首个可执行 Sprint

建议第一个 Sprint 只处理以下内容：

### Sprint 1A：恢复测试基线

- 修复 `test_no_lookahead.py`，确保明确验证 T+1 bar 成交。
- 新增开发测试依赖。
- 固定一套 synthetic 基准输出。

### Sprint 1B：已收盘 bar

- 实现 timeframe 解析和 close time。
- 在 `LiveTradingEngine` 中过滤未收盘 bar。
- 增加未收盘日线、小时线和分钟线测试。

### Sprint 1C：bar 幂等

- 先以 SQLite 实现 `StateStore`。
- 保存 last processed bar 和 signal id。
- 验证重复 tick 与进程重启不重复生成订单意图。

### Sprint 1 完成定义

- 全部测试通过。
- 同一根 bar 轮询 100 次仅处理一次。
- 未收盘 bar 不进入策略。
- 重启不执行旧信号。
- 尚不改变现有策略参数，不以收益变化作为 Sprint 验收标准。

---

## 14. 明确暂缓事项

以下内容应暂缓，直到 P0/P1 完成：

- 新增机器学习预测；
- 大规模参数搜索；
- 增加更多交易所；
- 高频或订单簿策略；
- 自动提高杠杆或仓位上限；
- 用当前历史最优收益作为上线依据；
- 对 `models/` 空壳进行表面填充但不建立完整验证链路。

---

## 15. 最终完成定义

本 Roadmap 的完成不是“代码已经能运行”，而是同时满足：

1. **正确性**：时间语义、订单状态、持仓、权益和风险计算可证明正确。
2. **一致性**：回测、历史回放和实盘共享核心决策流程。
3. **可恢复性**：断网、超时和重启不会制造重复订单或未知持仓。
4. **可审计性**：每笔成交可追溯到行情、信号、风险决定和订单。
5. **可运维性**：异常会被检测、告警、限制风险并支持人工接管。
6. **研究可信度**：策略通过严格样本外和成本敏感性验证。

在这六项全部满足前，系统定位应保持为研究与模拟交易平台。
