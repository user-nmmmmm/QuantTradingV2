# 架构决策记录（ADR）

这里记录**影响结构、工具链或研究完整性**的决定，以及当时为什么这样选。目的是让后来的人（包括未来的自己）知道哪些约束是有意为之、改动它们需要什么前提。

- 一个决定一个文件，编号递增，文件名 `NNNN-短标题.md`；模板见 [template.md](template.md)。
- 状态：`已采纳` / `已取代（见 NNNN）` / `已废弃`。不改写历史：想改决定就新增一条并把旧条标为已取代。
- 只记录取舍，不重复契约细节；精确行为写在对应的契约或模块文档里。
- 与统一 Roadmap 的关系：ADR 不维护阶段或任务状态，也不构成策略准入或实盘放行。

| 编号 | 决定 | 状态 |
|---|---|---|
| [0001](0001-editable-install-only.md) | 只支持可编辑安装，不支持分发 wheel | 已采纳 |
| [0002](0002-quality-ratchet.md) | 用"只减不增"的棘轮管理存量 lint/类型问题 | 已采纳 |
| [0003](0003-behavioral-source-identity.md) | 新增行为身份，不改写旧冻结记录 | 已采纳 |
| [0004](0004-layered-import-rules.md) | 用 import-linter 固定包的依赖方向，例外只减不增 | 已采纳 |
| [0005](0005-persisted-event-type-names.md) | 事件类型的持久化名称是线上格式 | 已采纳 |
| [0006](0006-delivery-archives-outside-git.md) | 历史交付 ZIP 放到 GitHub Release，不重写 Git 历史 | 已采纳 |
| [0007](0007-ops-package.md) | 运维/治理模块移到顶层包 `ops/`，不留旧路径垫片 | 已采纳 |
| [0008](0008-defer-forward-observation-window.md) | 前向观察窗顺延，重构完成后再登记新身份 | 已采纳 |
| [0009](0009-release-convention.md) | 发布约定：语义化版本、CHANGELOG、带注解的标签 | 已采纳 |
| [0010](0010-release-bound-identity.md) | 新冻结登记绑定"发布标签 + 行为身份回执"，旧记录不动 | 已采纳 |
| [0011](0011-signal-meta-package.md) | 信号元层移到顶层包 `signal_meta/`，不留旧路径垫片 | 已采纳 |
| [0012](0012-core-market-package.md) | 行情与时间/标的约定移到 `core/market/`（B2 第 1 簇） | 已采纳 |
| [0013](0013-core-trading-package.md) | 订单、保护单、账户与状态持久化移到 `core/trading/`（B2 第 2 簇） | 已采纳 |
| [0014](0014-core-planning-package.md) | 选币、打分与分配移到 `core/planning/`；`portfolio`、`valuation` 归入 `core/trading/`（B2 第 3 簇） | 已采纳 |
| [0015](0015-core-analytics-package.md) | 审计、诊断、健康与指标移到 `core/analytics/`（B2 第 4 簇） | 已采纳 |
| [0016](0016-core-foundation-package.md) | 基础件移到 `core/foundation/`（B2 第 5 簇，`core/` 平铺模块清零） | 已采纳 |
| [0017](0017-clear-layer-exceptions.md) | 清零分层规则的 4 条已知例外（B3） | 已采纳 |
| [0018](0018-mixins-to-collaborators.md) | 四个宿主类的 mixin 改为持有 host 引用的协作类（B4） | 已采纳 |
| [0019](0019-run-pipeline-cli-package-scripts-lifecycle.md) | 回测运行与命令行拆成流水线；入口移到 `cli/`；脚本按生命周期分类（B5） | 已采纳 |
| [0020](0020-frozen-research-inputs.md) | 冻结研究输入清单与下载缓存清单分离（D2） | 已采纳 |
| [0021](0021-locked-ci-and-dependabot-policy.md) | CI 从锁文件安装；Dependabot 只管 Actions（D3、D4） | 已采纳 |
| [0022](0022-mypy-hard-gate-list.md) | mypy 硬门槛清单：已零错误的模块不能回退（C3） | 已采纳 |
| [0023](0023-static-analysis-devcontainer-and-architecture-inventory.md) | CodeQL 启用、dev container 与平台矩阵的结论、自动生成的架构清单（D5、D6） | 已采纳 |
| [0024](0024-coverage-90-and-branch-coverage-evaluation.md) | 覆盖率门槛提到 90%；分支覆盖暂不设门槛（C5） | 已采纳 |
| [0025](0025-report-and-strategy-mixins.md) | 报告生成改协作类、Donchian 策略的三个私有 mixin 合成显式基类（B7） | 已采纳 |
| [0026](0026-engine-function-limits-and-typed-run-state.md) | 引擎与命令行的函数不超过 100 行；`_RunState`/`_MainState` 字段有具体类型（B8） | 已采纳 |
| [0027](0027-state-ownership-first-slice-circuit-breaker.md) | 状态所有权迁移的第一片：熔断器的 13 个状态属性搬进 `CircuitBreaker`，宿主用 `Delegated` 保留旧名（B6） | 已采纳 |
| [0028](0028-state-ownership-second-slice-broker-order-books.md) | 状态所有权迁移的第二片：`Broker` 的订单簿搬进 `Matching`、风控动作目标搬进 `Liquidation`（B6） | 已采纳 |
| [0029](0029-state-ownership-third-slice-live-account-sync.md) | 状态所有权迁移的第三片：`LiveBroker` 的 8 个账户同步状态属性搬进 `AccountSync`（B6） | 已采纳 |
| [0030](0030-state-ownership-fourth-slice-live-engine-recovery-and-export.md) | 状态所有权迁移的第四片：`LiveTradingEngine` 的 7 个对账与导出状态属性搬进 `Recovery`/`StateExport`（B6） | 已采纳 |
| [0031](0031-state-ownership-fifth-slice-tick-orchestrator.md) | 状态所有权迁移的第五片：`TickOrchestrator` 独占的 8 个 tick 循环记账属性搬进它自己；共享的引擎级状态留在引擎（B6） | 已采纳 |
