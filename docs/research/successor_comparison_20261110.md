# SR5-SUCC-001 新旧候选回测对比与保留段单次评价（2026-11-10）

> 状态：**已登记，结果产生之前冻结**。本文件与 `scripts/run_successor_comparison.py` 的哈希写入
> `reports/successor_comparison_20261110/registration.json`；任何一项变化脚本都拒绝继续。
> 路线图：[策略整改 Roadmap](../strategy_remediation_roadmap_20261110.md) 阶段 D2。
> 候选：[C1/C2 影响说明](candidate_successor_c1_c2_20261110.md)。

## 1. 目的

比较批次 4 合并后的新候选与“去掉 C1 的同一代码”，并对新候选做路线图要求的 final20 **单次**评价。

**污染声明**：final20 已被 2026-10-02 与 2026-11-10 的回顾性研究看过（基线与健康政策方案，从未看过
`use_obv: false` 的 final20 结果）。因此本次 final20 结果**只能作回顾性证据**：它可以否决新候选，
不能证明新候选有效，也不能用于任何参数选择。train60 与 validation20 的数字已用于 SR-ALPHA-004 的选择，
这里重报只为对照，不是新证据。

## 2. 方案

| 方案 | 配置 |
| --- | --- |
| `parent` | 当前代码 + `trend_breakout.use_obv: true`（即去掉 C1） |
| `successor` | 当前代码 + `config/params.yaml` 原样（C1 生效） |

C2 只影响实盘取数，不进入回测，两方案相同。`parent` **不是**已登记的 v3.1.1 身份：v3.1.1 之后合并的其他
修改（#117 等）也在两方案中；本对比只隔离 C1。

## 3. 数据、分段与成本

- 冻结输入：`reports/strategy_review_20260919/frozen_inputs`（60 币 Binance 日线），分段取自同目录
  `reference_protocol.json`：`train60`、`validation20`、`final20`。
- 成本倍数：1 倍与 2 倍（手续费、滑点、价差、波动滑点、冲击、借贷利率、强平罚金同时放大，沿用 `run_revalidation60.run_one`）。
- 期末按真实成本强制平仓。
- **final20 只运行一次**：`result.json` 写出后脚本拒绝再运行。

## 4. 指标

收益 %、最大回撤 %、`ratio = 收益 / 回撤`、去掉盈利最大 5 笔后的净平仓盈亏（`top5`）、平仓交易数、平均仓位。

## 5. 预注册判定（只看 final20）

记 cap = max(1.25 × parent 回撤, parent 回撤 + 3)。新候选在 final20 上**未被否决**，当且仅当 1 倍与 2 倍成本下都满足：

1. `accounting_ok` 为真；
2. successor 回撤 ≤ cap；
3. successor `ratio` ≥ parent `ratio`；
4. successor `top5` ≥ parent `top5`。

- 任一不满足：结论 **reject_successor**：新候选不登记（D1 不做），C1 撤回到研究。
- 全部满足：结论 **not_rejected_retrospective**：可以进入 D1 登记新身份与新前瞻观察窗；
  这**不是**准入，`TrendBreakout` 仍为 `paused_revalidation`，真正的证据来自之后的前瞻观察（D3）。

## 6. 已知局限

- final20 已被看过，结论只能单向（否决或不否决）。
- 只比较 C1 一个维度，不评价策略整体是否值得交易。
- 单一交易所、单一周期（1d）；SR5 原始设想中的第二价格源、4h 与完整消融矩阵不在本次范围。
