# 技术审查报告（2026-08-05）

> 审查方式：对照 `docs/current_project_analysis.md`（2026-08-02）与 `docs/2026_08_03_architecture_and_issues.md`（2026-08-03）的问题清单逐条复核当前代码；对实盘安全、架构一致性、工程质量三条线做全量代码阅读；在 `.venv` 中运行全部测试（**98 个通过，unittest，6.9s**）。
>
> 本报告取代前两份文档的"当前问题"章节，作为下一阶段的唯一问题基线。结论一句话：**G1/G2 重构方向正确、骨架已经立住，但仍不适合真实资金运行；本次新发现 3 个会导致实盘永久停机或猝死的缺陷，以及 1 个系统性美化回测收益的撮合 bug。**

---

## 一、总体结论

### 1.1 已修复并验证（相对于 2026-08-03 基线）

| 原问题 | 现状 | 证据 |
| --- | --- | --- |
| CLI 传递 API Key/Secret | 已修复，强制环境变量，缺失即退出 | `run_live.py`；`tests/test_g1_entrypoint_and_logging.py` |
| 下单前无持久化 | 已修复：`create_intent` 与 `mark_submission_attempted` 均在 `exchange.create_order` 之前落库 | `core/live_broker.py:113`、`:137-139` |
| 重复下单 | 已修复：同 `client_order_id` 重入走 `reconcile_order` 而非重复下单；`SafeLiveBroker` 跳过风险额二次占用 | `core/live_broker.py:114-119`；`core/safe_live_broker.py:67-78` |
| 订单状态机无约束 | 已修复：实盘侧有合法迁移表 + `validate_transition` 强校验 | `core/orders.py:8-33`、`:92-97` |
| `state_store.py` / `state_store_v2.py` 双实现 | 已解决：旧版移除至 `archive/legacy_state_store.py`，活代码仅引用 v2 | `live_trading/engine.py:13` |
| 回测/实盘无执行端口协议 | 已解决：`ExecutionPort` Protocol 被 Router/Strategy/双引擎真实使用，两侧 Broker 结构兼容 | `core/execution_port.py:8-32`；`router/router.py:5,40,163`；`strategies/base.py:7,110` |
| 配置文件缺失静默回退 | 文件级已 fail-closed（缺失/损坏/非 mapping 均 raise） | `config/config.py:44-54` |
| 研究脚本独立回测 | 已解决：`analysis/` 复用生产 `BacktestEngine`；`archive/` 完全隔离零引用 | `analysis/optimize.py:14,89-90` |
| 根目录遗留入口 | 已归档（`Trading_V1_Model.py`、`verify_*.py` 移入 `archive/` 且零引用） | `archive/README.md` |
| README/params.yaml 乱码 | 已清理 | — |
| `__pycache__`/pyc、live db 文件被 git 跟踪 | 已解决 | `.gitignore:21-28` |

### 1.2 仍不适合真实资金运行的理由（摘要）

- 存在两个**不可自愈的账本状态**（UNKNOWN 死局、僵尸 SUBMITTING），触发后要么永久停机且无人工恢复通道，要么永久空转对账（§2.1、§2.2）。
- 主循环 `_tick` 内大量调用**无任何兜底**，一个未预期异常即杀死进程，无 supervisor（§2.3）。
- 已写好的重试（`core/retry.py`）与告警（`core/alerting.py`）**完全没有接入活代码**（§2.4）。
- 回测撮合存在**限价单成交价可突破限价**的 bug，直接虚增回测收益（§3.3）。

---

## 二、P0：实盘阻断项（仍存在 + 新发现）

### 2.1 【新发现】UNKNOWN 死局：永久停机且无解除通道

**触发路径**：崩溃发生在 `mark_submission_attempted`（`core/live_broker.py:137`）与 `exchange.create_order`（`:139`）之间。此时订单意图已标记"已尝试提交"，但订单从未到达交易所。

**后续演化**：

