# R3 参数稳健性（2026-11-16）

> 状态：**草案，未冻结，无结果**。共同约束见[总纲](backtest_research_program_20261116.md)。
> 依赖：R1（通过的对象）、R4（基准成本）。计划脚本：`scripts/run_backtest_r3_parameter_robustness.py`（本批不含）。
> 工具：[`parameter_plateau`](../../analysis/research_validation.py)、`deflated_sharpe_ratio`、[`search_scope`](../../analysis/search_scope.py)。

## 1. 问题

策略的表现是落在一片参数高原上，还是依赖某个尖峰？一个只在单点有效的参数，不能当作稳定的优势。
H0：表现随参数微小移动而显著恶化，或在扣除搜索规模后不再显著。

## 2. 网格（冻结时写死，二维）

| 对象 | 维度 X | 维度 Y | 格点 |
| --- | --- | --- | --- |
| S1 | `entry_window` ∈ {10, 15, 20, 30, 40, 55, 80} | `exit_window` ∈ {5, 7, 10, 15, 20} | 仅 `exit < entry`，约 28 格 |
| S2 | `target_annual_volatility` ∈ {0.06, 0.08, 0.10, 0.15, 0.20} | `initial_atr_multiple` ∈ {1.5, 2.0, 2.5, 3.0} | 20 格 |
| S3 | `atr_threshold_pct` ∈ {0.02, 0.03, 0.04, 0.05} | `rsi_oversold` ∈ {20, 25, 30, 35}（`rsi_overbought = 100 − rsi_oversold`） | 16 格 |
| S4 | `window` ∈ {10, 20, 30, 40} | `entry_z` ∈ {1.5, 2.0, 2.5, 3.0} | 16 格 |

默认配置必须落在格点内。其他参数保持配置默认值；一次只动网格里的两个维度。

## 3. 数据与选择规则

- **选择只在 train60 内进行**：每格在 train60 上算评分（`ratio` 与夏普），最优格与高原都由 train60 决定。
- validation20 只用于确认，不得据此重新选格；final20 禁止读取。
- 成本取 R4 确定的 1× 基准；另在 2× 成本下重算 train60 评分一次，检验最优格或高原是否移动。

## 4. 指标

- 每格：train60 与 validation20 的 `ratio`、夏普、收益、最大回撤、平仓交易数。
- 高原：`parameter_plateau(scores, tolerance=0.10)`，给出最优点、近优区域面积占比、边界触碰情况。
- 邻域：最优格的切比雪夫半径 1 邻域（含自身）。
- 多重检验：对 train60 最优格做 deflated Sharpe，`trials` = 全部格点数 × 累计族内尝试数，
  `trial_sharpe_std` 用**全部格点的测得夏普离散度**，不使用单序列近似。

## 5. 预注册判定规则（草案阈值）

对象 X **通过**，当且仅当：

1. **高原**：近优区域（tolerance 0.10）占全部有效格点的比例 ≥ 30%，且不贴着网格边界（边界格点占近优区域 < 50%）；
2. **邻域**：最优格的半径 1 邻域里，train60 `ratio` 的中位数 > 0，且 ≥ 最优格 `ratio` 的 50%；
3. **默认配置**：默认格在近优区域内，或其 train60 `ratio` ≥ 最优格 `ratio` 的 70%；
4. **确认**：近优区域内的格点在 validation20 的 `ratio` 中位数 > 0，且为正的格点占比 ≥ 50%；
5. **多重检验**：deflated Sharpe 的概率 ≥ 0.95，否则判"显著性在扣除搜索规模后消失"；
6. **成本不翻转**：2× 成本下最优格仍在近优区域内，或移动不超过半径 1；
7. **样本量**：每个被计入的格点组合层平仓交易数 ≥ 30；不足的格点标记为无效并从高原面积分母中剔除，
   但剔除后有效格点少于 10 个时，整体判"证据不足"。

S3、S4 预计大量格点无效，预期结论多为"证据不足"，如实记录。

## 6. 多重检验与尝试登记

所有格点的运行（包括 2× 成本的复算和无效格点）进入累计尝试数；`SearchScope` 的有限范围合同须在运行前写明上述网格，
运行后若发现需要扩大网格，必须新开协议条目，旧结论的尝试数继续累计。

## 7. 产物

`reports/backtest_r3_20261116/`：`registration.json`、每格 `grid.csv`、热图（train60 与 validation20 并列）、
近优区域标注图、`deflated_sharpe.json`。摘要另写 `*_results.md`。

## 8. 已知局限

- 二维网格看不到更高维的参数相互作用。
- 相邻格点用同一份数据、同一批交易，强相关；高原面积夸大了"独立证据"，所以判定同时要求邻域中位数与确认段。
- train60 选格而 validation20 部分被污染，validation20 的"确认"力度弱于一个完全干净的样本；这一点在结果里单独标注。
- `RangeStrategy` 与 `VolatilityReversionStrategy` 只在各自状态下交易，交易数偏少。
