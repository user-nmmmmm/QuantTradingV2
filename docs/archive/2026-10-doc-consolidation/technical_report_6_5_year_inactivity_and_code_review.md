# 6.5 年无交易问题：技术分析、代码评审与解决方案

日期：2026-08-31  
评审对象：`20260831_152622_3344d_30Syms_Ret54.0pct` 及当前风险/运行时/回测实现  
评审性质：只读诊断与设计评审；本报告不修改资金路径代码

## 1. 执行摘要

最新 30 标的回测请求覆盖 2017-07-01 至 2026-08-27，但交易路由在 2020-02-18 后归零，2020-02-26 完成账户清算，此后权益一直保持 15,403.57，直到回测结束。所谓“6.5 年不交易”不是行情没有信号，也不是 OBV、Donchian 突破或仓位上限持续过滤，而是以下三项行为叠加：

1. 组合从历史高点回撤 15.41% 后进入粘性的 `BLOCK_NEW` 状态；
2. 运行时把“禁止新开仓”错误地实现为“不调用整个 Router”，使已有仓位的策略退出和 hard stop 也停止运行；
3. 回撤继续恶化至 22.96%，状态升级为 `LIQUIDATE` 并清空剩余仓位；该状态只能通过 `manual_resume()` 降级，而回测引擎没有恢复协议，因此后续永远不再路由，但仍继续遍历剩余六年半行情并计算全周期指标。

结论分为两个层面：

- **风险意图层面**：`BLOCK_NEW`、`LIQUIDATE`、`LOCKED` 采用粘性状态是明确、经过测试的实盘安全设计，不应简单删除。
- **实现层面**：`BLOCK_NEW` 不应停掉已有仓位的退出管理；回测也必须明确选择“终止”或“预注册恢复协议”，不能无提示地在清算后继续六年半并把它包装成普通全周期策略回测。

本评审发现 2 个 P0、5 个 P1、3 个 P2 问题。最高优先级修复是把“允许退出”和“允许开仓”拆成独立能力，并为回测增加明确的终止/恢复生命周期。

## 2. 范围与证据

### 2.1 核心产物

- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/report.txt`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/equity.csv`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/trades.csv`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/routing_log.csv`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/breaker_audit.csv`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/breaker_state.json`
- `reports/20260831_152622_3344d_30Syms_Ret54.0pct/run_manifest.json`

### 2.2 代码范围

- `core/runtime.py`
- `core/risk/circuit_breaker.py`
- `core/risk/`
- `backtest/engine.py`
- `router/router.py`
- `strategies/base.py`
- `tests/test_phase3_risk_margin_execution.py`
- `tests/test_backtest_engine.py`

### 2.3 方法

本评审执行了以下只读核对：

1. 将 breaker audit 中的权益值与日度权益曲线、成交和路由日志对齐；
2. 重建 2020-02-10 至 2020-03-02 的逐日状态；
3. 追踪 `RiskManager.check_circuit_breaker()` 的返回语义；
4. 追踪 `EventProcessor.process()` 对该返回值的使用；
5. 追踪 `BacktestEngine.run()` 的减仓、清算和循环生命周期；
6. 检查粘性状态、手工恢复和引擎行为的现有测试；
7. 使用当前状态和突破公式估算清算后的原始入场 setup。

## 3. 事实时间线

### 3.1 关键日期

| 日期 | 事件 | 回撤/权益 | 系统行为 |
|---|---|---:|---|
| 2020-02-14 | 历史权益高点 | 20,072.25 | 12 个仓位仍在持有 |
| 2020-02-15 | `NORMAL → REDUCE` | 触发前权益 17,898.47；回撤 10.83% | 12 个仓位各减半；成本后曲线权益 17,850.73 |
| 2020-02-16 | 正常策略退出 | 17,467.91 | ETC 剩余仓位跌破 10 日低点退出 |
| 2020-02-17 | 正常策略退出 | 17,347.27 | BCH 剩余仓位跌破 10 日低点退出 |
| 2020-02-18 | 最后一个正常路由日 | 17,821.05 | 仍有 10 个仓位受策略退出管理 |
| 2020-02-19 | `REDUCE → BLOCK_NEW` | 16,979.25；回撤 15.41% | Router 完全停止；已有仓位退出管理也停止 |
| 2020-02-20 至 02-25 | 冻结路由但继续持仓 | 权益由 16,922.28 降至 16,246.04 | 0 路由、0 策略退出；10 个仓位继续暴露 |
| 2020-02-26 | `BLOCK_NEW → LIQUIDATE` | 触发前权益 15,463.26；回撤 22.96% | 10 个仓位强制清算；成本后权益 15,403.57 |
| 2020-02-27 至 2026-08-27 | 粘性清算状态 | 权益固定 15,403.57 | 0 路由、0 开仓、0 持仓；引擎仍遍历行情 |