1. 重启后 `recover_open_orders()` 捞出该记录（已 attempted，非终态），调用 `reconcile_order()`。
2. `_fetch_exchange_order` 查不到这张单，返回 `None`（`live_broker.py:209`）。
3. `reconcile_order` 将其置为 `UNKNOWN`（`:211-216`）。
4. 下一次对账再查仍是 `None`，而 `:211` 的 `if current is not OrderStatus.UNKNOWN` 守卫**阻止对已是 UNKNOWN 的记录做任何后续迁移**——该记录永远是 UNKNOWN。
5. `has_unresolved_unknown()`（`:255-259`）每 tick 返回 True → 引擎永久 HALT（`live_trading/engine.py:130-135`）。

**后果**：进程进入"活死"状态——不交易、不报错退出、无告警外发（§2.4），只能人工发现并手工改 SQLite。这与 G1/G2 建立的 fail-closed 原则自相矛盾：fail-closed 停机了，却没有对应的"人工确认—恢复"状态机。

**修复方向**：

- 对"submission_attempted 但交易所查无此单"的记录，在经过 N 次确认/超时后允许迁移到终态（如新增 `EXPIRED_UNSUBMITTED` 或复用 `REJECTED`），并在迁移时强制 `sync()` 校准仓位；
- 增加持久化的人工确认机制（HALT 状态落盘 + 恢复命令），而不是内存态 `_operational_state`（`live_trading/engine.py:57`）。

### 2.2 【新发现】僵尸 SUBMITTING：永不终态的对账负担

`recover_open_orders()`（`core/live_broker.py:223-231`）对"SUBMITTING 且未 attempted"的记录只做 `_result()` 返回（`:227-228`），不做任何迁移或重提交。这类记录（崩溃于 `create_intent` 与 `mark_submission_attempted` 之间）永久留在 `list_non_terminal()` 里，每 tick 被捞出一次。

- 单独看危害较低（不阻断交易，同信号重触发时 `submit_order:116-117` 会接管）；
- 但与 §2.1 叠加，非终态集合只增不减，每 tick 对账成本线性增长；若同一信号不再出现，该记录永远悬置。

**修复方向**：为 SUBMITTING 记录加创建时间 TTL，超时后迁移到终态并告警。

### 2.3 【新发现】`_tick` 无兜底：意外异常直接杀死进程

`run()` 只捕获 `KeyboardInterrupt`（`live_trading/engine.py:109-114`）。而 `_tick` 中以下调用都在 per-bar `try`（`:201-216`）**之外**：

- `self._recover_orders()`（`:129`）→ 交易所网络异常、SQLite 异常；
- `self._update_data()`（`:137`）→ `fetch_ccxt` 网络异常；
- `self.broker.sync()`（`:138`）→ 同上；
- `state_store.get/set/claim_bar` → SQLite 异常（`database is locked`、损坏等）。

任一抛出未预期异常 → 进程直接退出，无 supervisor、无重启策略、无告警。整改前的问题是"捕获一切继续轮询"，整改后矫枉过正变成"一碰就死"。无人值守场景下二者都不可接受，但后者更难被监控发现（进程消失 vs 日志刷屏）。

**修复方向**：`_tick` 外层加兜底 `except Exception`：记录 traceback、经 `AlertSink` 告警、进入指数退避后继续（区分"致命配置错误"与"瞬时外部故障"），并配套进程级 supervisor（systemd/重启策略）。

### 2.4 重试与告警是死代码

- `core/retry.py:16-49` 实现了带分类的指数退避 `with_retry`，但全仓 grep 确认**仅 `tests/test_retry.py` 引用**，`LiveBroker.submit_order`/`reconcile_order`/`sync` 均未使用。提交订单遇到瞬时网络错误（`is_ambiguous_error` 覆盖的 TIMEOUT/NETWORK/RATE_LIMIT/EXCHANGE_UNAVAILABLE）直接置 UNKNOWN，配合 fail-closed 立即停机——**过敏触发**：一次网络抖动即可停机。
- `core/alerting.py`（27 行，`AlertSink` Protocol + `LoggingAlertSink`）**全仓零引用**。停机、critical 事件只有本地日志，无 webhook/邮件/IM 外发通道。
- 无应用层限流（仅 ccxt `enableRateLimit=True`，`live_broker.py:70`）；无连续 API 失败计数熔断（只有日权益熔断 `engine.py:174-181` 和 UNKNOWN 停机）。

