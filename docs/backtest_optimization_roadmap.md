# 回测优化 Roadmap 与开发计划

> 文档状态：Draft v0.1
> 生效日期：2026-08-13
> 上位路线图：[`unified_roadmap.md`](unified_roadmap.md)
> 回测可信内核（R0/R1，fixtures/指标契约/trade ledger/对账）：[`development_plan.md`](development_plan.md)
> 本文件范围：回测模块的**正确性修复、真实感建模、策略能力、数据/指标/图表扩展**，与 `development_plan.md` 不重复、共享其验收基建。

## 1. 目标与边界

目标：让回测结果**可信、可解释、可复现**——修复会使结果失真的执行/资金缺陷，补齐让策略在回测与实盘行为一致的能力，并让数据、指标、图表能支撑研究与参数工程。

边界：
- 本 Roadmap 不涉及实盘模块（R4–R8），仅触及回测与共享的 `core/broker / risk / portfolio` 中被回测使用的能力；改动需通过现有 224 项测试 + 新增回归。
- 不引入未经样本外验证的 ML/微观结构建模；指标先接已有 `core/metrics.py`，再新增。
- 每个批次必须有可量化的验收标准（性质测试 / 对账 / 固定样本差异记录），沿用 `development_plan.md` 的"一次一契约、先固定失败样本再改实现"原则。

## 2. 问题清单 → 优先级（追溯矩阵）

来源：回测模块代码审查 + 可复现实验。

| ID | 问题 | 根因位置 | 影响 | 优先级 |
| --- | --- | --- | --- | --- |
| B-01 | GTC 限价单永不失效，reservation 泄漏 → 标的锁死 | `core/broker/__init__.py:152-153`、`core/risk/reservation.py:22-27` | 策略长期不交易，结果失真 | **P0** |
| B-02 | 无现金充足校验，账户可为负现金 | `core/risk/__init__.py:128-216`、`core/portfolio.py:59-67` | 产生真实中不可能的结果 | **P0** |
| B-03 | 做空免费（无抵押/借券/资金费） | `core/portfolio.py` 符号化多空 | 空头 alpha 被高估 | P1 |
| B-04 | 止损不按触发价成交，延迟一整根 bar | `strategies/*` `should_exit` + `core/broker/__init__.py:348-352`（STOP 未用） | 回撤/盈亏失真 | P1 |
| B-05 | 熔断强平是 Next-Bar 且被成交量限速 | `backtest/engine.py:115-128` | 极端行情回撤被低估 | P1 |
| B-06 | 成交量双重 1% 限速，大单被拆 100+ 根 | `backtest/engine.py:72-75`、`core/risk/__init__.py:161-166` | 成交价系统性偏离信号价 | P1 |
| B-07 | 部分成交把一次往返算成多笔交易 | `backtest/reporting.py:143-276` | TotalTrades/WinRate 失真 | P2 |
| B-08 | 多标的用"最近已知价"估值 | `core/runtime.py:118-123` | 指标/熔断轻微失真 | P2 |
| B-09 | 基准时间轴不齐用 fillna(0) 近似 | `backtest/engine.py:146-164` | 超额收益不可比 | P2 |
| B-10 | 部分成交卖单在持仓耗尽后标 REJECTED | `core/broker/__init__.py:434-442,378-380` | 成交统计含假拒单 | P2 |
| B-11 | `metrics.py` 约 30 个指标函数未接入报告 | `backtest/reporting.py:71-74` | 研究能力闲置 | P2 |
| B-12 | 只跑日线，`resample_ohlcv` 未接线 | `main.py:51`、`core/data.py:81-108` | 无法多周期验证 | P2 |
| B-13 | 合成数据不可控/无极端场景模板 | `core/data_fetcher.py:332-401` | 压力测试缺失 | P2 |
| B-14 | `optimize.py` 无样本外/稳健性校验 | `analysis/optimize.py:68-119` | 参数过拟合 | P1 |
| B-15 | 无本地数据缓存 | `main.py:240-253` | 重复拉取、不可复现 | P3 |

## 3. 阶段总览与依赖