### 3.2 逐日路由证据

2020-02-18 仍有 14 条路由记录：

- `candidate: 3`
- `cash: 1`
- `position_exit_control: 10`

2020-02-19 起路由记录为 0。该突变与 `BLOCK_NEW` 到达同一天，不是市场状态逐步变差造成的。

### 3.3 清算后不是“没有信号”

按当前公式对 2020-02-18 之后的行情进行只读重算：

- `TREND_UP` 标的日：9,959；
- 20 日价格突破：3,125；
- 价格突破且 OBV 通过：2,943；
- `TREND_UP + 20 日突破 + OBV` 原始 setup：814。

814 是忽略持仓、组合资金、冷却和同日竞争的原始 setup 上限，不能解释为 814 笔可成交交易；但它足以排除“后六年半行情没有策略条件”这一假设。

## 4. 根因分析

### 4.1 直接根因：一个布尔值混合了两种不同权限

`CircuitBreakerMixin._blocks_new_risk()` 的语义是“是否禁止新风险”：

```python
return self.circuit_breaker_triggered or (
    rank(self.portfolio_breaker_action) >= rank(BLOCK_NEW)
)
```

`check_circuit_breaker()` 最终直接返回这个值。到达 `BLOCK_NEW` 后返回 `True`。

`EventProcessor.process()` 随后使用：

```python
if not breaker:
    # 遍历所有 symbol，调用 Router，处理退出并收集开仓候选
```

问题在于 Router 的职责不是只有开仓。`Router.collect_candidate()` 的顺序是：

1. 消费成交；
2. 若已有仓位，执行最大持有期、hard stop 和策略退出；
3. 只有空仓时才进入冷却、状态切换和开仓候选。

因此 `if not breaker` 把以下两项一起关闭了：

- 应关闭：新开仓候选与风险分配；
- 不应关闭：已有仓位的减仓、止损和策略退出。

这是 2020-02-19 至 02-25 已有仓位失去退出管理的直接原因。

### 4.2 持续根因：组合保护状态按设计是粘性的

`check_circuit_breaker()` 只允许风险动作升级：

```text
NORMAL < REDUCE < BLOCK_NEW < LIQUIDATE < LOCKED
```

当回撤恢复时，目标动作可能回到较低等级，但代码不会自动降级。注释明确规定只有 `manual_resume()` 可以降低保护级别。

该设计对实盘安全是合理的：一次严重组合事故不能因短期反弹自动恢复交易。但回测引擎没有任何 `manual_resume()` 调用，也没有预注册的自动恢复协议。因此一旦到达 `LIQUIDATE`，该次回测从投资决策角度已经终止。

### 4.3 报告根因：引擎没有把清算状态当作生命周期事件

`BacktestEngine.run()` 在 `LIQUIDATE` 后只执行一次 `force_liquidate()`，随后继续整个 market-data stream：

```python
for event in market_data.stream():
    ...
```

循环没有：

- `termination_timestamp`；
- `termination_reason`；
- `active_period`；
- `inactive_bars`；
- `resume_policy`；
- 终止后 break；
- active/full 双口径指标。

结果是清算后的现金权益被重复写入 2,386 个 drawdown periods，并参与九年 CAGR、Sharpe、基准比较和图表。

这种计算不一定在会计上错误：如果投资者资金清算后一直留在现金，4.90% 的九年 CAGR 是完整资金体验。但它不能替代策略 active-period 的研究指标，且必须在摘要中显式说明策略已于 2020 年终止。

## 5. 指标影响

