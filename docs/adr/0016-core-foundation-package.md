# 0016 基础件移到 `core/foundation/`（B2 第 5 簇，`core/` 平铺模块清零）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B2，[ADR 0012](0012-core-market-package.md)、[ADR 0013](0013-core-trading-package.md)、[ADR 0014](0014-core-planning-package.md)、[ADR 0015](0015-core-analytics-package.md)

## 背景
前四簇之后，`core/` 根只剩 10 个平铺模块，都是被其他簇和外部包广泛使用的基础件：领域类型（`domain`）、运行时装配（`runtime`）、日志、时钟、告警、实盘安全、脱敏、可复现性工具、运行时身份和源码身份（`source_identity`）。它们不属于前四簇的任何一个，也不该拆开。

## 决定
- 新建子包 `core/foundation/`（仍属 `core` 层，`__init__.py` 为空），搬入 10 个模块，**模块名不变**：`alerting`、`clock`、`domain`、`live_safety`、`logger`、`redaction`、`reproducibility`、`runtime`、`runtime_identity`、`source_identity`。`core.domain` → `core.foundation.domain`。
- 不留垫片；所有导入点一次性改写。`core/` 根此后只有 `__init__.py`，由 `tests/test_foundation_package.py` 固定。
- 路径写进配置的地方同步：CI 的 mypy 步骤、`scripts/dev.py` 的 `TYPED_FILES`、`scripts/main_acceptance.py`、文档里的命令；测试固定这些目标文件存在且与 CI 一致。
- **持久化类型名不跟着搬**（ADR 0005）：定义在 `domain` 里的可序列化类型（`FillRecord`、`OrderSubmissionResult`、`SyncResult`、`OrderStatus`、`OrderErrorCode`、`OrderIntent` …）的存储名按 `__module__` 生成，搬迁会把 `core.domain:…` 变成 `core.foundation.domain:…`，使已存事件无法解码、幂等比较失效。`core/events/codec.py` 新增 `_WIRE_MODULES = {"core.foundation.domain": "core.domain"}`，凡定义在该模块里的类型（含 `OrderIntent`、`RiskDecision` 等事件载荷类型）存储名仍用 `core.domain`；测试固定。这个问题由 `tests/test_event_performance_contracts.py` 的线上哈希测试在搬迁时发现。
- 批量改写继续排除哈希固定的输入和冻结的历史证据，另加 `docs/baseline/`（受保护的历史基线归档，`verify_baseline_archive.py` 校验它）。

## 备选方案
- 把 `domain`、`runtime` 留在 `core/` 根：与"根上没有平铺模块"的目标矛盾，也会让根上的内容无法再用测试固定。
- 拆成 `core/common/`、`core/safety/` 等更小的包：10 个模块互相引用，拆细只会增加跨包导入。

## 后果
- 行为身份报告显示为"移动 10 个文件、大量文件仅导入变化"，没有行为变化。
- 源码身份与发布绑定的实现路径变为 `core/foundation/source_identity.py`（见 ADR 0003、0010）。
- 登记源码清单的实验脚本（`start_paper_observation`、`run_p0_attribution_experiment` 等）里的路径随之更新；这些实验的既有登记因源码字节改变而不能再续（见 ADR 0012）。
- B2 完成：`core/` 平铺模块 78 → 0（B1 搬出 14 个到 `signal_meta/`，其余分入 `market`、`trading`、`planning`、`analytics`、`foundation`）。