```text
A 正确性/真实感修复（P0/P1）
 ├─ A1 订单生命周期 ─> A2 资金/杠杆 ─> A3 止损保真
 ├─ A4 熔断强平 ─> A5 成交量单源
 └─（A1–A3 完成后再动 B 层，避免在失真地基上铺指标）

B 执行与核算保真（P1/P2）
 └─ B1 权威 trade ledger* ─> B2 估值/基准 ─> B3 报告接线 metrics ─> B4 OOS 数据管理

C 策略能力（P2/P3，依赖 A 真实感 + B1 账本）
 └─ C1 真止损/止盈单 ─> C2 仓位管理 ─> C3 多周期/时间过滤 ─> C4 组合层策略 ─> C5 健康自适应

D 数据/指标/图表（P2/P3，可与 A/B 并行）
 └─ D1 多周期+缓存 ─> D2 数据扩展 ─> D3 指标库 ─> D4 图表 ─> D5 优化稳健性

E 质量保证（贯穿）
 └─ 回归基线（复用 development_plan Batch0）、性质测试、文档
```

> `*` B1 的 `core/trade_ledger.py` 已在 `development_plan.md` Batch 4 定义，本计划**复用其产出**，仅追加"修复 B-07 计数口径"任务。

## 4. 阶段 A：正确性/真实感修复（P0/P1）

### A1 订单生命周期（B-01）
- 目标：限价/止损单不能无限挂单占额度。
- 任务：
  1. `Broker` 增加订单 TTL 策略：策略未指定 TIF 时限价单默认 `DAY`，或 N-bar 未成交自动 `CANCELED`（阈值进 config）；
  2. 取消/过期时确保 `RiskReservationProjection` 释放（打通 `OrderEvent` 状态 → `RELEASING_STATUSES` 全路径，`core/risk/reservation.py:22-27`）；
  3. `Strategy.on_bar` 入场路径与实盘对齐：入场前检查 `has_active_open_order`（补齐 `core/broker/__init__.py:493` 的调用缺口）；
  4. reservation 的 `reference_price` 对限价单用"信号收盘价"，避免随市价放大（`core/risk/reservation.py:128-132`）。
- 验收：构造"单调上行 + 不触价限价单"样本，断言限价单在 N-bar 后被取消、reservation 归零、后续入场恢复；`pending_open_notional` 不再随时间增大。
- 测试：`tests/test_broker_order_ttl.py`、`tests/test_reservation_release.py`。

### A2 资金与杠杆语义（B-02、B-03）
- 目标：回测账户不能为负现金；做空要承担成本与抵押。
- 任务：
  1. `RiskManager.check_entry_risk` 增加**现金/保证金充足校验**（现货：`trade_value + 已有占用 ≤ free_cash`；合约：保证金占用 ≤ free margin）；
  2. `Portfolio.update_position` 对现金设置下限，越限拒绝而非静默变负；
  3. 引入做空成本模型：初始保证金占用、按 bar 累积的借券/资金费率（费率字段来自 D2 数据，缺省 `not_modeled` 并显式报告）；
  4. 回测 `max_leverage` 默认按市场类型区分（spot=1）；现货回测不允许裸做空（`short` 仅在合约市场可用，与实盘 `_validate_intent` 一致）。
- 验收：性质测试"6×30% 持仓"样本断言第 4 单起被现金校验拒绝、账户现金恒 ≥ 0；做空样本断言收取借券/资金费、收益等于真实成本后的结果；`max_leverage=3` 的 spot 样本告警提示不支持杠杆。
- 测试：`tests/test_risk_cash_sufficiency.py`、`tests/test_short_costs.py`。

### A3 止损/止盈执行保真（B-04）
- 目标：止损按触发价成交，不再"下一根开盘价"。
- 任务：
  1. `Strategy.should_exit` 检测到盘中穿透时提交 `OrderType.STOP`（`core/broker/__init__.py:348-352` 已具备撮合逻辑）；
  2. `Order` 增加 `stop_price` 字段并写入 trade 记录（与实盘订单状态机对齐）；
  3. 报告同时输出 `stop_price` 与 `actual_fill_price`，并在 report.txt 说明口径（"止损触发价 vs 成交价"）。
- 验收：实验样本"bar 内低点 90、止损 95"断言平仓价 ≥95（多）且在同一根 bar 记录触发，不再延迟至下一根；`trades.csv` 含两列价格。
- 测试：`tests/test_stop_price_fidelity.py`。

### A4 熔断强平即时化（B-05）
- 任务：
  1. 熔断路径改为**当前 bar 即可成**（市价清仓，不再 `timestamp=event.timestamp` 顺延到下一根）；
  2. 强平单不参与成交量 1% 限速（风控强平应全额成交或标记 partial + 告警）。
- 验收：构造熔断样本，断言平仓发生在触发 bar 且全额成交；与 Next-Bar 口径差异记录在固定样本差异表中。
- 测试：`tests/test_circuit_breaker_liquidation.py`。

