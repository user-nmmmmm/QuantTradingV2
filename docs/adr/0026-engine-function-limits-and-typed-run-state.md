# 0026 引擎与命令行的函数长度上限；运行状态对象的具体类型（B8）

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B8，[ADR 0019](0019-run-pipeline-cli-package-scripts-lifecycle.md)，[ADR 0022](0022-mypy-hard-gate-list.md)

## 背景
B5（ADR 0019）把 `BacktestEngine.run` 与 `main()` 拆成了流水线，并留下两件事：几个仍然很长的函数，以及两个状态对象 `_RunState`（`backtest/engine.py`）和 `_MainState`（`cli/run.py`）的字段全部标成 `Any`。2026-10-14 实测：`BacktestEngine.__init__` 149 行，`_close_tail_positions` 118 行，`cli/replay.py::replay_manifest` 131 行；`_RunState` 有 48 个字段（其中 4 个已有具体类型）、`_MainState` 有 26 个字段，大部分是 `Any`。

## 决定
- **拆函数，只搬语句块，不改语句。** 脚本按行号切出连续的语句块（带内容断言防止行号漂移），原样放进新函数，并按原来的顺序调用，所以校验错误出现的先后、消息文本和副作用顺序都不变。
  - `BacktestEngine.__init__` 149 → 73：`_init_candidate_selection`、`_init_temporal_inputs`、`_resolve_run_settings`、`_init_signal_policies`、`_init_breaker_policy`。
  - `_close_tail_positions` 118 → 66：`_record_terminal_valuation`（`valuation_only`）、`_submit_tail_exits`（EndOfBacktest 平仓与合成结算 bar）。
  - `replay_manifest` 131 → 43：`_restore_research_policies`、`_in_recorded_symbol_order`、`_build_replay_engine`、`_replay_report`。回放前置检查原来是十几处"打印 + `return 7`"，现在由前两个函数抛出私有异常 `_ReplayRefused`（文本与原来一字不差），`replay_manifest` 统一打印并返回 7。
- **状态对象的每个字段都标具体类型。** `_RunState` 48 个、`_MainState` 26 个，类型取自赋值来源函数的签名（`Portfolio`、`Broker`、`Dict[str, Strategy]`、`Optional[BenchmarkResult]`、`RuntimeResult` ……）。非运行时必需的类（`RiskManager`、`Router`、`Strategy` 等）只在 `TYPE_CHECKING` 下导入。
- **类型暴露出的几处不精确，用等价写法修正，不改行为：** `strategies` 参数在构建注册表之前可能是 `None`，所以在状态里拆出 `requested_strategies` 一个字段；只有 Donchian 家族有的 `strategy.health` 原来靠 `getattr(…, None)` 逐处判断，现在集中到一个函数 `_health_machines`；`budget` 与 `drawdown_reducer` 同时存在这一事实补进条件里；`artifact_failures` 实际是字符串列表；引擎跑完后 `market_data_adapter` 为 `None` 时给出明确的 `RuntimeError`，而不是 `AttributeError`。
- **新增结构测试** `tests/test_pipeline_structure.py`：这四个文件里没有 ≥ 100 行的函数；两个状态对象的注解与 `__slots__` 一一对应，且没有 `Any`。

## 范围说明
路线图 B8 讲的是引擎与命令行。全仓库另有 55 个超过 100 行的函数，多数在交易核心里（`portfolio_target_controller.process` 406 行、`fill_service._execute_trade` 386 行、`tick_orchestrator._tick_once` 319 行 ……）。它们触及下单与风控路径，每一个都需要先固定行为再拆，不属于这次的机械搬动，也不应与它混在一个 PR 里；这里不宣称"全仓库无长函数"。

## 后续：函数长度棘轮（2026-10-14）
不去逐个重写交易核心里的长函数，而是让"不更糟"由机器保证。`quality_ratchet.py` 新增第三个工具 `function_length`（`pyproject.toml` 的 `[tool.quality_ratchet.function_length]`，限额 100 行，范围是 13 个项目包）：每个 ≥ 100 行的函数以"文件::类.函数"记录行数，只许变短；新出现的 ≥ 100 行函数会让 `check` 失败。基线里现有 **87 个**（上文的 55 只统计了其中 10 个包，这里是全部 13 个包，含 `research`、`analysis`、`dashboard`），最长的是 `portfolio_target_controller.process`（406 行）。引擎与命令行的四个文件在基线里为 0，并有测试固定。把基线里的长函数拆开是逐个的、需要先固定行为的工作，留给触碰它们的 PR。

## 备选方案
- 把 `__init__` 的参数收进一个配置对象：会改变 `BacktestEngine` 的公共构造签名，影响全部调用方和历史脚本。
- 把回放前置检查保留为多处 `return 7`、只拆别的部分：函数仍然超长。
- 用 `Any` 以外的"宽"类型（`object`）：只是把错误推迟到使用处。

## 后果
- 引擎与命令行里的函数最长 97 行（`cli/run.py::_write_artifacts`），仍 < 100。
- 状态对象的字段现在会被 mypy 检查；`backtest/engine.py` 的存量没有增加（仍是原来的 1 项）。`cli/` 不在棘轮的 mypy 范围内，单独检查时仍有一批原有的发现（`Missing return statement` 等），不是这次引入的，也没有在这次处理。
- 回放前置检查的消息和退出码有测试覆盖的部分未变；未被覆盖的拒绝分支靠"文本逐字搬移"保证。
