# 质量门槛与棘轮

本页说明 CI 和本地检查如何防止质量回退，以及怎样逐步收紧。基础设施阶段 0.3 引入。

## 门槛一览

| 层 | 检查 | 范围 | 当前状态 |
|---|---|---|---|
| 硬门槛 | `ruff check .`（只含 `E9,F63,F7,F82` 语法/未定义名级错误）、`mypy` 三个契约文件 | 全仓库 / `core/foundation/domain.py`、`core/foundation/runtime.py`、`live_trading/execution_adapter.py` | 0 违规，必须保持 |
| **棘轮** | `scripts/quality_ratchet.py check`：ruff 的 `F,E4,E7,E9,B`（不含 `B905`）与 mypy（`core ops signal_meta backtest live_trading strategies router composition`） | 全仓库 / 上述八个包 | 存量记入基线：ruff 588 项、mypy 326 项（C2 之后；最初 621 / 869）；**只许减少，不许增加** |
| 导入方向 | `lint-imports`（分层契约 + `core` 不依赖其他包），详见 [架构边界说明](architecture_review.md) | 全部项目包 | 无已知例外；新增例外需要设计决定 |
| 覆盖率 | `pytest --cov=core --cov=ops --cov=signal_meta --cov=backtest --cov=live_trading --cov-fail-under=85` | 五个包 | 实测约 89%；门槛从 55% 提到 85% |

棘轮按"文件 + 规则"计数（忽略行号），所以无关修改不会打扰它。基线在 `scripts/quality_baseline.json`，规则配置在 `pyproject.toml` 的 `[tool.quality_ratchet]`。mypy 固定按 `linux` / `3.11` 分析，开发机和 CI 的计数一致。

## 日常使用

一条命令跑完与 CI `quality` 作业相同的检查并给出汇总：`python scripts/dev.py check`（`--no-tests` 跳过测试，`list` 查看所有任务；也可用 `quant-dev`）。注意：断链检查只看已被 git 跟踪的文件，新增的文档要先 `git add`。

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

### 耗时预算

- 完整套件在 CI（`-n 4`）上**目标 ≤ 5 分钟**，目前约 3 分钟；"Tests and coverage gate"步骤有 8 分钟的硬超时，整个 `quality` 作业 20 分钟。
- 单个测试约 5 秒以上必须加 `@pytest.mark.slow`；本地循环用 `-m "not slow"`。超预算时先看 CI 打印的最慢 20 个测试，把慢的拆小或标记，而不是放宽超时。

### 偶发失败（不稳定测试）

1. **不要靠重跑掩盖。** 先复现：`python -m pytest <测试> -n 0` 连续跑多次（例如 20 次），再换成 `-n 4` 看是否只在并行下失败。
2. **定位。** 常见原因：依赖当前时间或时区、测试间共享临时路径或全局状态（`monkeypatch` 漏恢复）、端口/文件锁、依赖字典或集合的遍历顺序、并行进程互相覆盖同名文件。
3. **修，或隔离。** 修不了当天就加 `@pytest.mark.quarantine(reason="…（说明现象与已定位的原因，至少 10 个字符）", until="YYYY-MM-DD")`：测试继续运行，失败不阻塞（等同非严格 xfail），通过会显示为 XPASS。**期限最多约 30 天**；`tests/test_quarantine_policy.py` 在过期后让 CI 失败，逼着修复或删除；同时被隔离的测试最多 10 个。
4. 隔离不是豁免：在 PR 描述里写明原因和负责人，修好后立即去掉标记。

### 平台相关的跳过

在 Windows 上创建符号链接需要开发者模式或管理员权限（`WinError 1314`）。依赖符号链接的测试用 `tests/capabilities.py` 的 `requires_symlinks`：先探测能力，不具备时跳过（本地 Windows 全绿），具备时照常运行。Linux CI 上能力探测必然成功，`tests/test_quarantine_policy.py` 保证这类跳过不会在 CI 上悄悄发生。新的平台相关测试照此写，不要用 `sys.platform` 硬编码。

## 本地钩子（可选）

```bash
python -m pip install pre-commit && pre-commit install
```

`.pre-commit-config.yaml` 只用本地工具：硬门槛 ruff、棘轮、Markdown 断链、仓库卫生检查。

## 存量问题的复核结论（C2，2026-10-14）

棘轮扫描曾列出几类值得人工看一眼的模式。逐个读过调用点之后的结论如下；**没有发现需要改变交易行为的缺陷**。

| 模式 | 结论 | 处理 |
|---|---|---|
| **F841** `core/broker/financing.py` 的 `seconds_per_year` | 死代码，不是漏了年化换算：借贷利息的年化实际写在 `_accrue_short_borrow` 和 `_accrue_quote_borrow` 里（365 天、按实际经过秒数）；永续资金费率是"每个结算周期的费率"，本来就不年化 | 先写特征测试 `tests/test_financing_annualization.py`（用 3 小时间隔精确比较，改成 360 天会失败，已做变异验证），再删除死变量并把两处硬编码合并为 `SECONDS_PER_YEAR`。数值逐位不变 |
| **B023**（闭包里使用循环变量），29 处，分布在 14 个文件 | **全部无害**：每个闭包都在定义它的那一轮迭代内被调用（例如 `binance_depth_observer` 在 `finally` 里取消并等待 `producer` 之后才进入下一次重连）。没有闭包活过迭代 | 用默认参数显式绑定循环变量，消除"晚绑定"隐患，语义等价；B023 现为 0 |
| **B904** `core/market/data_versions.py`、`scripts/fetch_spot_intraday.py` | 原异常只是预期的控制流（"文件已存在，去比对内容"；"`float()` 解析失败，改按日期解析"），新抛出的 `ValueError` 与之无关 | 改为 `raise … from None` |
| **F841** `backtest/reporting/trades.py` 的 `s_losses` | 无副作用的死赋值 | 删除 |
| **mypy `attr-defined`**（原 49 项） | 无运行时缺陷。`LiveBroker` 的 6 个属性在 `configure_account_source()` 里迟绑定，读取前都有 `getattr(…, None)` 保护；`Broker.risk_action_targets` 同理；`protective_orders` 里变量名 `current` 先当列表后当单个订单（前一分支必然 `return`）；`signal_meta_replay` 的 `broker: object` 标注过窄；其余是动态附加的属性 | 用不创建属性的类级注解声明迟绑定属性（`getattr` 语义不变）、重命名变量、放宽标注，剩 2 处带理由的 `type: ignore`；`attr-defined` 49 → 11 |
| 仍留在棘轮里的 `attr-defined`（`strategies/trend_breakout.py` 的 3 个私有 mixin，11 项） | `_raw_entry_signal`、`name`、`entry_window` 等由混入它们的具体策略类提供，不是缺陷 | 随策略 mixin 的重构处理（ADR 0018 把它们排除在 B4 之外） |
| **F811 出现在测试里** | 多为 pytest fixture 参数遮蔽同名导入，属正常写法 | 不处理 |

基线随之降低：ruff 621 → 588，mypy 365 → 326。以后新增这类模式，先判断闭包/异常/迟绑定属性是否真的越出了当前迭代或保护范围，再决定修还是用带理由的 `noqa`/`type: ignore`。
