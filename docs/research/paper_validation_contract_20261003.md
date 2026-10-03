# 论文统计验证接口契约

日期：2026-10-03。实现：[analysis/paper_validation.py](../../analysis/paper_validation.py)。专项测试：[tests/test_paper_validation.py](../../tests/test_paper_validation.py)。

本模块完成论文路线图阶段2中的统计工具工作包。它接收调用方提供的研究数据，不读取最终留存样本，不拟合交易策略，不改写准入门槛，也不授权真实资金。工具实现完成不代表某个候选已经通过研究、执行或资金准入。

## 输入与输出边界

候选收益面板为 `pd.DataFrame`：行是唯一、递增、无缺失的 `DatetimeIndex`；列为唯一的非空字符串候选ID。每行必须是同步的同一期收益，各列资金与成本口径一致。失败候选仍须保留；未知数据不能用零收益填充；弃权期为零必须有实际现金/持仓依据。缺失、重复、不同时间轴或非有限值返回 `invalid`，不默默取交集或移除列。

函数输出是可保存的诊断字典。`status=ok` 表示该方法完成计算且所需口径由调用方声明完整；`diagnostic` 表示只计算局部证据；`insufficient` 表示样本或分数不足；`invalid` 表示输入契约失败。所有输出的 `admission_eligible` 固定为 `False`。没有任何 `pass` 或资金放行状态。

面板本身不能证明全部搜索历史完整。调用方必须把候选ID与实验登记、失败试验和搜索身份对应。现有历史已被查看时，完整局部候选族也仍是回顾性研究。

## CSCV与PBO

接口：`cscv_pbo(candidate_returns, *, n_groups=8, score="sharpe", family_complete=False, minimum_rows_per_group=2)`。

按原论文要求把时间轴分为偶数个等长块，枚举全部 `C(S,S/2)` 个训练/测试互补组合。数据长度必须整除分组数；不满足时返回 `insufficient`，调用方若截取等长区间必须另外记录截取范围及原因。

训练中选择最高分候选，再在完整候选族的测试分数中计算其升序名次。相对名次为 `rank/(候选数+1)`，logit为 `log(rank_fraction/(1-rank_fraction))`。PBO使用 `logit <= 0` 的组合比例。训练并列按输入列顺序选首列；测试并列用平均名次，正中位并列保守计入过拟合。分数可选每期Sharpe或平均收益；任一组合出现零方差等未定义Sharpe时，保留失败组合而不删候选，整体返回不足。

`family_complete=False` 可保留 `diagnostic_probability`，`probability` 为 `None`。逐组合结果包括选择身份、分数、名次、logit和并列数量。候选族完整声明仅适用于所声明的局部族，不证明全项目历史搜索完整。[PBO原论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。

## CPCV与路径组装

接口：`cpcv_splits(event_starts, event_ends, *, n_groups=6, n_test_groups=2, embargo_fraction=.01)`；`assemble_cpcv_paths(plan, split_predictions)`。

事件区间必须对齐、成熟、按起点递增；同时间事件不得跨组。拆分为N个连续组，每次同时选k个测试组，产生 `C(N,k)` 个组合折以及 `C(N-1,k-1)` 条完整路径。每个测试组复用现有 `purged_cv_indices` 的重叠区间与embargo排除，再把各组允许的训练集合取交集。因此非连续测试组之间仍可保留合法训练事件，不能把全部中间区间当成测试。

模型必须分别在每折允许的训练事件上拟合，每折输出只包含其测试事件的原始整数位置。组装器逐组消费测试单元，每个 `(split_id, group_id)` 在全部路径中只用一次，每条路径按原时间轴包含每个事件一次。缺预测、重复、额外位置或非有限预测返回 `invalid`；任何空训练折使计划返回 `insufficient`。

输出是事件预测路径，不是收益路径。交易信号、仓位、成本和持仓跨段规则需另行固定后回放。禁止把已在全部历史拟合的候选收益拆开后称为CPCV样本外结果。CPCV可用后来时间组训练，只适合作研究重采样；不能冒充当时可执行的因果回放。多条路径共享历史，不能将路径数乘进独立样本数。

## White Reality Check与依赖重采样

接口：`white_reality_check(candidate_returns, benchmark_returns, *, family_complete=False, iterations=2000, block_length=5, bootstrap="stationary", seed=42, minimum_observations=30)`；通用索引函数为 `block_bootstrap_indices(n_observations, *, iterations, block_length, seed=42, method="stationary")`。

基准必须与候选面板的时间索引完全一致。先计算成本后候选与固定基准的逐期差，再对整族共同采样；每个候选单独去均值以施加最不利零优势原假设。统计量为 `sqrt(n)*max(0, 各候选平均超额收益的最大值)`。显式包含零优势基准使没有正样本优势的族得到p值1。Bootstrap使用相同索引共同重采样全部候选，保留同时期候选相关性。

stationary模式以 `1/block_length` 概率重启，否则沿时间轴循环前进；circular模式推广已有趋势验证的固定长度循环块思路。p值使用 `(1+超过次数)/(1+迭代数)`，输出随机种子、块长、统计量、分位数及Monte Carlo标准误。少于最小观测或两个名义块返回不足；族不完整仅填 `diagnostic_p_value`。

该实现为非学生化的White族最大值检验，不是Hansen SPA；弱平稳和弱依赖假设仍需研究者判断。正式报告应展示预先规定的块长敏感性。[White原论文](https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152)、[Politis与Romano原论文](https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476870)。

## DSR完整证据与估计敏感性

接口：`deflated_sharpe_evidence(candidate_returns, selected_candidate, *, historical_trials_complete=False, registered_before_results=False, declared_total_trials=None, minimum_observations=30, periods_per_year=365)`。

每列实际收益计算每期Sharpe，用跨试验Sharpe样本标准差估计离散度。输出全部试验Sharpe、离散度及方差、所选候选的偏度与原始峰度（正态=3）、样本数、期望最大Sharpe、Sharpe采样方差项及概率。计算始终用每期单位；`periods_per_year` 仅影响年化显示，不改变DSR概率。

只有历史搜索完整、结果前登记、声明总试验数与面板列数严格匹配时，才填 `probability`；否则只保留 `diagnostic_probability`。零方差试验不能删掉后宣称历史完整；无法估计的族返回不足。

候选收益相关矩阵的特征值参与率提供有效试验数估计；初始正自相关和提供时间有效观测估计。两者明确标为启发式，并与原始试验数、原始时间样本同时展示四组敏感性，绝不替代原始历史计数。有效试验数接近1时，期望最大Sharpe的近似公式在1至2之间做线性插值，该插值仅用于估计敏感性。

DSR解析概率包含非正态矩修正，但其时间采样推断仍是iid近似。相关估计不证明独立性，历史完整也不使解析概率自动成为准入结论。[DSR原论文](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)。

## 已执行验证

专项测试覆盖小矩阵手算、完整组合及互补、稳定并列、无优势基准、候选共同重采样、自相关块保留、标签区间重叠与embargo、完整事件路径、不同时间轴拒绝、缺历史试验及不足诊断、每期/年化单位隔离、有限JSON序列化。

2026-10-03执行结果：`22 passed`。测试使用本地合成反例验证方法，不构成项目候选的实际市场收益或准入结果。