### 2.5 周期性对账未按规划落地

`docs/p0_live_reconciliation_current_state.md:26-29` 的规划与当前实现的偏差：

| 规划 | 实现现状 |
| --- | --- |
| 周期对账位于 `_tick` 中 `broker.sync()` **成功之后** | 实际在每 tick **开头**（`engine.py:129`），sync 之前 |
| 按**配置间隔**独立触发 | 与交易 tick 强耦合，每 tick 全量执行 |
| 统计对账差异、写入状态文件 | 返回值被直接丢弃（`engine.py:129`） |
| 启动恢复**只在** `initialize()` 执行 | `initialize()`（`:88`）与每 tick（`:129`）都在恢复 |

即当前代码正是该文档自述的"整改前"状态。每 tick 对全部非终态单逐笔 `fetch`（`live_broker.py:225-230`），与 §2.2 的僵尸单叠加后 tick 延迟线性增长，还放大交易所限流风险。

### 2.6 持久化层健壮性不足（原问题仍存在）

- `core/state_store_v2.py:18`、`core/order_store.py:21`、`core/persistent_risk_guard.py:28` 三处 SQLite：均无 WAL 模式、无 `busy_timeout`（并发读即 `database is locked`）、无备份、无 `integrity_check`/损坏恢复路径；`state_store_v2` 无 schema 版本机制（`order_store.py:69-89` 好歹有 ad-hoc 列迁移）。
- `live_status.json` 非原子写（`engine.py:251-252` 直接 `open(...,"w")`），崩溃可留半个 JSON；`_export_state` 吞掉所有异常且只记类型名（`:253-254`）。
- `OrderStore.close()` 后置 `_connection=None`（`order_store.py:229-234`），此后 `get` 抛 `AttributeError` 而非明确异常。

### 2.7 【新发现】时钟时区混用

- 实盘引擎默认时钟是 naive 本地时间（`engine.py:49`，`clock or datetime.now`），而 `LiveBroker` 用 UTC（`live_broker.py:58`）。
- `claim_bar` 的租约时间戳直接传 `now.isoformat()`（`engine.py:195`），naive 值被 `state_store_v2.py:97-99` 当 UTC 比较——非 UTC 时区机器上租约年龄计算带固定偏移，可能提前回收（重复处理 bar）或延迟回收（bar 卡死一个租约周期）。
- 日初权益 key 用本地日期（`engine.py:169`），跨时区/夏令时时"交易日"口径与交易所不一致。

**修复方向**：引擎时钟统一为 `datetime.now(timezone.utc)`，并在 `_tick` 入口断言 tzinfo。

### 2.8 【新发现】过期的部分成交单永不终态

`normalize_exchange_status`（`core/orders.py:42-59`）的判定顺序：`filled > 0` → `PARTIALLY_FILLED`（`:49-50`）**先于** `canceled/expired` → `CANCELED`（`:53-54`）。一张已部分成交后过期的限价单（交易所返回 `expired` + `filled>0`）每次对账都被归一化为非终态的 `PARTIALLY_FILLED`，永久留在对账集合中，且仓位事实（部分成交）与账本状态（仍挂单）持续分叉。

**修复方向**：先判交易所终态 raw status（canceled/expired/rejected），再按 filled 量细分终态类型（如 expired-with-fill → CANCELED 并触发 sync）。

### 2.9 【新发现】`sync()` 用 free 余额低估现金

`core/live_broker.py:421-423`：`cash = free.get(base, total.get(base))`。挂单冻结的资金不计入 free，挂单期间权益快照系统性低估现金，进而影响日权益熔断（`engine.py:174-181`）的口径——可能误触发或低估回撤。应使用 `total`（或 free + 冻结项）。

### 2.10 现货路径的订单参数问题（原风险仍在，细节确认）

