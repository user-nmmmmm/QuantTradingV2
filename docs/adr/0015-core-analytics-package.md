# 0015 审计、诊断、健康与指标移到 `core/analytics/`（B2 第 4 簇）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B2，[ADR 0012](0012-core-market-package.md)、[ADR 0013](0013-core-trading-package.md)、[ADR 0014](0014-core-planning-package.md)

## 背景
前三簇之后，`core/` 根还剩 17 个平铺模块。其中 7 个是"对结果做检查与度量"的：回测审计、账务一致性检查、基准、诊断、带 JSON schema 的指标结果、策略健康状态机，以及技术指标。依赖图（含函数内延迟导入）显示它们只向下依赖 `core` 内少数模块，使用方是 `backtest`、`strategies`、`composition`、`signal_meta`、`analysis`、`scripts` 和测试。`core/metrics/`（1740 行的指标计算子包）已是独立子包，不在这一簇里。

## 决定
- 新建子包 `core/analytics/`（仍属 `core` 层，`__init__.py` 为空），搬入 7 个模块，**模块名不变**：`accounting_check`、`backtest_audit`、`benchmarks`、`diagnostics`、`metric_result`、`strategy_health`、`indicators`。`core.indicators` → `core.analytics.indicators`。
- 不留垫片；所有导入点和脚本里"登记源码清单"用到的路径一次性改写。
- 批量改写继续排除哈希固定的输入（`config/` 下非 `.py` 文件、`tests/fixtures/`）和冻结的历史证据；搬迁前检查了 `__file__`、`__module__`、pickle、动态导入，这些模块均未命中。

## 备选方案
- 并入 `core/metrics/`：`metrics` 是已有的、有门面的指标计算子包，把审计与健康状态机塞进去会混淆职责。
- 把 `strategy_health` 放进 `trading`：它是对策略行为的度量，不管理订单或账户。

## 后果
- 行为身份报告显示为"移动 7 个文件、若干文件仅导入变化"，没有行为变化。
- 这些模块的 logger 名随路径变化。
- `core/` 根平铺模块 17 → 10，剩下的是基础件（`alerting`、`clock`、`domain`、`live_safety`、`logger`、`redaction`、`reproducibility`、`runtime`、`runtime_identity`、`source_identity`），在第 5 簇处理。
