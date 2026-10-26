# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本规则见 [docs/releasing.md](docs/releasing.md)。
每一版的标题必须是 `## [X.Y.Z] - YYYY-MM-DD`，由 `scripts/release_check.py` 检查。

这里只记录**对使用者或冻结范围有意义的变化**；逐项细节在 PR 与 [基础设施路线图](docs/infrastructure_roadmap.md)。
策略研究结论、准入状态与实盘放行不在本文维护，仍以[统一 Roadmap](docs/unified_roadmap.md)为准。

## [Unreleased]

### Added

- PR25-00/01/02 (paper research programme, batch B1): hashed paper literature registry built from the Excel workbook (L01-L08 presence computed, only L02 and L04 are in the table), offline research baseline capture, and a search-scope / source-closure audit for `ResearchEvidenceRun` records. Research-only tooling; no strategy, admission or live behaviour changes. Plan: `docs/research/paper_research_excel_integration_plan_20261026.md`.

## [3.1.1] - 2026-10-17

Patch release preparation for the fixes in #99–#106, the release baseline in #107, the v2 data review and approved adoption in #108/#109, the earlier documentation closeout in #110, and the selector/archive integrity and dependency updates in #111–#113. The [repair baseline and owner runbook](docs/repair_release_baseline.md) links regressions, historical-evidence treatment and the release/tag/binding sequence. After this closeout merges, select the final main commit and verify its complete CI before any separately authorized release action. No v3.1.1 tag or new release binding is created here; A5 remains deferred and AUD-07 external evidence remains open.

### Added

- AUD-03: retain the separate v2 review and historical unsigned draft, then bind the approved adoption to its actual data commit with a new [technical receipt](docs/data_review/20261016_adoption_receipt.json). Offline local/CI gates check inputs, evidence, receipt and research policy together ([adoption record](docs/data_review/20261016_adoption.md), [ADR 0041](docs/adr/0041-approved-data-adoption-and-research-window.md)).
- AUD-07: link private vulnerability reporting directly, document owner notification/review checks, and generate a read-only per-ref/SHA/worktree inventory in ignored local storage (ADR 0040).

### Changed

