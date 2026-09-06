# QuantTrading 统一开发计划

> 文档状态：Active v1.0  
> 生效日期：2026-08-02  
> 上位路线图：[`unified_roadmap.md`](unified_roadmap.md)  
> 当前焦点：R0 基线与治理、R1 回测可信内核

## 1. 执行原则

一次提交只解决一个可独立验证的契约或行为；先固定失败样本再修改实现；公式、账本和展示分层提交；口径变化记录新旧差异；不覆盖无关改动；未建模能力返回状态，不用估算冒充事实。

任务卡必须记录：任务 ID、所属阶段、目标与非目标、输入输出契约、修改文件、测试、兼容性、差异、验收证据、已知限制和文档更新。

## 2. Batch 0：文档与固定基线

### 本轮专项补充（2026-09-05）

- [x] P0 组合 BLOCK_NEW 永久冻结修复：冷静期、低风险试运行、状态持久化和退出管理；
- [x] 同数据/同配置开关 A/B，以及关闭策略健康门控的独立隔离诊断；验收见 [`p0_drawdown_recovery.md`](p0_drawdown_recovery.md)。
- [ ] 当前策略 MANUAL_LOCK 的研究解释与重新准入，仍按策略专项路线图推进；本轮不解除。
- [x] 后续锁定排查：修复止损棘轮跨仓位残留，保存触发当时的退出批次证据并重跑历史回测；
  见 [`strategy_health_lock_investigation.md`](strategy_health_lock_investigation.md)。修复后仍有策略锁定，重新准入未完成。

### 原 Batch 0 清单

- [x] 建立唯一总路线图和统一执行计划；
- [x] 保留回测指标详细计划作为领域权威文档；
- [x] 建立无交易、可手算、固定历史/合成三类 fixtures；
- [x] 固定 start/end、seed、symbols、配置和数据摘要；
- [x] 保存 orders、fills、closed trades、equity、metrics；
- [x] 连续运行三次并结构化比较；
- [x] 固定 Python、测试依赖、测试命令和生成物规则。

建议目录：`tests/fixtures/backtest/`、`tests/test_backtest_regression.py`、`docs/baselines/`。

## 3. Batch 1：指标契约与 JSON

- [ ] 定义 `MetricResult`；
- [ ] 状态统一为 `ok/insufficient_data/undefined/not_modeled/invalid_input`；
- [ ] 冻结单位、参数、样本数和公式版本；
- [ ] `unrecovered` 作为回撤事件属性；
- [ ] 增加 `schema_version` 和 `formula_version`；
- [ ] 递归清理 Timestamp、NumPy、`pd.NA`、`NaT` 和非有限值；
- [ ] 最终 JSON 编码使用 `allow_nan=False`；
- [ ] 保留旧扁平字段兼容层。

## 4. Batch 2：权益、收益和年化

- [ ] 权益输入只读，明确时区、排序、重复时间戳和非法值策略；
- [ ] 清理输入时返回数据质量摘要；
- [ ] 建立唯一简单收益序列，剔除首个无收益点并保留有效 0；
- [ ] 年化优先级为显式配置 > timeframe+calendar > 可靠推断 > undefined；
- [ ] 区分缺失 bar 与混合频率、交易日与 24×7 市场；
- [ ] 月收益按日历月末计算，首月不完整标记，无交易月保留 0；
- [ ] 覆盖跨月、跨年、闰年、缺失交易日和复合对账。

## 5. Batch 3：成本与成交事实

- [ ] 冻结 reference/fill price、unit slippage、bps 和 slippage cost；
- [ ] 明确 fill price 是否已含滑点，避免重复扣除；
- [ ] commission、spread、impact、funding、borrow 分字段；
- [ ] 缺少数据标记 `not_modeled`；
- [ ] 普通、止损、止盈、切换、熔断和强平共享成交模型；
- [ ] 完成多空、部分成交和费用手算测试；
- [ ] 固定订单下成本增加时净 PnL 不增加；
- [ ] Gross/Execution/Net PnL 名称和公式一致。

## 6. Batch 4：权威交易账本

新增 `core/trade_ledger.py`，输入标准化 fills，输出 `closed_trades/open_lots/invalid_records/reconciliation_summary`。

