# SR1-HEALTH-006 健康冷却政策预注册研究（2026-11-10）

> 状态：**已登记，结果产生之前冻结**。本文件与 `scripts/run_health_policy_study.py` 的源码哈希
> 一并写入 `reports/health_policy_study_20261110/registration.json`；任何一项变化都会使脚本拒绝继续。

## 1. 动机与污染声明

- 2026-11-10 的复现消融（`reports/regime_exit_ablation_20261110`）显示 `TrendBreakout`
  在 validation20 / final20 两段中满仓时间只有 9% / 5%，冷却期内被挡掉的交易合计 +22R。
- 用这些交易的 R 分布做独立同分布模拟：期望 +0.2R 的策略平均仓位倍数仅 0.22，与期望 −0.3R
  的失效策略（0.12）区分度很弱；把连续亏损门槛从 3 提到 5，捕获的 R 约翻倍。
- **污染声明**：本假设是在看过 validation20 与 final20 结果之后提出的。因此这两段都不能用于
  选择。final20 是最终保留段，本研究在代码里禁止运行它；validation20 只用于对已选方案做一次确认。
- `consecutive_negative_cohorts=5` 不在 SR1-HEALTH-001 登记的候选 `[2, 3, 4]` 之内，所以单独
  登记为新实验，不改写旧登记。

## 2. 假设

H1：当前健康政策（连续 3 个亏损 cohort 触发冷却，且试用期要求“去掉最佳仍为正”）对
正偏态、低胜率的趋势策略误触发过多。放宽触发门槛或去掉该规则后，在 train60 上能提高
风险调整后收益，且最大回撤不会显著恶化。

H0：放宽后没有稳定改善，即当前冷却挡掉的主要是真正的不利行情。

## 3. 方案（固定，不做参数搜索）

只覆盖 `strategy_health` 中下列字段；其余配置、数据、成本、路由与风控全部与 `config/params.yaml` 一致。

| 方案 | 覆盖 | 可被选中 |
| --- | --- | --- |
| `h0_baseline` | 无 | 是（零假设的默认） |
| `h1_streak4` | `consecutive_negative_cohorts: 4` | 是 |
| `h2_streak5` | `consecutive_negative_cohorts: 5` | 是 |
| `h3_no_without_best` | `probation_require_positive_without_best: false` | 是 |
| `h4_streak5_no_without_best` | 上面两项同时 | 是 |
| `href_health_off` | `enabled: false` | **否，仅作参照** |

## 4. 数据与分段

- 冻结输入：`reports/strategy_review_20260919/frozen_inputs`（60 币 Binance 日线），
  分段取自同目录 `reference_protocol.json`。
- **选择段**：`train60` = 2017-08-17 至 2022-12-12。
- **确认段**：`validation20` = 2022-12-13 至 2024-09-20。只运行 `h0_baseline` 与被选中的方案。
- **禁止**：`final20`。脚本拒绝该分段。
- 期末强制按真实成本平仓（`forced=True`），与 SR P0/P1 研究一致。

## 5. 预注册判定规则

记 `ratio = return_pct / max_drawdown_pct`。`top5` 是去掉盈利最大的 5 笔平仓交易后的净平仓盈亏，
即 `net_closed_pnl_without_top_5`。`年度胜出` 指在 train60 覆盖的各完整/部分自然年中，
`annual_results.csv` 收益高于基线的年数占比。

**选择（只用 train60）**：方案必须同时满足以下条件才有资格：

1. `accounting_ok` 为真；
2. `max_drawdown_pct ≤ max(1.25 × 基线, 基线 + 3)`；
3. `top5 ≥ 基线 top5`，即改善不能只靠少数大单；
4. 年度胜出 ≥ 50%；
5. `ratio > 基线 ratio`。

在有资格的方案中选 `ratio` 最高的一个。如果没有方案有资格，结论就是 **维持现行政策**（接受 H0），
不再运行确认段。

**确认（validation20，只跑一次）**：被选方案的 `ratio ≥ 基线`，且 `top5 ≥ 基线`，才判为“确认”。

**结论的效力**：即使确认通过，也只说明该方案可以进入 SR5 独立样本外流程，**不修改
`config/params.yaml`，不改变 `strategy_governance` 中的 `paused_revalidation`**。

## 6. 已知局限

- 每段只有几十笔交易，单段结果噪声大。年度胜出与 top5 条件就是为此设置的。
- 健康机制与组合风控、回撤预算存在交互；本研究只衡量整体效果，不做逐项归因。
- train60 早期可交易币种较少，结论对 2017–2019 年的权重偏低。
