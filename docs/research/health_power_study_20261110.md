# SR1-HEALTH-007 健康机制统计效力：模拟（2026-11-10）

> 状态：**已登记，结果产生之前冻结**。本文件与 `scripts/run_health_power_study.py` 的哈希写入
> `reports/health_power_study_20261110/registration.json`；任何一项变化脚本都拒绝继续。
> 路线图：[策略整改 Roadmap](../strategy_remediation_roadmap_20261110.md) B5（问题 F-07 的补充）。

## 1. 问题

健康机制在连续 3 个亏损 cohort 后暂停开仓，再以缩小仓位试用、逐级恢复。对一个胜率低、
盈利集中在少数大单的趋势策略，好策略也常常连亏，于是机制可能把好坏策略一样地压住。
SR1-HEALTH-006（B1）选出 h3（去掉“去掉最佳仍为正”的试用规则），但确认段没有提供独立证据，
路线图要求先用本研究判断机制本身的区分能力。

**污染声明**：2026-11-10 代码审查时做过一次非正式模拟（+0.2R 平均仓位 0.22，−0.3R 为 0.12），
当时用的 R 分布包含 validation20 与 final20 的交易。本研究改为只用 train60 的 cohort，并把方法、
期望值网格和判定阈值固定在运行之前。

## 2. 方法

- **R 分布**：SR-EXIT-001 在当前代码上跑出的 `baseline_train60` 运行
  （`reports/time_exit_rebuy_study_20261110/runs/baseline_train60/strategy_health_cohorts.csv`，
  文件哈希写入登记）中 `counts_toward_health` 为真的 cohort 的 `r`。先去均值，再加上目标期望 μ，
  保留原分布的偏态与尾部。不读取 validation20 或 final20 的任何交易。
- **期望网格**：μ ∈ {−0.3, −0.2, −0.1, 0, +0.1, +0.2, +0.3} R / cohort。
- **到达**：每个允许开仓的自然日以概率 p 产生一个 cohort（cohort 按自然日归并，与 `health_cohort_id` 一致）。
  p = 计入 cohort 数 ÷ 同一运行中非冷却天数（由 `strategy_health_transitions.csv` 计算）。
  冷却期内不产生 cohort。
- **标的**：每个 cohort 从该运行出现过的标的中均匀抽一个（满足试用期的“不同标的数”要求）。
- **机器**：真实的 `StrategyHealthMachine`，每天评估一次，cohort 以 `initial_risk = 1`、
  `realized_pnl = R`、`exit_reason = "signal"` 写入。
- **方案**：`current`（`config/params.yaml` 的 `strategy_health`）与 `h3`
  （只改 `probation_require_positive_without_best: false`）。
- **规模**：每个（方案, μ）1000 条路径，每条 6 × 365 天，随机种子 20261110。
- **指标**（每个方案与 μ）：
  - 平均风险倍数（按天平均，冷却期为 0，等于按满仓计捕获的期望 R 比例）；
  - 满仓（ACTIVE）天数占比、冷却天数占比；
  - 第一年内至少触发一次冷却的概率（μ > 0 时即**误触发率**）；
  - 每年平均冷却次数。

## 3. 预注册判定规则

记 M(μ) 为平均风险倍数，F(μ) 为第一年内触发冷却的概率。

**区分能力合格**，当且仅当 `current` 同时满足：

1. M(+0.2) ≥ 0.60；
2. M(−0.3) ≤ 0.30；
3. F(+0.2) ≤ 25%。

**对 h3 的含义**（只在 `current` 不合格时适用）：若 h3 满足 M(+0.2) − M_current(+0.2) ≥ 0.10
且 M_h3(−0.3) ≤ 0.30，则 h3 列为阶段 C1 的健康维度候选（仍需新候选身份与 SR5 独立样本外评价）；
否则健康政策在阶段 C 不改。

本研究不改任何配置。

## 4. 已知局限

- 真实引擎只在出现原始入场信号的 bar 上评估健康状态，冷却到期后要等下一个信号才进入试用；
  本模拟每天评估，因此恢复速度偏快，M 偏高（对“合格”偏宽松）。
- cohort 独立同分布、平稳；真实交易有聚集与市场状态依赖，连亏会更集中，误触发率可能被低估。
- p 由一段运行估计，未考虑组合风控、回撤预算等其他门。