| 统计窗口 | 截止日期 | 总收益 | CAGR | Sharpe | 最大回撤 |
|---|---:|---:|---:|---:|---:|
| 至 `BLOCK_NEW` | 2020-02-19 | 69.79% | 23.50% | 1.338 | -15.41% |
| 至最终清算 | 2020-02-26 | 54.04% | 18.64% | 1.067 | -23.26% |
| 全报告期 | 2026-08-27 | 54.04% | 4.90% | 0.564 | -23.26% |

上述差异说明：

- 全周期 CAGR 和 Sharpe 被六年半现金期显著摊薄；
- 终止前指标又可能高估长期稳健性，因为没有覆盖后续市场制度；
- 正确做法不是挑更好看的一个，而是同时展示 full-capital-period 与 active-strategy-period，并标明终止原因。

## 6. 代码评审发现

### [P0] `BLOCK_NEW` 错误关闭已有仓位退出路径

**位置**：`core/runtime.py:164-190`，关联 `router/router.py:50-113`  
**证据**：2020-02-19 到 02-25 路由为 0，但仍持有 10 个仓位。  
**影响**：hard stop、Donchian exit、状态退出、最大持有期全部失效，组合只能等待更高级别清算。  
**风险**：扩大损失；使 `BLOCK_NEW` 的实际行为偏离名称和配置含义；实盘中也可能在最需要退出时失去策略级退出管理。  
**建议**：拆分 `allow_exits` 与 `allow_entries`。任何组合动作下都应处理成交和退出；只有 entry collection/allocation 受 `BLOCK_NEW` 控制。`LIQUIDATE/LOCKED` 由组合级强平优先处理。

### [P0] 清算后无显式终止或恢复协议，导致永久静默运行

**位置**：`backtest/engine.py:245-325`，`core/risk/circuit_breaker.py:93-118, 152-164`  
**证据**：2020-02-26 清算后仍遍历至 2026-08-27；`manual_resume()` 在回测路径没有调用。  
**影响**：后续币种和信号从未被策略评估；报告表面覆盖九年，实际交易只覆盖约两年半。  
**建议**：新增显式 `backtest.breaker_policy`。默认采用 `on_liquidate: terminate`；若需要研究恢复，必须使用预注册、确定性的 cooldown/rebase 协议并生成独立报告。

### [P1] `breaker` 布尔值命名和返回语义过载

**位置**：`core/risk/circuit_breaker.py:87-90, 120-184`；`core/runtime.py:164-201`  
**问题**：同一个布尔值同时表示日内熔断、组合禁止开仓和运行时是否应完全跳过路由。  
**影响**：调用方无法区分“禁止开仓”“必须减仓”“必须清算”“完全锁定”。  
**建议**：返回结构化 `RiskControlDecision`，至少包含：

```text
action
allow_position_management
allow_new_entries
force_reduce_fraction
force_liquidate
terminal
reason_codes
```

### [P1] breaker audit 缺少时间戳和前后权益

**位置**：`core/risk/circuit_breaker.py:157-164`  
**问题**：audit 只有 `from/to/equity/high_water/drawdown`，没有 timestamp、bar index、阈值、触发原因、动作后的权益和成本。  
**影响**：无法仅靠 audit 文件确定动作日期；audit 中 17,898.47 与 equity curve 的 17,850.73 看似不一致，实际是强制减仓及成本前后口径不同。  
**建议**：记录 `occurred_at`、`bar_index`、`threshold`、`pre_action_equity`、`post_action_equity`、`cost`、`positions_before/after`、`action_id`。

### [P1] `daily_loss_triggered` 字段并不等于日内亏损被触发

**位置**：`core/risk/circuit_breaker.py:67-73`；`backtest/engine.py:379-383`  
**问题**：`reset_daily_breaker()` 在组合状态达到 `BLOCK_NEW` 后会把 `circuit_breaker_triggered` 继续设为 True；报告却把该通用状态输出为 `daily_loss_triggered`。  
**证据**：最新 `breaker_state.json` 显示 `daily_loss_triggered: true`，但 breaker audit 只有 portfolio drawdown transitions，没有独立 daily-loss 事件。  
**影响**：错误归因风险触发来源。  
**建议**：拆分：

```text
daily_loss_triggered
portfolio_action
blocks_new_risk
liquidation_triggered
locked
```

### [P1] 日线回测中的 daily-loss 基准可能退化为同值比较

