# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本规则见 [docs/releasing.md](docs/releasing.md)。
每一版的标题必须是 `## [X.Y.Z] - YYYY-MM-DD`，由 `scripts/release_check.py` 检查。

这里只记录**对使用者或冻结范围有意义的变化**；逐项细节在 PR 与 [基础设施路线图](docs/infrastructure_roadmap.md)。
策略研究结论、准入状态与实盘放行不在本文维护，仍以[统一 Roadmap](docs/unified_roadmap.md)为准。

## [Unreleased]

### Changed
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