- AUD-03: adopt the approved daily prefixes in #109. Trim 27 CSVs through 2026-08-26 and retain MATIC through 2024-09-09, separately accounting for 83 unmatched dates and one matching delisting-day bar. BTC/ETH raw downloads and the original download cache remain unchanged; the local research loader enforces a 2026-08-26 end for the other 29 inputs and 2024-09-09 for MATIC, including the DataVersionStore path. The adoption removes 111 raw rows; 67,266 adopted raw rows yield 67,218 research-eligible rows across the approved ranges.
- AUD-06: align dependency and Python/Node instructions with the installed project and CI matrix; replace stale module paths with checked navigation, scan reference-style Markdown links, and restore historical report entry points without rewriting frozen evidence (ADR 0039).
- Remove the unused Streamlit dependency subtree and explicitly pin the HTTP, signing and image dependencies used by the runtime (#112). Add real-library HTTP, WebSocket, signing and report regressions to the installation matrix.
- Refresh Requests to 2.33.1, Soup Sieve to 3.0.3, Multidict to 6.9.1 and IDNA to 3.15 (#113). Pin hostname and header dependencies directly for editable installs; regenerate the Linux/Python 3.11 lock, keeping its other 46 versions unchanged. Extend Unicode hostname, native/Python header-container and selector compatibility checks.
- Require Python 3.11.5 or newer for Soup Sieve 3.0.3, with matching package metadata, environment checks and installation guidance. Environments using Python 3.11.0–3.11.4 need a Python patch update before installation.
- Consolidate the public engineering roadmap from #98 through #101, then record the merged #99–#113 fixes, data adoption, current-main CI, final-release checks and separately pending owner operations. Retain all branches and worktrees; inventories and governance receipts remain local.

### Fixed

- Bind frozen selector inference, receipt hashes and replay snapshots to the same captured manifest/model bytes; reject nonportable and case-aliased package member paths (#111). Pin historical delivery archive identities independently of the editable manifest and reject missing or rewritten records even without local ZIP copies. Align V4 draft identity/window guidance with current contracts ([ADR 0042](docs/adr/0042-captured-selector-bytes-and-retained-archive-identities.md)).
- AUD-05: preserve zero/negative equity, real report endpoints and drawdowns beyond 100% losses. Metrics formula 2.1 shares positive-denominator return handling across headline/risk/monthly statistics, charts and Excel; undefined intervals remain visible instead of being dropped or filled with zero. Catchup and regular fetches share derivative client aliases, and report section/Broker time hooks dispatch through the host subclass (ADR 0038).
- AUD-04: refuse baseline updates that introduce or increase any finding, including mypy hard-gate failures; rejected updates leave the file unchanged. Local and CI quality checks share developer tasks with the 90% coverage gate and dashboard tests. Quarantine collection counts expanded cases and enforces a 10-case / 30-UTC-day limit, and `pyproject.toml` changes trigger container CI for PRs and main pushes (ADR 0037).
- AUD-03: bind corroboration to the bytes actually read, reject stale or changing inputs and unknown filenames, validate partial-tail OHLCV invariants, and distinguish successful empty data from confirmed invalid symbols. New cache records declare LF-normalized hashes; legacy cache verification and incremental refresh accept newline equivalence without relaxing content or source checks (ADR 0036).
- AUD-02: cover independent research CLI/runtime and installation inputs, include `pyproject.toml` in frozen scope, and require behavioral review for import-only edits and moves. New `code-token/v2` identities normalize f-string expression layout while preserving observable text and formatting; saved v1 receipts and bindings retain their original verifier (ADR 0035).
- AUD-02: isolate frozen historical replay imports and record each module's source. Identity CLI scope comes from the selected head, references are pinned, and release verification refuses missing main references or failed ancestry checks ([ADR 0034](docs/adr/0034-frozen-replay-and-reference-verification.md)).
- AUD-01: serialize release-bound registration and prospective-sample opening with a shared process lock. Build complete artifacts in a sibling temporary directory, recheck parent/registry/opening evidence and publish atomically. Competition, interruption and retry regressions prevent incomplete formal directories and refuse overwrites ([ADR 0033](docs/adr/0033-atomic-bound-registration.md)).

### Notes

- New registration requires a fresh `code-token/v2` binding to the final release commit. The adopted loader boundary, #111 selector implementation and #112/#113 dependency/package metadata change frozen identity. Existing tags, v1 bindings, historical CSVs, review drafts and protocol records retain their original identities; old bindings cannot cover these later changes.
- The adopted prefixes match the captured public responses under v2 comparison. Source evidence was observed on 2026-10-15; replay does not establish historical availability. Two download records, 28 `unverified` origins and all `point_in_time: unknown` statuses remain unchanged. Technical adoption is not strategy validation or admission to trade.
- This closeout prepares the 2026-10-17 release entry without creating a tag or starting A5. If publication occurs on a later date, update this entry before selecting and validating the final release commit. Full audits, governance and registration materials remain local.

## [3.1.0] - 2026-10-14

结构整理的后续：B6 状态所有权迁移（协作类拥有自己的状态，宿主用 `Delegated` 保留旧名）、A5 的登记工具，以及 2026-10-12 之后的质量、数据与环境工作。冻结范围的行为身份相对 `v3.0.0` 有变化（`core/broker/`、`core/live_broker/`、`live_trading/` 中 9 个文件），所以新的冻结登记要绑定这个标签而不是 `v3.0.0`（[ADR 0032](docs/adr/0032-bound-successor-registration.md)）。交易与回测语义不变。

### Notes
- 路线图 A5 工具（ADR 0032）：新增 `scripts/register_bound_successor.py`，把新的冻结登记绑定到带注解的发布标签及其行为身份（`release-bound-identity/v1`），配置哈希取标签处 `config/params.yaml` 的 LF 规范化 SHA-256；配置变更必须附 `config-change-statement/v1` 声明（`docs/research/capital_config_change_statement_20261014.json`），脚本核对父配置与标签处配置的差别恰好等于声明的新增项且每项默认关闭。旧脚本 `register_strategy_successor.py` 与旧登记的校验不变。**登记本身尚未执行**，需要先发 `v3.1.0`（`v3.0.0` 早于 B6，不覆盖现在的代码）。
- 路线图 B6 第五片（ADR 0031）：`TickOrchestrator` 独占写入的 8 个 tick 循环记账属性（`_tick_count`、`_consecutive_tick_crashes`、`_next_retry_delay`、`_consecutive_strategy_failures`、`_last_strategy_error`、`_last_tick_seconds`、`_alerted_health_transitions`、`_live_fill_risk_audit`）搬进它自己；`LiveTradingEngine` 与测试替身 `TickOrchestratorHost` 用 `Delegated` 保留旧名字，外部读写不变。`_alerted_health_transitions` 从构造起就是空集合（以前首次告警时才创建）。引擎、`runtime_controls` 和状态导出共同写入的状态（`_operational_state`、`_healthy`、`data_map` ……）留在引擎上。迁移前录制的金样本测试（`tests/test_tick_state_ownership.py`）一字不改地通过。
- 路线图 B6 第四片（ADR 0030）：`LiveTradingEngine` 的 7 个对账与导出状态属性（`_reconciliation_status`、`_last_reconciliation_at`、`_last_order_sync_at`、`_unresolved_unknown_cache`、`_last_export_tick` ……）搬进 `Recovery` 和 `StateExport`；引擎用 `Delegated` 保留旧名字，外部读写不变；`_account_reconciliation_persistence_failed` 仍然是“未绑定直到首次账户对账”，`getattr(…, False)` 的探测语义不变。迁移前录制的金样本测试（`tests/test_live_engine_state_ownership.py`）一字不改地通过。
- 路线图 B6 第三片（ADR 0029）：`LiveBroker` 的 8 个账户同步状态属性（`account_reconciliation_report`、`unowned_positions`、`last_account_sync_at`、`_account_source` ……）搬进 `AccountSync`；`LiveBroker` 用 `Delegated` 保留旧名字，外部读写不变；其中六个属性仍然是“未绑定直到配置或同步”，`getattr(…, None)` 的探测语义不变。迁移前录制的金样本测试（`tests/test_live_broker_state_ownership.py`）一字不改地通过。
- 路线图 B6 第二片（ADR 0028）：模拟撮合器 `Broker` 的 `pending_orders`、`active_orders` 搬进 `Matching`，`risk_action_targets` 搬进 `Liquidation`；`Broker` 用 `Delegated` 保留旧名字，外部读写不变。`risk_action_targets` 首次使用前读到 `{}` 而不是 `AttributeError`。迁移前录制的金样本测试（`tests/test_broker_state_ownership.py`）一字不改地通过。
- 棘轮复核（C2）：借贷利息的年化只在 `_accrue_short_borrow` / `_accrue_quote_borrow` 里计算，`financing.py` 里原有的 `seconds_per_year` 局部变量是未使用的死代码，已合并为 `SECONDS_PER_YEAR` 并用测试固定；29 处"闭包里使用循环变量"逐一确认无害并显式绑定。这些改动在冻结范围内，但交易与回测行为不变（身份报告会显示为行为变化，因为字节变了）。

### Fixed
- `DataVersionStore` 并发写入同一个不可变对象时，Windows 上读取刚被另一线程换到位的文件可能短暂抛出 `PermissionError`（负载下约 3/40 次使 `test_concurrent_identical_writes_publish_complete_objects_once` 失败）。现在这一处读取在 Windows 上有界重试（5 次，10–40 毫秒），内容不同仍报"immutable content conflict"，持续拒绝和其他平台照常抛出。存储的字节与语义不变；文件在冻结范围内，身份报告会显示为行为变化（仅 Windows 重试）。

### Added
- `devcontainer` 工作流（D5 收尾）：用 `devcontainers/ci` 构建 `.devcontainer/`，并在容器里运行 `python scripts/dev.py check --no-tests`；容器输入变化时、每周一次和手动触发时运行，不是必需检查。首次运行通过。
- 函数长度棘轮（B8 收尾，ADR 0026 后续）：`quality_ratchet.py` 新增 `function_length` 工具，>= 100 行的函数按文件::函数记录行数（现有 87 个），只许变短，新增的长函数让 CI 失败；引擎与命令行里为 0。
- D2 收尾：`scripts/data_inputs.py corroborate` 联网比对每个文件与 Binance 公共日线，结果作为绑定文件摘要的 `corroboration` 块写进清单（`verify` 检查）。2 个完全一致，27 个只差最后一根日线（那一根是未收盘的半根 K 线），`MATIC_USDT.csv` 多出 83 行接口没有的日期；数据文件未改动，见 `docs/data_identity.md`。
- 冻结研究输入清单（路线图 D2，ADR 0020）：`data/binance/1d/_frozen_inputs.json` 为 30 个已跟踪的 CSV 登记行数、范围与 SHA-256（CRLF 规范化为 LF），并如实标注来源核验状态（2 个有下载记录，28 个未核验）；`scripts/data_inputs.py verify` 在文件缺失、被替换或未登记时失败，已加入 CI 与 `dev.py check`。说明见 `docs/data_identity.md`。
- 锁文件再生成脚本（D3，ADR 0021）：`scripts/regenerate_lock.py`（`--check` 离线核对规范形式、摘要与直接依赖）；CI 改为 `pip install -r requirements.lock.txt -r requirements-dev.txt` 后运行 `check_environment.py --strict-lock`。
- mypy 硬门槛清单（C3，ADR 0022）：`scripts/mypy_hard_gate.txt`（128 个零错误模块），`quality_ratchet.py check` 对其中任何发现直接失败，`promote` 把新达标的模块收进清单。
- 自动生成的架构清单（D6，ADR 0023）：`docs/architecture_inventory.md` 与 `scripts/architecture_inventory.py --check`；`架构图/path_moves.json` 记录交付图所引用的 16 个已搬迁路径。
- 覆盖率门槛 85% → 90%（C5，ADR 0024），同时为一批没有直接测试的运维与恢复工具补了行为测试：事故日志、进程监督、Telegram 心跳、R7/Phase 6 失败即关闭入口、订单状态归一与下单异常分类、SQLite 快照/恢复守卫、补帧与冷却回放、入场闸门的各项拒绝原因、公共行情缓冲的校验。
- CodeQL 工作流（D5，ADR 0023）：Python 与 JavaScript，每周与 PR 运行，不是必需检查。
- 测试治理（路线图 C4）：`quarantine` 标记（带原因和到期日，过期后 CI 失败）、`tests/capabilities.py` 的能力探测跳过（Windows 上没有符号链接权限时跳过，Linux CI 保证不跳过）、完整套件的耗时预算（目标 ≤ 5 分钟，CI 步骤 8 分钟超时），约定写在 `docs/quality.md`。

### Changed
- 路线图 B6 第一片（ADR 0027）：熔断器的 13 个状态属性（`blocked_until`、`probation_equity`、`recovery_count`、`high_water_equity`、`portfolio_breaker_action`、`breaker_epoch` ……）从 `RiskManager` 搬进 `CircuitBreaker`；`RiskManager` 用新的描述符 `core/risk/delegation.py::Delegated` 保留全部旧名字，外部读写不变。迁移前在原代码上录制的金样本测试（`tests/test_breaker_state_ownership.py`）一字不改地通过。mypy 302 → 301，`circuit_breaker.py` 与 `delegation.py` 进入硬门槛清单（136 → 138）。
- C3 收尾：11 处纯类型注解（局部容器变量、默认值为 `None` 的参数），不改变任何语句的行为；mypy 存量 313 → 302，7 个模块进入硬门槛清单（129 → 136）。另有两处（`backtest/delayed_execution.py`、`backtest/signal_ghost.py`）的注解需要把元组赋值拆成多行，会让它们本来就超长的函数更长，被函数长度棘轮拒绝，所以没有做。
- 路线图 B8（ADR 0026）：`BacktestEngine.__init__`（149 行）、`_close_tail_positions`（118 行）和 `cli/replay.py::replay_manifest`（131 行）拆成按原顺序调用的小函数，语句、消息和校验顺序不变（回放前置检查改为内部异常 `_ReplayRefused`，消息与退出码 7 不变）；`_RunState` 与 `_MainState` 的 74 个字段全部换成具体类型，结构测试固定。
- 报告生成与 Donchian 策略里最后的 mixin 清零（路线图 B7，ADR 0025）：`ReportGenerator` 不再继承 `ReportMetricsMixin` 等四个类，改为惰性持有 `ReportMetrics`、`TradeReconstruction`、`ReportWriters`、`ReportCharts` 并保留全部同签名转发方法（原先单独使用 `TradeReconstructionMixin` 的调用方改用 `TradeReconstruction`）；`strategies/trend_breakout.py` 的三个私有 mixin 合成显式基类 `DonchianTrendStrategy`。行为不变；mypy 存量 326 → 313，该文件进入硬门槛清单。
- `requirements.lock.txt` 改为规范形式（两行表头、`GitPython`/`gitdb` 调换顺序）并更新摘要，**没有任何版本变化**；锁文件在冻结范围内，身份报告会显示这一处变化（ADR 0021）。
- Dependabot 的 GitHub Actions 更新改为每月一次、小版本与补丁合并成一个分组 PR、主版本单独成 PR；pip 更新仍不启用（ADR 0021）。
- `.devcontainer` 的 `postCreateCommand` 与 `dev.py setup` 改为同 CI 一致的安装路径；`.gitignore` 放行 `data/binance/1d/_frozen_inputs.json`。
- `BacktestEngine.run`（838 行）拆成 4 个准备阶段、每根 K 线 5 步、4 个收尾阶段和 2 个结果片段，`run` 只剩驱动；`main()` 拆成 10 个阶段，入口实现从 `main.py` 移到新包 `cli/`（`main.py` 剩 5 行，`quant-backtest` 入口不变；导入 `main` 里的私有函数改为 `cli.data` / `cli.replay` / `cli.run`）；`scripts/classification.json` 给 100 个脚本登记生命周期并由测试检查（ADR 0019）。
- `Broker`、`LiveBroker`、`RiskManager`、`LiveTradingEngine` 的 13 个 mixin 改为持有 `host` 引用的协作类（`Matching`、`FillService`、`CircuitBreaker`、`OrderReconciler`、`TickOrchestrator` …），宿主惰性持有它们并保留同签名的转发方法，外部调用不变；mypy 存量 869 → 404（ADR 0018）。状态仍由宿主持有。
- 分层规则不再有已知例外（ADR 0017）：`paper_labels`、`paper_label_execution`、`label_account_reconciliation` 从 `analysis/` 搬到 `signal_meta/`；`create_selector`、`restore_selector` 从 `backtest.coin_selector` 搬到 `research.ml_selection.serving`，导入路径随之改变。
- 10 个基础件模块从 `core/` 根移到 `core/foundation/`，导入路径随之改变（`core.domain` → `core.foundation.domain`）；CI 的 mypy 目标、`scripts/dev.py` 同步更新；不留兼容垫片（ADR 0016）。至此 `core/` 根没有平铺模块。
- 7 个审计、诊断、健康与指标模块从 `core/` 根移到 `core/analytics/`，导入路径随之改变（`core.indicators` → `core.analytics.indicators`）；不留兼容垫片（ADR 0015）。
- 7 个选币、打分、分配与目标仓位模块从 `core/` 根移到 `core/planning/`，`portfolio`、`valuation` 并入 `core/trading/`，导入路径随之改变（`core.allocation` → `core.planning.allocation`，`core.portfolio` → `core.trading.portfolio`）；不留兼容垫片（ADR 0014）。
- 20 个订单、保护单、账户与状态存储模块从 `core/` 根移到 `core/trading/`，导入路径随之改变（`core.order_store` → `core.trading.order_store`；命令 `python -m core.sqlite_backup` → `python -m core.trading.sqlite_backup`）；不留兼容垫片（ADR 0013）。
- 17 个行情与时间/标的模块从 `core/` 根移到 `core/market/`，导入路径随之改变（`core.data_fetcher` → `core.market.data_fetcher`）；不留兼容垫片（ADR 0012）。已登记的纸面观察实验因源码路径改变不能再 `--resume`，需登记新协议。
- 14 个信号观察/元层模块从 `core/` 移到顶层包 `signal_meta/`，导入路径随之改变（`core.signal_observation` → `signal_meta.signal_observation`）；不留兼容垫片（ADR 0011）。

## [3.0.0] - 2026-10-12

首个带标签的版本：把 2026-10 基础设施整理（路线图第 2 节）之后的 `main` 固定为后续整理与新身份登记的基线。
**它不是新的冻结候选**：前向观察窗已按 [ADR 0008](docs/adr/0008-defer-forward-observation-window.md) 顺延，新身份将在结构整理完成后另行登记。

### Added
- 行为身份 `code-token/v1`、PR 影响报告（`scripts/identity_report.py`），并能把文件移动、仅导入路径变化与真正的行为变化分开报告（ADR 0003）。
- 可编辑安装与控制台入口（`quant-backtest`、`quant-live` 等）、`install-smoke`（Linux/Windows × Python 3.11/3.13）。
- ruff/mypy 棘轮、覆盖率门槛、import-linter 分层规则、`scripts/dev.py check`。
- 顶层包 `ops/`：准入、灰度、健康、预检、事故记录等运维/治理模块（ADR 0007）。
- 发布约定：语义化版本、本文件、`scripts/release_check.py`（ADR 0009），以及"标签 + 行为身份回执"的绑定记录（ADR 0010）。

### Changed
- 12 个运维/治理模块（15 个文件）从 `core/` 移到 `ops/`，导入路径随之改变（`core.health` → `ops.health`）；不留兼容垫片。
- 交付 ZIP 迁到 GitHub Release，不再放在仓库里（ADR 0006）。

### Notes
- 配置：`config/params.yaml` 在 2026-10-04 新增了默认关闭的 `capital:` 块，因此它不再等于 `followup-successor-03` 登记的配置身份（见 ADR 0008）。