**位置**：`core/runtime.py:156-169`  
**问题**：跨日时先使用“当前事件更新后的权益”覆盖 `_daily_start_equity`，随后立刻用相同当前权益检查日内回撤。对于每天只有一个 event 的 1D 回测，daily drawdown 在该检查点通常为 0。  
**影响**：`daily_loss_limit` 在日线回测中可能无法表达前收至今收的单日损失；现有测试只检查 reset 调用次数，没有检查传入基准值。  
**建议**：明确日线定义。若定义为前一交易日收盘权益，则保存 previous-session close equity；若定义为当日开盘权益，则需用开盘价格重估后再比较。增加 1D 与 intraday 两套测试。

### [P1] `applied_breaker_actions` 不支持恢复后的第二次动作

**位置**：`backtest/engine.py:244, 268-287`  
**问题**：`reduce/liquidate/locked` 被放入整次 run 生命周期的集合。一旦未来增加 `manual_resume()` 或自动恢复，第二个风险周期再次达到 `REDUCE` 时不会执行减仓。  
**影响**：恢复方案若直接加入，会产生“状态显示 reduce 但不执行”的隐患。  
**建议**：以 `action_transition_id` 或 `breaker_epoch` 做幂等，而不是按动作名称全局去重。

### [P2] 风险动作和回测生命周期耦合在 engine 条件分支中

**位置**：`backtest/engine.py:256-305`  
**问题**：margin liquidation、drawdown reduce、account liquidation 和 daily limit 分散在一个 `if/elif` 链中，优先级隐含。  
**影响**：同一 bar 同时满足 margin 和 portfolio breaker 时，只保留第一个分支；审计不记录被覆盖的原因。  
**建议**：由风险控制器返回单一、带优先级和 reason set 的执行计划，engine 只执行计划。

### [P2] 缺少回测终止后的机会损失报告

**位置**：reporting/writers 和 engine result payload  
**问题**：系统继续读取行情，却不输出“因为风险停机而跳过多少潜在 setup”。  
**影响**：用户容易把 0 交易误判为策略没有信号。  
**建议**：终止后可选运行只读 shadow signal evaluator，不允许下单，只输出 `suppressed_setups_after_termination`；必须明确它是反事实诊断，不计入绩效。

### [P2] 现有测试保护了粘性状态，却未保护正确的 `BLOCK_NEW` 语义

**位置**：`tests/test_phase3_risk_margin_execution.py:28-53`，`tests/test_backtest_engine.py:27-53`  
**现状**：已有测试验证状态粘性、manual resume 和每日 reset 调用。  
**缺口**：没有端到端测试证明：

- `BLOCK_NEW` 时新开仓被拒绝；
- `BLOCK_NEW` 时已有仓位仍执行 hard stop/策略退出；
- `LIQUIDATE` 后生成终止元数据；
- 无恢复策略时永不重新开仓；
- 恢复后第二次 `REDUCE` 仍会执行；
- daily loss 与 portfolio block 的审计字段不混淆。

## 7. 目标状态机

建议保留现有五级风险动作，但明确每级能力：

| 状态 | 管理已有仓位 | 策略正常退出 | 新开仓 | 组合强制动作 | 是否终止 |
|---|---:|---:|---:|---|---:|
| NORMAL | 是 | 是 | 是 | 无 | 否 |
| REDUCE | 是 | 是 | 是，乘 reduced multiplier | 首次进入时减仓 | 否 |
| BLOCK_NEW | 是 | 是 | 否 | 无 | 否 |
| LIQUIDATE | 是 | 组合清算优先 | 否 | 清空全部仓位 | 回测默认是 |
| LOCKED | 是 | 组合清算优先 | 否 | 清空全部仓位 | 是 |

关键原则：

1. “禁止新增风险”不能等于“禁止降低风险”；
2. 退出路径应始终可用，除非组合级清算在同一 bar 具有更高优先级；
3. 粘性状态保留，恢复必须显式；
4. 回测终止和实盘锁定是不同生命周期表达，但应共享同一风险事实。

## 8. 推荐解决方案

### 8.1 方案 A：默认生产/准入回测——清算即终止（推荐）

配置示意：

```yaml
backtest:
  breaker_policy:
    on_reduce: continue_reduced
    on_block_new: exit_only
    on_liquidate: terminate
    on_locked: terminate
    recovery: none
```

