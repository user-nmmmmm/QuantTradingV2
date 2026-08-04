# 遗留脚本一致性审计

审计日期：2026-08-03。生产回测权威实现为 `backtest.engine.BacktestEngine`。下表中的脚本均未导入该实现，因此其费率、撮合顺序和 Next-Bar Execution 语义不能视为与生产系统一致。

| 脚本路径 | 复用生产引擎 | 最后提交 | 被当前文档引用 | 建议处理 |
|---|---:|---:|---:|---|
| `research/alpha_breakout.py` | 否 | 2026-02-08 | 否 | 归档；独立撮合研究原型，不用于决策 |
| `research/p2_reality_check.py` | 否 | 2026-02-08 | 否 | 归档；独立撮合研究原型，不用于决策 |
| `Trading_V1_Model.py` | 否 | 2026-07-22 | 是，且被标为遗留入口 | 归档；保留历史参考，不作为入口 |
| `verify_range.py` | 否 | 2026-02-06 | 仅历史文档 | 归档；功能由测试覆盖 |
| `verify_router.py` | 否 | 2026-02-06 | 仅历史文档 | 归档；功能由测试覆盖 |
| `verify_trend_strategies.py` | 否 | 2026-02-06 | 仅历史文档 | 归档；功能由测试覆盖 |
| `archive/Local_Crypto_Backtest.py` | 否 | 2026-02-07 | 否 | 保留归档现状 |
| `archive/range_mr.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/reports.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/Robustness_Check.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/runner.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/tempCodeRunnerFile.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/Trading_Crypto_Model.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/Trading_US_Stock_Model.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/Trading_V1_Model.py` | 否 | 2026-02-06 | 是，作为历史文件 | 保留归档现状 |
| `archive/trend_long.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |
| `archive/trend_short.py` | 否 | 2026-02-06 | 否 | 保留归档现状 |

## 结论与使用约束

- 根目录与 `research/` 中列出的独立撮合脚本移入 `archive/legacy_p1/`，文件头明确标注历史属性。
- 现有 `archive/` 内容继续保留，但统一由 `archive/README.md` 声明不得作为当前生产行为或投资决策依据。
- 后续若恢复任何脚本，必须改为调用 `BacktestEngine`，并用冻结基线验证费率和 Next-Bar Execution 时间线一致后，才能移出归档区。
