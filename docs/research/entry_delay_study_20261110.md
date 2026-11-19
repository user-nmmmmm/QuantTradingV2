# SR-ALPHA-004 入场确认层的边际贡献（2026-11-10）

> 状态：**已登记，结果产生之前冻结**。本文件与 `scripts/run_entry_delay_study.py` 的哈希写入
> `reports/entry_delay_study_20261110/registration.json`；任何一项变化脚本都拒绝继续。
> 路线图：[策略整改 Roadmap](../strategy_remediation_roadmap_20261110.md) B3（问题 F-02）。
> 复用已登记的 SR-ALPHA-002（`use_obv`）与 SR-ALPHA-003 的 `stability_period` 维度；
> 路由冷却 `router.cooldown_bars` 此前没有登记，在本实验中新登记。

## 1. 问题

`TrendBreakout` 入场前要过多层确认：状态机（ADX、均线结构、`stability_period` 根稳定确认）、
路由切换后的 `cooldown_bars` 根冷却、20 日突破、OBV 同向。每币约 6 年只有 8–14 笔交易，
健康与准入的统计效力都很弱（B5）。本研究逐个放宽其中三层，看每一层是否值得保留。

**污染声明**：validation20 已在 SR1-HEALTH-006 等研究中被查看过（只看过基线与健康政策方案，
未看过本研究的任何方案）。final20 被脚本拒绝。

## 2. 方案（一次只改一个维度，其余与 `config/params.yaml` 一致）

| 方案 | 覆盖 | 维度 |
| --- | --- | --- |
| `b0_baseline` | 无（`stability_period = 5`、`cooldown_bars = 2`、OBV 开） | — |
| `obv_off` | `research.strategy_ablation: no_obv_confirmation` | OBV（SR-ALPHA-002） |
| `stab2` | `state.stability_period: 2` | 稳定期（SR-ALPHA-003） |
| `stab3` | `state.stability_period: 3` | 稳定期 |
| `stab10` | `state.stability_period: 10` | 稳定期（收紧方向，作对照） |
| `cool0` | `router.cooldown_bars: 0` | 路由冷却（新登记） |
| `cool1` | `router.cooldown_bars: 1` | 路由冷却 |

## 3. 数据与分段

- 冻结输入：`reports/strategy_review_20260919/frozen_inputs`（60 币 Binance 日线），分段取自同目录
  `reference_protocol.json`。所有方案都跑 `train60` 与 `validation20`，期末按真实成本强制平仓。**禁止 `final20`**。

## 4. 指标

`ratio = return_pct / max_drawdown_pct`；`top5` = 去掉盈利最大 5 笔后的净平仓盈亏
（`net_closed_pnl_without_top_5`）；另报平仓交易数（`closed_trades.csv` 行数）、平均仓位。

## 5. 预注册判定规则

方案 X 相对基线**通过**，当且仅当在 train60 **和** validation20 两段都满足：

1. `accounting_ok` 为真；
2. `max_drawdown_pct ≤ max(1.25 × 基线, 基线 + 3)`；
3. `top5 ≥ 基线 top5`；
4. `ratio > 基线 ratio`。

- 某个维度有通过的方案，就把该维度列为阶段 C1 候选，取该维度内 validation20 `ratio` 最高的方案。
- 多个维度都有候选时，C1 只取 validation20 `ratio` 相对基线提升最大的**一个**维度（一次只改一个维度）；
  其余留待之后各自的新候选周期。
- 没有方案通过：结论是“维持现行确认层”，阶段 C1 在入场维度为空。
- `stab10` 是收紧方向的对照，可以通过，但只用于判断方向，不作为放宽候选。
- 本研究不改配置；C1 的修改需要新候选身份与 SR5 独立样本外评价。

## 6. 已知局限

- 每段只有几十到一百多笔交易，单段噪声大；两段都要通过就是为此设置的。
- 稳定期同时影响状态机的所有出入场，不只是入场延迟；本研究衡量整体效果，不做逐项归因。
- 放宽确认层会提高交易频率，`top5` 与回撤上限用来防止“只靠多交易摊薄”。
