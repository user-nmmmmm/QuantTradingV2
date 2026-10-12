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