### A5 成交量限速单源化（B-06）
- 任务：
  1. 移除 `max_participation_rate` 与 `liquidity_limit_pct` 的双重限制，只保留**一个权威口径**（建议：RiskManager 做下单前校验，Broker 只做撮合预算，二者取其一为唯一限速）；
  2. 限速参数写入 `config/params.yaml`，报告记录每笔成交的 `participation_ratio`。
- 验收：同一订单在新口径下成交根数显著下降（性质测试：成交数量 ≤ 成交量×参与率上限恒成立）。
- 测试：`tests/test_participation_single_source.py`。

## 5. 阶段 B：执行与核算保真（P1/P2）

### B1 权威 trade ledger（B-07 + 复用 development_plan Batch 4）
- 任务：
  1. 复用 `core/trade_ledger.py` 的 FIFO closed-trade 输出；
  2. **合并部分成交**：同一 position cycle 的多笔 fill 聚合为一笔 closed trade（修复 B-07 计数失真）；
  3. `ReportGenerator._analyze_trades` 删除手写 FIFO，改用 ledger 输出。
- 验收：`TotalTrades` = 完整往返次数；closed trade 净 PnL = 账户已实现净 PnL（对账）。

### B2 估值与基准（B-08、B-09）
- 任务：
  1. 明确"缺价/陈旧价"策略：多标的时间轴不齐时用**最近已确认 bar**估值并在报告中标注 `stale_price_bars` 计数；禁止静默用 avg_price（与实盘 `valuation.build_portfolio_snapshot` fail-closed 对齐）；
  2. 基准对齐改为**公共区间内等权再平衡**，去掉 `fillna(0)` 近似；缺失标的在该 bar 不参与均值（报告记录 coverage）。
- 验收：构造"标的时间轴错开"样本，断言权益曲线与基准用同一价格规则，且陈旧 bar 被标注。

### B3 报告接线 metrics.py（B-11）
- 任务：把已实现的指标接进 `ReportGenerator.generate`，输出 `metrics.json`：
  - `calculate_trade_quality`（胜率区间/PF/持有期）
  - `calculate_attribution`（按策略/标的/月份，∑=NetPnL）
  - `calculate_benchmark_comparison`（超额收益/相关）
  - `calculate_cost_sensitivity`（佣金×滑点网格）
  - `calculate_exposure`（逐 bar 敞口 → 供 D4 图表）
- 验收：`metrics.json` 覆盖 BM1–BM8 关键字段；`report.txt` 显示 N/A 及原因；旧扁平字段兼容测试通过。

### B4 样本外数据管理（B-14 前置）
- 任务：
  1. `main.py` / `optimize.py` 增加 `--oos`（train/test 切分）与 walk-forward 窗口支持，复用 `core/metrics.py` 的 `walk_forward_windows / train_test_split_returns`；
  2. 保存 train/test 边界与参数，报告 OOS 指标与 IS/OOS 差异。
- 验收：参数选择只发生在 train 段，test 段不参与调参；报告输出 OOS Sharpe/PF。

## 6. 阶段 C：策略能力（P2/P3）

### C1 真止损/止盈单落地（配合 A3）
- 任务：`Strategy.on_bar` 入场时把 `stop_loss` / `take_profit` 作为**挂单**提交（`Broker` 增加 `OrderType.STOP/STOP_LIMIT` 撮合与 TTL），追踪止损在 `should_exit` 中更新挂单而非事后判断。
- 验收：趋势/突破策略的止损以挂单形式存在；回测结果与 A3 一致。

### C2 仓位管理升级
- 任务：
  1. 新增 ATR/波动率目标（vol targeting）定仓，替代固定 risk_per_trade；
  2. 支持分批/加仓与组合剩余风险预算分配。
- 验收：波动率目标下，持仓波动率收敛到目标区间；加仓不突破集中度/杠杆。

### C3 多周期与时间过滤
- 任务：
  1. 接入 `MarketStateMachine.align_state_to_lower_tf`（`core/state.py:187-205`）做 HTF 确认；
  2. 增加可选 session / 最小连续确认 bar 过滤。
- 验收：策略按 HTF 状态过滤前后结果差异记录；测试覆盖对齐无 lookahead。

### C4 组合层策略
- 任务：增加组合级模块（跨标的资金分配、相对强弱过滤、风险预算调度），对现有单标的策略做上层编排。
- 验收：多标的组合敞口 ≤ 杠杆上限且资金利用优于独立策略；与风控反馈闭环。

### C5 健康/自适应统一
- 任务：把 TrendBreakout 的"熄火"机制通用化，用**成交价**（非信号价）统计，增加策略级最大回撤熔断与参数扰动自检。
- 验收：用真实成交价重算后，`TrendBreakout.check_health` 样本结论与修复前差异记录。

