# 0043 `cli` 包纳入覆盖率口径

- 状态：已采纳
- 日期：2026-11-15
- 相关：[质量门槛](../quality.md)，[基础设施路线图](../infrastructure_roadmap.md) C5，[ADR 0024](0024-coverage-90-and-branch-coverage-evaluation.md)

## 背景
ADR 0024 把覆盖率门槛提到 90% 时，`cli` 包（`run.py`、`replay.py`、`data.py`，B5 从 `main.py` 搬出的回测入口）没有放进 `--cov` 范围，理由是纳入会改变口径，留待单独评估。结果是命令行入口无论有没有测试，门槛都看不到。

2026-11-15 在 Windows 本地（Python 3.13，串行，5448 passed、8 skipped、111 subtests）带 `--cov=cli` 实测行覆盖：

| 范围 | 语句 | 行覆盖 |
|---|---|---|
| 原五个包（`core ops signal_meta backtest live_trading`） | 25701 | 91.15% |
| `cli` | 725 | 84.69%（`run.py` 83.7%、`replay.py` 92.4%、`data.py` 79.2%） |
| 六个包合计 | 26426 | 90.97% |

同一次运行另有 10 个失败，全部在 `tests/test_dependency_runtime_compatibility.py`：本机虚拟环境的 6 个依赖版本落后于锁文件（#112/#113 之后没有重装），与本次改动无关；失败的测试不调用被统计的包里的代码以外的路径，对上表影响可以忽略，但上表仍是本地数字，不是 CI 结果。

## 决定
- `scripts/dev.py` 的覆盖率命令加上 `--cov=cli`，门槛保持 **90%**。CI 通过 `dev.py test` 运行，所以本地与 CI 仍是同一条命令。
- 不为了这次变更补凑数的测试，也不调整门槛：合并口径下降约 0.18 个百分点，仍高于门槛。
- `strategies`、`router`、`composition` 仍不在范围内，本 ADR 不评估它们。

## 备选方案
- 继续排除 `cli`：入口代码缺测试时没有任何门槛会发现。
- 给 `cli` 单独设门槛：`--cov-fail-under` 只有一个数字，需要另写脚本读取覆盖率 JSON；`cli` 只有 725 条语句，不值得多一套机制。

## 后果
- 余量从约 1.15 个百分点降到约 0.97 个（本地）。Linux CI 上的数字没有实测；若首次 CI 低于 90%，按 ADR 0024 的约定补 `cli/data.py`、`cli/run.py` 的测试，不降门槛。
- 覆盖率数字与此前的记录不可直接比较：2026-11-15 之前是五个包的口径。
