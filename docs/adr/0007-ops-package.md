# 0007 运维/治理模块移到顶层包 `ops/`，不留旧路径垫片

- 状态：已采纳
- 日期：2026-10-12
- 相关：V4.0 第一步，[V4 设计草案](../v4_architecture_design.md)，[ADR 0004](0004-layered-import-rules.md)

## 背景
`core/` 有 97 个单元、约 3.2 万行，混着交易内核、行情适配、指标，以及一批只服务于准入与运维的模块。实测依赖图（含函数内延迟导入）显示：准入/灰度/健康策略/预检/事故记录等 12 个模块在 `core` 内部**没有任何非同组的导入方**，只被 `live_trading`、`run_live.py`、`dashboard`、脚本和测试使用；而 `alerting`、`live_safety`、`runtime_identity`、`redaction`、`sqlite_backup` 被 `live_broker`、`risk`、`order_store` 等内核模块导入，不能搬。

## 决定
- 新建顶层包 `ops/`，分层位置在 `config` 之下、`core` 之上：可以导入 `core`，`core` 不得导入它（import-linter 的分层契约和"core 不依赖其他包"契约都已加入 `ops`）。
- 搬迁 12 个模块：`admission_gates`、`r7_acceptance`、`gray_release`、`health`、`strategy_governance`、`startup_preflight`、`supervisor`、`telegram_heartbeat`、`incident_journal`、`operations/`（包）、`status_snapshot`、`strategy_registration`。模块名不变，只换包名（`core.health` → `ops.health`）。
- **不在 `core/` 留兼容垫片**：垫片本身就是 `core → ops` 的向上导入，会违反刚建立的规则。所有导入（约 90 处，含测试和 `mock.patch` 目标字符串）一次性改写；文档、质量基线的路径同步更新，质量基线的计数保持不变。
- 冻结范围（`config/frozen_scope.json`）加入 `ops/`，覆盖不会因搬迁而缩小；覆盖率与 mypy 范围加入 `ops`。
- 未改动：冻结的历史证据文件（`docs/research/ml_selection_next_acceptance_20261005.json`、`docs/codex_mail_findings_20260920.json`、`docs/research/current_strategy_protocol.json`、`docs/archive/2026-08*`、`2026-09*`）里的旧路径保持原样，它们记录的是当时的事实。

## 备选方案
- 作为 `core/` 的子包：不新增顶层包，但 core 内部没有可强制的边界。
- 在 `core/` 里留垫片并登记为到期的例外：多一轮清理，且让例外清单变长。

## 后果
- 运行手册里的 `python -m core.admission_gates` 等命令变成 `python -m ops.admission_gates`，已同步到现行文档；历史记录里仍是旧写法。
- 冻结身份的逐字节哈希会因文件移动而变化（行为身份报告显示为删除 + 新增，内容未改）。
- `ops/` 之后若要再拆（例如 `core.foundation.alerting` 随 `live_broker` 重构迁出），按同样的依赖图方法评估。
- 同一方法将用于 `signal_meta/`（信号元层，14 个模块）。