- 现货卖单默认 `is_reduce=True`（`live_broker.py:364`）后，`:130-131` 无条件给 params 塞 `reduceOnly: True`——这是衍生品参数，部分交易所现货接口会报错或行为未定义。应只在 `market_type in DERIVATIVE_TYPES` 时传。
- 数量钳制（`min(qty, closable)`）仅对衍生品生效（`:365-367`），现货超卖只能靠交易所拒单兜底（会走 §2.4 的 UNKNOWN/REJECTED 路径，又可能触发停机）。

### 2.11 策略异常被吞、监控状态失真

`engine.py:214-216`：bar 处理中任意策略/路由异常被 `except Exception` 捕获 → release bar、记 traceback、继续下一 bar。设计意图（单 bar 失败不连坐）可以接受，但 `_healthy` 不变、`_operational_state` 不变，导出的 `live_status.json` 仍显示 healthy——**策略持续失败时监控完全无感知**。应至少累计连续失败计数，超阈值后降级为 DEGRADED/HALTED 并告警。

---

## 三、P1：架构一致性与回测正确性

### 3.1 双订单状态机并存且值已漂移

- 回测侧改名 `BacktestOrderStatus(Enum)`（`core/broker.py:44`），消除了同名冲突，但仍是独立状态机：`PARTIALLY_FILLED` 的值回测为 `"partially_filled"`（`broker.py:48`）vs 实盘 `"partial"`（`core/domain.py:15`）；回测独有 `SUBMITTED`/`EXPIRED`，实盘独有 `SUBMITTING`/`ACCEPTED`/`CANCEL_PENDING`/`UNKNOWN`。
- 迁移校验只覆盖实盘侧（`core/orders.py:8-33`），`BacktestOrderStatus` 无任何迁移约束。
- 评估：语义分叉有现实理由（回测无"提交中"瞬时态），短期可接受；但值漂移（`"partial"` vs `"partially_filled"`）会在统一报表/对账时咬人，R5（共享事件管线）前必须收敛。

### 3.2 配置双来源：文件级已 fail-closed，键级静默回退与数值漂移仍在

`config/config.py:44-54` 已在文件缺失/损坏时 raise（好）。但 `config.get(section)` 对缺失 section 返回 `None`（`config.py:72-73`），消费方 `or {}` + `.get(key, default)` 静默回退，且**回退值与 `params.yaml` 不一致**：

| 键 | 代码回退值 | `params.yaml` 值 | 位置 |
| --- | --- | --- | --- |
| commission_rate_taker | `0.001` | `0.0005` | `backtest/engine.py:153`；`params.yaml:5` |
| commission_rate_maker | `0.0005` | `0.0002` | `backtest/engine.py:154-156`；`params.yaml:6` |
| risk_per_trade | `0.01` | `0.02` | `core/system_factory.py:28` |
| max_pos_size_pct | `0.20` | `0.30` | `core/system_factory.py:32` |
| stability_period | `2` | `1` | `core/system_factory.py:39` |
| atr_pct_threshold | `0.05` | `0.025` | `core/system_factory.py:45` |
| cooldown_bars | `3` | `2` | `core/system_factory.py:61-63` |
| slippage | `0.0` | `5`（bps） | `backtest/engine.py:66` |

即 YAML 中删掉某个 section/key，回测/实盘参数静默变成另一套值，无任何告警。

**更严重的 fail-open**：`build_router` 中 `routing` section 缺失时 `regime_map` 被置为**空字典**（`system_factory.py:55-58`），所有市场状态路由为 Cash，不告警、不报错——系统看起来"正常运行"但永不开仓。`router/router.py:22-27` 还存在第三份与 yaml 不一致的默认路由表。

**修复方向**：以 `params.yaml` 为唯一事实源，代码内默认值全部移除或改为"缺失即 raise"；启动时打印生效配置的摘要。

### 3.3 【新发现】回测撮合 bug：限价单成交价可突破限价