行为：

1. `REDUCE`：减仓一次，继续路由和开仓，但风险乘数降低；
2. `BLOCK_NEW`：继续检查已有仓位退出，不收集/分配新开仓；
3. `LIQUIDATE`：清空仓位，记录终止原因和时间；
4. engine 可以停止遍历，或继续做 shadow diagnostics，但 active performance 截止终止日；
5. 报告状态必须是 `TERMINATED_BY_RISK`，不能只显示普通完成。

优点：最符合当前粘性风险治理；最不容易通过自动恢复美化历史结果。

### 8.2 方案 B：预注册恢复研究——冷静期后显式 rebase

仅用于单独的 research scenario，不应覆盖方案 A 的结果。

配置示意：

```yaml
backtest:
  breaker_policy:
    on_block_new: exit_only
    on_liquidate: cooldown
    recovery:
      mode: timed_rebase
      flat_bars_required: 30
      health_bars_required: 5
      rebase_high_water: true
      max_resumes: 1
      approved_by: backtest_protocol_v1
```

要求：

- 恢复规则在查看 holdout 前固定；
- 清算后必须保持空仓至少 N bar；
- 数据/系统健康必须连续通过；
- rebase 必须写入 audit；
- 恢复形成新的 breaker epoch；
- 报告分别展示每个 epoch；
- 与无恢复基线并列，不得只展示恢复后的更优版本。

### 8.3 不推荐方案

以下方案不应采用：

- 在每个自然年自动清零 high-water，但不披露；
- 回撤恢复到 15% 以下就自动解除粘性状态；
- 直接删除 `BLOCK_NEW/LIQUIDATE`；
- 为增加交易次数在完整历史中多次调用无审计 `manual_resume()`；
- 用后六年的信号表现反向选择最佳恢复日期。

这些做法会把风险治理问题转换成明显的回测过拟合。

## 9. 推荐代码改造

### 9.1 引入结构化风险决策

建议新增不可变对象：

```python
@dataclass(frozen=True)
class RiskControlDecision:
    action: BreakerAction
    allow_position_management: bool
    allow_new_entries: bool
    force_reduce_fraction: float | None
    force_liquidate: bool
    terminal: bool
    reason_codes: tuple[str, ...]
    transition_id: str | None
```

`check_circuit_breaker()` 返回该对象，避免调用方猜测一个布尔值的含义。

### 9.2 将 Router 拆成退出和开仓两阶段

推荐接口：

```python
router.process_position_management(...)
router.collect_entry_candidate(...)
```

事件顺序：

```text
market data / pending fills
→ valuation
→ risk decision
→ forced portfolio action（若有）
→ position management（仍有仓位时）
→ entry candidates（仅 allow_new_entries）
→ allocation
→ audit / accounting
```

需要定义同一 bar 中组合清算与策略退出的优先级，避免重复平仓。建议 `LIQUIDATE/LOCKED` 优先于策略退出；`BLOCK_NEW` 仅影响开仓。

### 9.3 增加 BacktestLifecycleState

建议输出：

```text
status: completed | terminated_by_risk | locked | data_exhausted
active_start
active_end
termination_timestamp
termination_reason
inactive_bars
resume_count
breaker_epochs
suppressed_setups_after_termination
```

### 9.4 报告双口径指标

核心摘要同时展示：

```text
Full capital-period return/CAGR/Sharpe
Active strategy-period return/CAGR/Sharpe
Post-termination cash duration
Termination reason
Benchmark comparison over both aligned windows
```

不得用 active-period 指标替换完整资金体验，也不得只用 full-period 指标掩盖策略已经终止。

## 10. 测试计划

### 10.1 单元测试

1. `NORMAL` 允许退出和开仓；
2. `REDUCE` 返回正确 multiplier 和唯一 transition id；
3. `BLOCK_NEW` 禁止 entry，但允许 hard stop；
4. `BLOCK_NEW` 禁止 entry，但允许 Donchian exit；
5. `LIQUIDATE` 返回 force-liquidate + terminal；
6. `manual_resume` 创建新 epoch；
7. 新 epoch 再次进入 REDUCE 时再次执行减仓；
8. daily loss 和 portfolio drawdown 具有独立标志；
9. audit 每条记录含 timestamp 与 pre/post equity。

