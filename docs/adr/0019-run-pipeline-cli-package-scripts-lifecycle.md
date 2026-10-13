# 0019 回测运行与命令行拆成流水线；入口移到 `cli/`；脚本按生命周期分类（B5）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B5，[ADR 0017](0017-clear-layer-exceptions.md)、[ADR 0018](0018-mixins-to-collaborators.md)

## 背景
`BacktestEngine.run` 是一个 838 行的函数，`main.py` 的 `main()` 是 523 行，文件 1126 行；两者都是"准备 → 循环/依次执行 → 收尾"的线性流程，靠几十个共享局部变量串起来，既无法单独测试某一阶段，也让 `main.py` 成为被 20 多个文件引用的巨型模块。`scripts/` 有 100 个脚本，只有 README 在按用途分类。

## 决定
**1. `BacktestEngine.run` 流水线化。** 按顶层语句边界切成 4 个准备阶段（`_prepare_*`）、每根 K 线 5 个步骤（`_bar_*`）、4 个收尾阶段（`_finish_*`）和 2 个结果片段（`_result_core` / `_result_reports`）；`run` 本身只剩 32 行驱动。跨阶段共享的局部变量放进 `_RunState`（`__slots__`，拼错字段名会直接报错）；**只在单个阶段内使用的名字仍是该阶段的局部变量**。共享的判定规则是"某个阶段里先读后写"（值来自别的阶段或上一根 K 线）。方法体由 AST 工具机械改写（`x` → `rs.x`），不手改语句；循环里的 `continue`/`break` 只出现在最后一步，改写为 `return False/True`；结果字典按原顺序拆成两半再合并，键序不变。回测引擎的等价性基线测试（逐笔成交与权益）验证行为不变。
**2. `main()` 同样流水线化，入口移到新包 `cli/`。** `main` 拆成 10 个阶段函数（每个返回 `Optional[int]` 退出码，非 `None` 即提前结束），共享状态 `_MainState`。`main.py` 的 1126 行按职责移到 `cli/data.py`（行情加载）、`cli/replay.py`（清单回放与时间输入恢复）、`cli/run.py`（参数、一次回测、报告产物）；`main.py` 只剩 5 行入口（`quant-backtest = "main:main"` 不变，`python main.py …` 的子进程调用不变）。不留垫片：测试、`dashboard` 与脚本改为直接导入 `cli.*`。
- 新包 `cli` 在分层里位于 `dashboard` 之下、`research | analysis` 之上（它要导入 `research`，`dashboard` 可以导入它）；冻结范围加入 `cli/`，覆盖不缩小。
- **仓库根定位**：原来 `Path(__file__).resolve().parent` 在根目录的 `main.py` 里就是仓库根，搬到 `cli/` 后会指向 `cli/`；改为模块级 `ROOT = Path(__file__).resolve().parents[1]`，`tests/test_pipeline_structure.py` 固定 `ROOT` 与 `config/params.yaml` 的位置。
- 测试里对入口模块的 patch 目标随代码移动：`BacktestEngine`、`_load_requested_data`、`_execute_backtest`、`format_primary_metrics` 在 `cli.run`，`DataFetcher` 在 `cli.data`。
**3. `scripts/` 按生命周期分类，但不物理搬迁。** 新增 `scripts/classification.json`：每个脚本是 `supported`（入口、调度、验证、开发与质量门槛：34 个）、`research`（可复用的非主线实验：47 个）或 `historical`（一次性交付：19 个）；`register_strategy_successor.py` 虽在 README 的历史专项里，但新身份登记仍要用它（ADR 0008、0010），所以归 `supported`。`tests/test_scripts_classification.py` 检查：每个脚本恰好归一类并与 README 分节一致；`supported` 脚本不得依赖 `research`/`historical` 脚本（两条有理由的显式例外）；CI 与 `dev.py` 只运行 `supported` 脚本。
- **不搬迁的原因**：`historical` 脚本把自己的文件哈希写进证据回执（如 `run_strategy_review_cross_market.py` 比对 `digest(__file__)`），搬迁要改 `ROOT = parents[1]`，字节一变，历史批次的核验与续跑就会失败；`research` 脚本之间、与文档和测试之间的引用很多，批量搬迁的风险大于收益。分类 + 依赖规则已经达到"一次性研究脚本不进入主线"的目的。

## 备选方案
- 把 `run` 做成若干带各自状态的对象（真正的流水线对象）：需要为每个阶段设计输入输出，改动面大；先用共享状态对象机械拆分，再按需收窄。
- 为 `main.py` 保留 `from cli... import *` 垫片：违反"不留垫片"，并让测试 patch 目标含糊。
- 把 19 个历史脚本搬到 `scripts/history/`：见上，会使已记录的证据无法核验。

## 后果
- `BacktestEngine.run` 32 行，最长的阶段 84 行；`main` 24 行，最长的阶段 97 行。`BacktestEngine.__init__`（149 行）和 `_close_tail_positions`（118 行）不在本步范围。
- `_RunState` / `_MainState` 的字段目前标注为 `Any`（按名字共享，类型待收紧）；mypy 存量 404 → 365。
- 状态对象是"可变的共享袋子"：阶段之间的顺序依赖仍然隐含在 `run` 的驱动里，只是现在集中在一处、可见。
- 新的 `scripts` 条目必须同时更新 README 与 `classification.json`，否则测试会失败。