`core/broker.py:243-249` 正确地用限价约束了 `exec_price`（买单 `exec_price = open_price if open_price <= order.price else order.price`），但 `_execute_trade` 随后对所有方向叠加乘性滑点（`:319-324`）：

```python
if order.side in {"buy", "cover"}:
    fill_price = price * (1 + total_slip_rate)   # 可高于 limit price
elif order.side in {"sell", "short"}:
    fill_price = price * (1 - total_slip_rate)   # 可低于 limit price
```

买单最终成交价可能**高于限价**——现实中不可能的成交。`TrendUpStrategy` 恰好用限价单入场（`strategies/trend_following.py:113`），该 bug 直接影响主策略的回测结果。方向性影响：maker 成交时滑点使买入价抬升（虚增成本、偏保守），但 taker 分支 `open_price <= order.price` 时同样叠加滑点，突破限价的部分属于**虚构价格**，两者混合后回测收益不可信（方向不定，但必然失真）。

**修复方向**：`_execute_trade` 对 LIMIT 单在加滑点后重新 clamp 到 `order.price`（买单 `min(fill, limit)`、卖单 `max(fill, limit)`），或限价单直接跳过滑点（保守做法：maker 成交零滑点）。需要同步更新 `tests/test_p1_timeline_orders.py` 的预期。

### 3.4 回测正确性的其他缺口

- **无现金充足性检查**：buy/short 不校验 cash，回测中现金可为负（隐式无息杠杆），仅 `core/risk.py:155-160` 的 3x 敞口上限兜底。与 R1"现金/持仓/PnL 对账"目标冲突。
- 卖出数量 clamp 到 0 时订单被误标 `REJECTED`（`core/broker.py:280-281`、`:331-338`）——"没东西可卖"被记为拒单，污染订单统计。
- 风控用信号 bar 成交量（`strategies/base.py:201`）、撮合用成交 bar 成交量（`core/broker.py:211-214`），两套流动性口径不一致。
- `main.py:74` 默认初始资金 1000 与引擎默认 10000 不一致，入口不同结果不同。
- 无前视偏差与 next-bar 成交语义是**守护良好**的部分：`core/broker.py:222` 拒绝同 bar 成交、`strategies/trend_breakout.py:104-113` Donchian `shift(1)`、`tests/test_no_lookahead.py` 有断言，维持现状即可。

---

## 四、P2：工程质量与可维护性

| 项 | 现状 | 证据 |
| --- | --- | --- |
| ML 空壳包 | 仍存在：`models/features.py`/`labels.py`/`predictor.py`/`trainer.py` 各仅一个空类，全仓零引用 | `models/*.py` |
| Dashboard | 仍只有 `dashboard/utils.py`（416 行样式工具），无应用入口；`tests/test_p7_dashboard_integration.py` 名不副实（测的是 live_status.json 导出） | `dashboard/` |
| 生成物跟踪 | `reports/` 21 个历史文件、`dummy_output/` 6 个文件仍被 git 跟踪（ignore 已加但未 `git rm --cached`）；`dummy_output/` 无 ignore 条目且被 `tests/test_p2_orders_pnl.py:113` 当输出目录，跑测试即产生脏文件 | `git ls-files` |
| 命名 | "Qaunt" 拼写未改，`README.md:144` 结构图仍在传播 | `README.md:144` |
| 异常吞噬 | 活代码仅 `main.py:288` 一处裸 except（清理临时日志，低风险）；资金/订单路径已是 fail-closed，合格。两处例外见 §2.11 与 `live_broker.py:431`（sync 失败只记类型名无 traceback） | — |
| `main.py` 退出码 | **新发现**：所有失败路径（日期错误、无数据、回测失败）均 `print + return`，进程退出码恒为 0，CI/脚本无法感知失败；无参裸跑阻塞在交互输入（`main.py:108-196`） | `main.py` |

---

## 五、P3：测试与交付