### 10.2 集成测试

构造一条确定性价格序列：

1. 建仓；
2. 回撤至 10%，验证减仓；
3. 回撤至 15%，同时产生策略退出，验证退出仍执行；
4. 再次建仓请求，验证被拒绝；
5. 回撤至 20%，验证强制清算和终止元数据；
6. 终止后继续输入行情，验证不会产生订单；
7. research recovery 模式下冷静期结束，验证按协议恢复；
8. 恢复后第二次风险周期仍正确执行。

### 10.3 回归验收

对固定数据重跑当前 30 标的基线，预期变化：

- 2020-02-19 后仍有 position-management 路由，直到仓位退出或清算；
- 在 `BLOCK_NEW` 期间没有新的 entry risk decision；
- 若已有仓位先触发策略退出，最终 AccountLiquidation 的仓位数可能少于当前 10 个；
- 最终权益、交易数和 drawdown 允许变化，因为当前结果受 P0 缺陷影响，不能要求逐位兼容；
- accounting identity、事件幂等、订单生命周期必须继续通过；
- 报告明确输出 termination metadata。

## 11. 实施阶段

### 阶段 1：P0 语义修复

- 拆分 `allow_position_management` 与 `allow_new_entries`；
- 保证 `BLOCK_NEW` 下退出路径继续运行；
- 增加相应单元和集成测试；
- 不引入自动恢复。

### 阶段 2：回测生命周期

- 增加默认 `on_liquidate: terminate`；
- 输出 termination metadata；
- 报告 full/active 双口径；
- 输出停机期和 suppressed setup 诊断。

### 阶段 3：审计模型

- breaker audit 增加时间戳、阈值、pre/post equity、成本和 action id；
- daily 与 portfolio 状态拆分；
- `applied_breaker_actions` 改为 transition/epoch 幂等。

### 阶段 4：可选恢复研究

- 实现预注册 timed-rebase 模式；
- 独立 scenario 和独立报告；
- 不覆盖生产默认和无恢复基线；
- 使用 walk-forward/holdout 重新验证。

## 12. 验收门槛

修复完成至少满足：

1. `BLOCK_NEW` 后新开仓数为 0；
2. `BLOCK_NEW` 后已有仓位的 hard stop/策略退出测试通过；
3. `LIQUIDATE` 后状态为 `terminated_by_risk`；
4. 报告显示 active end、termination date 和 inactive days；
5. breaker audit 每条状态转换可唯一映射到 equity curve 和 fills；
6. daily-loss 字段不再被 portfolio block 冒充；
7. 无恢复模式下后续不会再次开仓；
8. 恢复模式下每次恢复和每个 risk epoch 可审计；
9. 固定基线重跑 accounting identity 全部通过；
10. 新报告不再把“后六年半零交易”解释成“策略没有信号”。

## 13. 测试执行说明

本评审运行了风险与引擎相关的 14 个目标测试。风险状态机相关测试通过；2 个 `test_backtest_engine.py` 测试因当前受限环境不能写入系统临时目录而失败，错误发生在 `TemporaryDirectory`/`routing_log.csv` 文件权限，不是断言失败。由于本轮不修改实现，没有为绕过环境权限而改动测试代码。

现有测试确认粘性状态和 `manual_resume()` 是有意设计；但未覆盖本报告识别的 `BLOCK_NEW` 退出路径问题。

## 14. 最终结论

6.5 年不交易的主因不是策略稀疏，而是风险状态和回测生命周期共同作用：

```text
15% 高水位回撤
→ BLOCK_NEW
→ 运行时错误跳过全部 Router（退出也停止）
→ 回撤扩大
→ LIQUIDATE
→ 粘性状态无恢复
→ 引擎继续遍历六年半但永远不再路由
```

正确修复不是取消熔断，而是：

```text
BLOCK_NEW = 退出照常、只禁开仓
LIQUIDATE = 清算并默认终止回测
恢复 = 预注册、显式、可审计的独立研究情景
报告 = 完整资金期与策略活跃期双口径
```

在完成 P0 修复并重跑之前，当前 54.04%/38 trades 报告应视为受风险路径缺陷影响的历史证据，不应作为当前策略准入或参数优劣的最终依据。
