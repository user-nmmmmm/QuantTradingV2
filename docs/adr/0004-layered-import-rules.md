# 0004 用 import-linter 固定包的依赖方向，例外只减不增

- 状态：已采纳
- 日期：2026-10-11
- 相关：[PR #66](https://github.com/user-nmmmmm/QuantTradingV2/pull/66)，`pyproject.toml` 的 `[tool.importlinter]`，[架构边界说明](../architecture_review.md)

## 背景
原有的架构测试漏掉了 `research`，`core/events/codec.py` 因此悄悄依赖了研究代码；另外回测引擎直接导入 `analysis` 和 `research`。依赖方向靠人工记忆无法长期维持。

## 决定
- 分层（高→低）：`dashboard` > `research | analysis` > `backtest | live_trading` > `composition` > `router` > `strategies` > `config` > `core`；`core` 不依赖其他项目包。函数内的延迟导入同样检查，`scripts/` 与 `tests/` 在规则之外。
- 移除 `core → research`；仍向上的 4 条延迟导入登记为已知例外（`backtest.engine → analysis.label_account_reconciliation`、`backtest.coin_selector → research.ml_selection.*`），并由测试固定为"只减不增"。
- 例外随 V4 选币器与特征管线统一时清零。

## 备选方案
- 立即搬动例外涉及的模块：牵涉 `main.py`、仪表盘、脚本和约 7 个测试模块，且本质是选币器的归属设计，留给 V4。
- 只用手写的 AST 测试：无法表达分层与独立同级。

## 后果
新增跨层依赖必须先改规则并说明理由。若要引入新的顶层包，需要先决定它在层级中的位置。