## 7. 阶段 D：数据 / 指标 / 图表（P2/P3）

### D1 多周期与缓存（B-12、B-15）
- 任务：
  1. `main.py` 增加 `--timeframe`，接线 `DataHandler.resample_ohlcv`（`core/data.py:81-108`），回测引擎支持任意周期；
  2. 本地 parquet/csv 缓存（按 symbol+timeframe+日期范围键控），二次运行命中缓存。
- 验收：`--timeframe 4h` 可跑通；连续两次运行命中缓存不重拉网络。

### D2 数据扩展（B-13 + 资金费率）
- 任务：
  1. Synthetic 增加可复现 seed 与极端场景模板（跳空、闪崩、单边停牌、高波动 regime），`generate_scenario` 支持 `seed` 参数（`core/data_fetcher.py:332-401`）；
  2. 可选接入资金费率/借券费率数据源（ccxt `fetch_funding_rate`），回填 A2 做空成本。
- 验收：压力模板触发熔断/强平路径；funding 数据缺失时成本字段标 `not_modeled` 且不静默为 0。

### D3 指标库扩展（`core/indicators.py`）
- 任务（按优先级）：RSI、MACD、ROC/Stochastic（动量）；OBV、VWAP、MFI、量比（量能）；历史波动率、BB 带宽、EWMA vol（波动）；Supertrend/Parabolic SAR（结构）。
- 验收：每个新指标有边界测试（NaN 前缀、与 pandas/TA-Lib 参考对拍、输入只读）。

### D4 图表扩展（`backtest/reporting.py` + 可选 dashboard）
- 任务（优先级序）：
  1. 价格图 + 进出场标记；
  2. 月度收益热力图（年×月）；
  3. 滚动 Sharpe / 滚动回撤；
  4. 盈亏 / R-multiple 分布直方图；
  5. 按策略/regime 分组净值 + regime 区间高亮；
  6. 信号价 vs 成交价散点（滑点可视化）；
  7. 组合敞口时序（复用 B3 的 `calculate_exposure`）；
  8. 参数敏感性热力图（对接 D5 输出）。
- 验收：所有图表数据来自 `metrics.json`/标准结果文件（图表不重复计算）；无数据时显示 N/A。

### D5 优化稳健性（B-14）
- 任务：
  1. `optimize.py` 接入 B4 的 OOS 切分，筛选指标改为 OOS Sharpe/PF；
  2. 增加参数扰动检验（±10% 邻域）与 `benjamini_hochberg` 多重检验（`core/metrics.py:741-771`）；
  3. 输出参数热力图 CSV + 图表（D4-8）。
- 验收：OOS 排名与 IS 排名差异可视化；超过 3 个参数组合时输出 BH 校正后的显著参数集。

## 8. 质量保证（阶段 E，贯穿）

- E1 复用 `development_plan.md` Batch 0 fixtures：每个批次先固定失败样本，再改实现，三次运行事实一致。
- E2 性质测试：成本不变量（固定订单下成本增加 → 净 PnL 不增）、现金恒 ≥0、对账（closed-trade PnL = 账户已实现 PnL）、单调性。
- E3 回归：每批次后运行 `python -m pytest -q`（现有 224 项全绿 + 新增项），并更新 `docs/backtest_assumptions.md` 与 `README.md` 的参数/口径说明。

## 9. 顺序与里程碑

```text
M1（A 完成，P0/P1 修复）: A1→A2→A3→A4→A5   验收：B-01..B-06 实验样本全绿，无负现金/无限挂单/裸做空
M2（B 完成）: B1→B2→B3→B4                   验收：metrics.json 全量、trade ledger 对账通过、OOS 接入
M3（C 完成）: C1→C2→C3→C4→C5                验收：策略用真实成交价/挂单止损，HTF 与组合策略可运行
M4（D 完成）: D1→D2→D3→D4→D5                验收：多周期+缓存、资金费率、指标/图表齐全、优化稳健
持续: E1–E3 每批次回归
```

每个里程碑之间留一次"固定样本差异报告"：记录修复前后指标变化（尤其 B-03/B-04/B-07 会导致收益、交易数、回撤口径变化），防止"修对了但结果变了"被误读为回退。

## 10. 已知限制（不纳入本计划或显式 not_modeled）

- 无 tick/盘口数据时不建模真实冲击与订单簿深度（只做参与率近似）；
- 不做 ML/新闻/基本面信号；
- 美股调整（拆股/分红）仅在引入股票数据源时处理；
- 资金费率缺失时做空成本标 `not_modeled`，不在回测中假装为 0 收益成本。
