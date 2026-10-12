# 0011 信号元层移到顶层包 `signal_meta/`，不留旧路径垫片

- 状态：已采纳
- 日期：2026-10-12
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B1，[ADR 0004](0004-layered-import-rules.md)，[ADR 0007](0007-ops-package.md)，[ADR 0008](0008-defer-forward-observation-window.md)

## 背景
`core/` 里有 14 个只服务于"信号观察与元层研究"的模块：12 个 `signal_*`（观察、结果标签、期望值账本、状态模型、轴归因、自适应等）加上 `temporal_labels`、`temporal_financing`，约 3500 行。实测依赖图（含函数内延迟导入）显示：`core/` 里**没有任何**其他模块导入它们；导入方只有 `backtest/`、`main.py`、`scripts/` 和测试。它们自身只向下依赖 `core.market.timeframes`、`core.reproducibility`、`core.market.data_versions`、`core.market.universe`、`core.strategy_health` 和 `core.market.temporal_data`。`core.market.temporal_data` 还被行情层使用，必须留在 `core`。

## 决定
- 新建顶层包 `signal_meta/`，与 `ops` 同层（`ops | signal_meta`，互相独立），位于 `config` 之下、`core` 之上：可以导入 `core`，`core` 不得导入它；`ops` 与 `signal_meta` 也不得互相导入。import-linter 的分层契约、"core 不依赖其他包"契约和 `tests/test_architecture_boundaries.py` 均已加入。
- 搬迁 14 个模块，**模块名不变**，只换包名（`core.signal_observation` → `signal_meta.signal_observation`）。不在 `core/` 留垫片（垫片就是 `core → signal_meta` 的向上导入）。所有导入点一次性改写，质量基线的计数保持不变。
- **指纹保持可比**：`backtest/signal_meta_replay.py` 的 `_identity` 字典键沿用搬迁前的名字（`core/signal_adaptive_types.py` …），只改变读取的文件位置；`implementation_identity` / `adaptive_implementation_identity` 仍按同目录的同名文件集合计算。测试 `tests/test_signal_meta_package.py` 固定这些键和文件集合。指纹的**值**随源码字节变化（导入路径改了），这是这类"按源码哈希"的指纹本来的性质，不是新引入的问题。
- 冻结范围（`config/frozen_scope.json`）加入 `signal_meta/`，覆盖不会缩小；覆盖率与 mypy 范围加入 `signal_meta`。
- 未改动：冻结的历史证据（`docs/archive/2026-08*`、`2026-09*`、`docs/research/ml_selection_next_acceptance_20261005.json`、`docs/codex_mail_findings_20260920.json`、`docs/research/current_strategy_protocol.json`）以及记录当时文件哈希的 `docs/research/paper_continuation_acceptance_20261003.json`，里面的旧路径是当时的事实。

## 备选方案
- 作为 `core/` 的子包：不新增顶层包，但 `core` 内部没有可强制的边界，且无法表达"core 不依赖它"。
- 去掉文件名里的 `signal_` 前缀（`signal_meta.observation`）：更整齐，但与搬迁混在一个 PR 里会让评审和身份报告失去"纯移动"的性质；留给之后单独的重命名。
- 在 `core/` 留垫片：多一轮清理，并违反刚建立的分层规则。

## 后果
- 冻结身份的逐字节哈希因文件移动而变化；行为身份报告显示为"移动 14 个文件、5 个文件仅导入变化"（行为变化只有回放指纹的文件映射和 `frozen_scope.json`）。
- `docs/signal_meta_layer.md` 等现行文档里的命令和路径已同步；历史记录里仍是旧写法。
- 这是 V4.0 的第二步；同一方法用于 B2 中其余 `core` 簇。
