# 质量门槛与棘轮

本页说明 CI 和本地检查如何防止质量回退，以及怎样逐步收紧。基础设施阶段 0.3 引入。

## 三层门槛

| 层 | 检查 | 范围 | 当前状态 |
|---|---|---|---|
| 硬门槛 | `ruff check .`（只含 `E9,F63,F7,F82` 语法/未定义名级错误）、`mypy` 三个契约文件 | 全仓库 / `core/domain.py`、`core/runtime.py`、`live_trading/execution_adapter.py` | 0 违规，必须保持 |
| **棘轮** | `scripts/quality_ratchet.py check`：ruff 的 `F,E4,E7,E9,B`（不含 `B905`）与 mypy（`core backtest live_trading strategies router composition`） | 全仓库 / 上述六个包 | 存量记入基线：ruff 621 项、mypy 869 项；**只许减少，不许增加** |
| 覆盖率 | `pytest --cov=core --cov=backtest --cov=live_trading --cov-fail-under=85` | 三个包 | 实测约 89%；门槛从 55% 提到 85% |

棘轮按"文件 + 规则"计数（忽略行号），所以无关修改不会打扰它。基线在 `scripts/quality_baseline.json`，规则配置在 `pyproject.toml` 的 `[tool.quality_ratchet]`。mypy 固定按 `linux` / `3.11` 分析，开发机和 CI 的计数一致。

## 日常使用

```bash
python scripts/quality_ratchet.py check            # 与 CI 相同
python scripts/quality_ratchet.py check --tool ruff
python scripts/quality_ratchet.py update           # 修掉存量问题后，把改进写入基线
```

- 新增代码触发新的 ruff 或 mypy 问题：`check` 失败并列出 `文件::规则: 旧 -> 新`。优先修掉；确属必要时用精确的 `# noqa: CODE` / `# type: ignore[code]` 并在评审里说明，不要直接改基线。
- 修掉存量问题后 `check` 会提示"可锁定"，运行 `update` 并提交基线，改进就不会再丢。
- 想把某类规则提升为硬门槛：先把它的存量清零，再把规则加入 `[tool.ruff.lint] select`，并从棘轮配置里去掉。

## 测试

- 并行：`python -m pytest -n 4`（需要 `pytest-xdist`，已在 `requirements-dev.txt`）。完整套件在 4 个进程下约 3 分钟，串行约 5–10 分钟。
- 超时：每个测试 600 秒硬上限（`pytest-timeout`）。
- 标记：`--strict-markers` 已开启。`slow`（约 5 秒以上的测试，目前 14 个测试函数、18 个用例）、`network`（真实联网，目前没有）。快速循环：`python -m pytest -m "not slow" -n 4`。新增耗时测试请加 `@pytest.mark.slow`。
- CI 会打印最慢的 20 个测试（`--durations=20`）。

## 本地钩子（可选）

```bash
python -m pip install pre-commit && pre-commit install
```

`.pre-commit-config.yaml` 只用本地工具：硬门槛 ruff、棘轮、Markdown 断链、仓库卫生检查。

## 存量问题里值得人工复核的几类

棘轮只是冻结它们，没有修改任何行为。以下是扫描时发现、值得逐个看一眼的模式（未逐个判定是否为缺陷）：

- **B023（闭包里使用循环变量）**：`core/binance_depth_observer.py`（8）、`research/ml_selection/next_round.py`（4）、`analysis/execution_calibration.py`（4）、`scripts/run_trend_portfolio_v3.py`（4）、`backtest/reporting/trades.py`（2）等。若闭包在循环结束后才被调用，会拿到最后一次迭代的值。
- **F841（赋值未使用）**：`core/broker/financing.py` 的 `seconds_per_year`，在资金费率/借贷利息计提函数内，看一下是不是漏掉了年化换算。
- **B904**：`core/data_versions.py`、`scripts/fetch_spot_intraday.py` 里 `except` 中 `raise` 缺少 `from`，会丢失原始异常链。
- **mypy `attr-defined`（475 项）**：绝大多数来自 mixin 访问宿主类属性，随"mixin 改组合"的重构自然消失，不需要单独处理。
- **F811 出现在测试里**多为 pytest fixture 参数遮蔽同名导入，属正常写法。
