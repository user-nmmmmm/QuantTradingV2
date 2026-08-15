# M-01～M-20 能力验收矩阵

本矩阵以可运行代码和测试为准，替代 2026-08-12 清单中的历史“缺失”状态。

| ID | 当前实现与验收证据 |
| --- | --- |
| M-01 | `tests/test_backtest_regression.py` 固定 fixtures、结构化事实包及连续运行一致性。 |
| M-02 | `core/metric_result.py` 提供 `MetricResult`、明确状态和 JSON Schema，区分零值与不可计算。 |
| M-03 | `core/ledger.py` 的权威账本保存可重建 fill、费用、现金、仓位和已实现 PnL。 |
| M-04 | `core/cost_model.py` 统一成本语义；缺失的资金/借券成本显式标为 `not_modeled`。 |
| M-05 | `PortfolioProjection.reconcile` 和现金充足性检查覆盖组合级对账。 |
| M-06 | `core/metrics.py` 提供回撤事件、交易质量、暴露、信号漏斗和成本敏感性。 |
| M-07 | `core/metrics.py` 提供归因、基准、R-Multiple、MAE/MFE 和 SQN。 |
| M-08 | `analysis/validation.py` 组合 OOS/walk-forward/Bootstrap/Monte Carlo/多重测试；`optimize.py --oos` 输出证据。 |
| M-09 | `core/exchange_boundary.py` 统一 markets、精度、步长、最小数量/名义金额。 |
| M-10 | `core/reconciliation_job.py` 原子输出日终对账报告。 |
| M-11 | `core/events.py` 定义共享事件、因果 ID、幂等消费和回放。 |
| M-12 | 原子状态、遥测、滞回告警、启动检查、心跳、对账、备份回滚和 Dashboard schema 均有模块与测试。 |
| M-13 | R7 故障注入、sandbox 凭据门控测试和连续运行证据审计已实现。 |
| M-14 | `core/gray_release.py` 与 `run_live.py --live` 强制 R7、单标的、小额上限、最小权限、人工批准和回滚。 |
| M-15 | TrendBreakout/TrendBreakdown 健康度通过 `bind_state_store` 持久化恢复。 |
| M-16 | 自动测试验证所有非 Cash 路由都有注册策略。 |
| M-17 | `Strategy.hard_stop_exit` 在策略特定退出前统一检查硬止损。 |
| M-18 | `strategies/volatility.py` 提供独立 VOLATILE 策略并接入路由。 |
| M-19 | `strategies/statistical_arbitrage.py` 提供跨标的配对信号。 |
| M-20 | `core/supervisor.py` 提供有限重启、指数退避及稳定期复位。 |

Supervisor 示例：`python -m core.supervisor --max-restarts 10 -- python run_live.py --sandbox --symbols BTC/USDT`。它只负责进程存活，不放宽任何交易权限或风险门禁。