- [ ] 文档化 FIFO；支持多空、加减仓、部分成交和反向；
- [ ] fill、position cycle、closed trade 使用稳定 ID；
- [ ] 保存入出场、成本、PnL、收益率、持有期和策略标签；
- [ ] 不完整事实不进入 closed sample，但保留警告；
- [ ] 从 `ReportGenerator` 移除 FIFO。

验收：closed trade 净 PnL 等于账户已实现净 PnL；相同 fills 产生确定结果。

## 7. Batch 5：Portfolio 与资金对账

快照包含 cash、long/short/net market value、gross exposure、realized/unrealized PnL、costs、equity、价格时间和估值状态。

- [ ] `equity = cash + net_position_value`；
- [ ] 初始资本、资金流、PnL、成本和期末权益桥接；
- [ ] 无持仓、单/多持仓、部分平仓和未平仓测试；
- [ ] 货币容差配置化，超差失败或高优先级告警；
- [ ] 缺价正式验收失败，不以 avg price 静默替代。

## 8. Batch 6：报告迁移与 BM0 验收

- [ ] `ReportGenerator` 只编排和展示；
- [ ] 输出 `metrics.json`、`closed_trades.csv` 和 reconciliation；
- [ ] 文本显示 `N/A` 和原因，图表读取标准结果；
- [ ] 旧字段有兼容测试和废弃说明；
- [ ] 固定样本记录新旧差异；
- [ ] 完整测试通过并更新 BM0 状态。

BM0 未验收前，不开发依赖账本或资金对账的高级指标。

## 9. Batch 7：BM1 与 BM2

- BM1：回撤事件列表；区分最大幅度和最长持续；保存峰值、谷底、恢复、自然日和观测周期；未恢复使用 `is_open=true`；输出 Top-N。
- BM2：样本摘要、平均盈亏、盈亏比、最大单笔、期望值、分位数、连胜连败、持有期、胜率区间和 PF 状态。无可靠 initial risk 时 R-Multiple 为 `not_modeled`。

## 10. Batch 8：BM3 与 BM4

- BM3：时间/资金/净/总暴露、空仓、同时持仓、换手、单位暴露收益，以及 signal → filter → order → fill → closed trade 漏斗。
- BM4：成本分解、成本比例、低/基准/高情景、滑点×佣金网格；保存完整配置和订单路径是否变化。

## 11. Batch 9：BM5–BM8

- BM5：entry/exit 分开归因，按策略、标的、方向、regime、退出原因分组并对账；
- BM6：基准对齐、超额收益、跟踪误差、信息比率、Beta/Alpha、滚动和分段；
- BM7：Initial Risk、R-Multiple、MAE/MFE、Stop/Exit Efficiency、SQN；
- BM8：train/test、walk-forward、block bootstrap、Monte Carlo、trials count、多重测试。

## 12. Batch 10：交易所规则与共享管线

- EXCH-01：markets、rounding、min qty/notional、reduce-only、结构化拒绝原因，回测/实盘复用校验。
- ARCH-01：MarketEvent/Signal/OrderIntent/Order/Fill/Snapshot、correlation/causation ID，共享 signal → risk → intent，三种模式只替换 adapter。

## 13. Batch 11：监控、告警和运维

- [ ] 原子 versioned snapshot；
- [ ] 数据、账户、组合、策略、订单、执行和系统遥测；
- [ ] 告警滞回、确认、恢复和抑制；
- [ ] Policy/Action 分离并保留人工覆盖；
- [ ] 启动检查、心跳、日终对账、优雅停机；
- [ ] 状态备份恢复、稳定 schema Dashboard 和故障演练。

## 14. 验收产物与当前顺序

每批保存代码与测试、契约/公式版本、fixtures、结构化结果、新旧差异、对账/性质测试、已知限制、未建模项和完整测试摘要。

当前顺序：固定 fixtures → 指标契约/JSON → 权益/收益/年化 → 成本语义 → trade ledger → Portfolio 对账 → 报告迁移和 BM0 验收。

EXCH-01 可独立并行设计；ARCH-01、监控告警和高级指标不得抢在上述契约稳定前大规模实施。