- **测试现状良好但在单环境**：28 个测试文件、98 个用例在 `.venv` 下全绿；覆盖回测引擎/回归基线/无前视、config、execution port、G1 实盘安全、G2 bar 租约与 fail-closed、订单状态机、retry、live 状态导出。系统默认 Python 缺依赖仍无法直接跑（无环境自检入口）。
- **无 CI**：无 `.github/workflows`，无类型检查/lint/安全扫描/覆盖率门禁；无 Makefile/tox/pytest.ini。
- **依赖锁定**：`requirements.txt` 9 个直接依赖 `==` 钉版无哈希；`requirements.lock.txt` 71 行 freeze 式全量钉版无哈希，且含仅 `archive/` 引用的 `backtrader==1.9.78.123`——lock 与活代码脱节。
- **缺口类型**：仍无交易所 sandbox 端到端测试、无断网/超时/重复响应/部分成交的故障注入测试（`tests/test_retry.py` 只测了 retry 工具本身，而 retry 并未接入活代码）。

---

## 六、修复路线图建议

与 `docs/unified_roadmap.md` 的 R0–R8 对齐，按依赖顺序排列：

**第一批（P0，阻断一切实盘推进，预计小改动）：**

1. 修 UNKNOWN 死局与僵尸 SUBMITTING：为"查无此单"和"未提交"记录增加确认次数/TTL 后的终态迁移 + 强制 sync（§2.1、§2.2）。
2. `_tick` 外层兜底 + 退避，区分致命与瞬时故障（§2.3）。
3. 把 `with_retry` 接入 `submit_order`/`reconcile_order`/`sync`，把 `AlertSink` 接入引擎 critical 事件（§2.4）——两者都是已写好的代码，接线成本低。
4. 修 `normalize_exchange_status` 判定顺序（§2.8）、sync 改用 total（§2.9）、现货 reduceOnly 条件化（§2.10）。

**第二批（P0 收尾 + P1 回测可信）：**

5. 兑现周期对账规划（落点移到 sync 后、配置化间隔、差异统计入状态文件），与 `p0_live_reconciliation_current_state.md` 对齐（§2.5）。
6. SQLite 三件套加 WAL + `busy_timeout`；`live_status.json` 改 tmp+rename 原子写（§2.6）。
7. 时钟统一 UTC（§2.7）；HALT 状态持久化 + 人工恢复命令（§2.1 配套）。
8. 修限价单滑点 bug + 更新受影响测试与回归基线（§3.3）；回测加现金充足性检查（§3.4）。
9. 配置唯一事实源：移除代码内漂移默认值，routing 缺失时 raise（§3.2）。

**第三批（P2/P3，对应 R0 治理与 R6/R7 准备）：**

10. `main.py` 失败路径返回非零退出码；`git rm --cached` 清理 reports/ 与 dummy_output/ 并补 ignore；删除或显式标注 models/ 空壳与 dashboard/。
11. 加最小 CI（安装依赖 + unittest + 退出码检查）；lock 剔除 backtrader 并标注生成方式。
12. 故障注入测试骨架（超时/重复响应/部分成交/崩溃恢复），为 R7 长跑做准备。

**明确不建议现在做的**（与 roadmap §3 一致）：ML 交易、Alpha 扩张、dashboard 美化——账本与对账闭环稳定前这些都是负资产。

---

## 附录 A：验证命令

```bash
# 测试（项目 .venv，98 通过）
.venv/Scripts/python.exe -m unittest discover -s tests -q

# 重试/告警未接线的证据
#   Grep "from core.retry|import retry" → 仅 tests/test_retry.py:4
#   Grep "alerting" → 仅 core/alerting.py 自身

# 生成物仍被跟踪的证据
git ls-files | grep -E 'reports/|dummy_output/'   # 27 个文件
```

## 附录 B：文档状态说明

- 本报告与 `docs/p0_live_reconciliation_current_state.md` 的差异：该文档描述的周期对账方案**尚未实现**，当前代码是其自述的"整改前"状态（§2.5）。
- `docs/current_project_analysis.md`、`docs/2026_08_03_architecture_and_issues.md` 中已修复项见 §1.1，仍存在问题已被本报告吸收，两份文档建议移入 `docs/archive/` 或标注"已被 2026-08-05 报告取代"。
