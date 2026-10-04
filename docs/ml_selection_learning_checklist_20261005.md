# 机器学习选币：合并后学习检查清单

记录日期：2026-10-05。V1 已通过 [PR #48](https://github.com/user-nmmmmm/QuantTradingV2/pull/48) 合并，合并提交为 `77f9ca4198f5cd52d5c1cc32c2d56629f1fad2fc`。

这份清单帮助你读懂已经实现的选币流程、判断证据，并设计下一轮实验。按 1—8 的顺序学习；每项完成练习、核对结果后再勾选。

**用户学会知识，不等于模型增加训练。** 手算、合成数据和阅读报告用于验证理解；重训模型需要新的实验配置、冻结记录、预算及评价。工程 CI 通过说明实现通过对应检查，不证明模型有收益增量。

完整实现与本轮结论见 [V1 实施与研究复核](ml_selection_v1_review_20261005.md)，运行入口见 [训练说明](ml_selection_roadmap.md)，后续实验见 [下一阶段 Roadmap](ml_selection_next_roadmap_20261005.md)。

## 从实际不足选择学习重点

| 模型训练或证据不足 | 已有本地证据 | 对应学习与后续实验 |
|---|---|---|
| 可用于训练的记录与原始候选记录不同 | V3 共 129,423 条记录，成熟训练分区 41,980 条、1,077 个决策日 | 第 1 项：时间切分、同日分组、标签成熟；先审计样本流失原因 |
| 评分模型尚未证明选币增量 | 验证期 Ridge −2.79%、LightGBM −0.74%、原规则约 −0.36% | 第 2—3 项：预处理、回归和排序；先诊断标签、排序及候选暴露 |
| RL 最佳验证策略保持现金 | 三个 seed 最佳验证均为零选择、零成交、零收益、零奖励 | 第 4—6 项：实际权益奖励、梯度与门槛；拆解现金胜出的原因 |
| 实际策略更新预算很小 | seed 42/43/44 分别更新 4/5/4 次；每回合动作数为 244—460 | 第 7 项：以实际更新和时间窗口计算预算，不能把候选行数当更新数 |
| 增大树数未必增加有效复杂度 | LightGBM 预算 150 轮，最佳迭代为 18，早停等待为 20 轮 | 第 7 项：先读验证曲线，再决定预算和目标是否需要变化 |
| 历史币池与最终样本证据不足 | 静态币池为 `observed_history_only`；`independent_holdout=false`、`formal_admission=false` | 第 8 项：补齐历史成员及可知时间，预登记真正新的评价区间 |
| 冻结 RL 候选还没有完整前向桥接 | 已有评分诊断日志；RL 所需账户与原策略候选状态仍待接入 | 第 8 项：预测文件练习可先做，真实 RL 前向表现需完成状态桥接后取得 |

以上数值来自本地 `reports/ml_selection_full_20261004_v3/`。完整行情、模型及报告不随代码仓库分发；新克隆可以做合成练习，不能假定已经拥有这些本地产物。

V3 对应旧冻结源码。提交前已修复中断恢复等实现问题，修复后的源码与 V3 不同；新训练必须创建新实验，不能继续使用旧冻结目录或把旧结果当作修复后代码的重新运行结果。

## 1. 时间泄漏、标签成熟与同日分组

- [ ] **要懂的概念：**K 线时间、收盘决策时间 `as_of`、结果首次可知时间 `label_available_at`；训练、验证、测试的时间边界；同日横截面完整性。

源码入口：[dataset.py](../research/ml_selection/dataset.py) 的 `feature_snapshot`、`build_dataset`、`chronological_split`；练习参考 [数据集测试](../tests/test_ml_selection_dataset.py)。

项目按同一决策日整组剔除未成熟样本。若只逐行剔除，提前止损的亏损币可能被保留，而尚未结束的盈利币被删除，从而改变训练标签分布。

教材：[scikit-learn 官方 TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)。时间顺序和 `gap` 是基础；本项目还根据标签成熟时间处理整组样本，普通时间切分不会自动完成这一步。

- [ ] **练习：**构造三个同日合格币，`as_of=2022-12-20`，训练边界为 `2023-01-01`。A 的亏损结果在 `2022-12-25` 可知；B、C 的结果分别在 `2023-01-03`、`2023-01-04` 可知。补齐必要特征后调用 `chronological_split`，比较整组清除与仅清除 B、C 的差别。

另复制一份行情，只修改该决策日之后的价格和成交量，重新生成先前特征。

- [ ] **验收：**A、B、C 均不进入训练；入选训练行的标签可知时间严格早于训练边界；修改未来行情后，过去的特征与资格不变。能够解释保留 A 会引入什么偏差。

训练补强：先统计各边界被剔除的日期、币数和原因，再考虑新样本；不通过放宽成熟规则来凑训练行数。

## 2. Ridge、训练集预处理与模型保存

- [ ] **要懂的概念：**平方误差、L2 正则、截距；缺失值填充、均值和标准差；训练集拟合与验证集转换的区别。

源码入口：[models.py](../research/ml_selection/models.py) 的 `FeatureScaler`、`fit_model`、`RidgeModel`；练习参考 [模型测试](../tests/test_ml_selection_models.py)。

Ridge 提供简单基准。填充和标准化参数只能来自训练集；截距不受 L2 惩罚。模型保存特征顺序和预处理参数，推理继续使用同一套变换。

教材：[scikit-learn 官方 Ridge](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html)；补充阅读 [官方预处理泄漏说明](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage)。本项目使用 NumPy 实现，公式和防泄漏原则仍相同。

- [ ] **练习：**单特征训练数据设为 `[1, 2, NaN, 4]`。先手算有效值中位数，再填充缺失值并求均值；只把验证特征放大一百万倍，保持日期和标签不变，重新拟合 Ridge。

另将这个练习模型保存、加载，并对同一输入比较预测；练习产物放在临时目录，不改写正式实验。

- [ ] **验收：**填充中位数为 `2`，填充后均值为 `2.25`；改变验证特征不改变训练 scaler 和 Ridge 参数；保存、加载前后预测最大绝对差不超过 `1e-12`。

训练补强：调整特征或正则强度前，先说明其假设与验证指标；参数选择使用训练、验证区间，不根据已看过的测试表现反向调参。

## 3. 回归预测、横截面排序与选币增量

- [ ] **要懂的概念：**连续收益回归、同日排序、Spearman、最高与最低分数组收益差；标签诊断与账户收益的区别。

源码入口：[models.py](../research/ml_selection/models.py) 的 `ranking_metrics`；[selector.py](../research/ml_selection/selector.py) 的 `select`；[pipeline.py](../research/ml_selection/pipeline.py) 的监督训练与组合对照。

当前 Ridge 和 LightGBM 拟合连续净收益标签，再按预测排序。LightGBM 的目标是 `regression`，当前实现没有使用 `LGBMRanker`；回归误差小也不保证排序或组合收益改善。

教材：[LightGBM 官方 LGBMRanker](https://lightgbm.readthedocs.io/en/stable/pythonapi/lightgbm.LGBMRanker.html)，重点理解查询分组 `group`。它是学习排序任务的对照资料，不表示本项目已实现该模型。

- [ ] **练习：**同日四币，预测 `[1,2,3,4]`，成熟标签 `[0.1,0.2,0.3,0.4]`。调用 `ranking_metrics(..., quantiles=2)`；再反转预测顺序，最后把四个预测设为相同值。若提供标签可知时间，也要传入合法的 `mature_as_of`。

同时画出原策略候选 → 资格通过 → 模型入选 → 风控批准 → 实际成交的数量流程，标明每一步由哪个模块处理。

- [ ] **验收：**正序 Spearman 为 `1`，高低组收益差为 `0.2`；反序为 `-1`、`-0.2`；全部同分时报告排序信息不足。解释为什么这些标签均值不能相加当作账户收益，以及低敞口、健康暂停如何影响比较。

训练补强：先检查标签分布、同分比例、同日排序和实际候选覆盖，再预登记目标函数对照；不以模型名称或复杂度判断改进。

## 4. 实际权益、成交成本与奖励拆解

- [ ] **要懂的概念：**现金、持仓市值、净权益、实际手续费、未实现盈亏；成交换手；回撤增加惩罚与最大回撤的区别。

源码入口：[environment.py](../research/ml_selection/environment.py) 的 `equity_rewards`；原执行入口为 [backtest/engine.py](../backtest/engine.py) 与 [core/runtime.py](../core/runtime.py)；练习参考 [环境测试](../tests/test_ml_selection_environment.py)。

项目奖励来自扣费后的实际权益。手续费已经影响权益，奖励不能再次扣费；未成交目标不产生实际成交换手。回撤惩罚按路径增加量累计，不能直接拿最大回撤乘权重替代。

教材：[QuantConnect 官方交易费用模型](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/transaction-fees/key-concepts)，用于理解成交成本如何进入账户核算。

- [ ] **练习：**初始资金 `1000`，以 `100` 买入一单位，手续费率 `0.1%`；价格涨到 `102` 后卖出，费率相同。手算买入后现金、卖出前权益和卖出后权益。将这些权益输入奖励函数，并关闭回撤、额外换手惩罚。

再加入一个未成交订单，确认它没有改变实际成交记录；有 V3 本地产物时，选一个正收益但负奖励的 RL 回合，分列净对数收益与惩罚。

- [ ] **验收：**现金为 `899.9`，卖出前权益为 `1001.9`，卖出后权益为 `1001.798`；无额外惩罚时奖励总和等于 `log(1001.798/1000)`，误差不超过 `1e-12`；未成交订单的成交换手为零。

训练补强：先确认奖励是否符合研究目标，再设计权重对照。提高成本做压力实验时重新运行成交环境，不只在最终收益上减一个估计数。

## 5. Bernoulli REINFORCE、梯度与 reward-to-go

- [ ] **要懂的概念：**动作 `a`、概率 `p`、logit、对数概率梯度 `a-p`；动作后的累计奖励、折扣、基线和优势；一次回合后的一次更新。

源码入口：[models.py](../research/ml_selection/models.py) 的 `BernoulliPolicy.update`；[environment.py](../research/ml_selection/environment.py) 的 `discounted_returns`；[pipeline.py](../research/ml_selection/pipeline.py) 的 `train_policy`。

收盘决策只能影响后续成交。训练使用严格晚于动作决策时间的奖励行，避免奖励动作发生前已经取得的收益；策略排序仍来自父评分模型。

教材：[OpenAI Spinning Up 原始策略梯度教程](https://spinningup.openai.com/en/latest/spinningup/rl_intro3.html)，重点阅读 reward-to-go 与 baseline。

- [ ] **练习：**使用教学简化条件：输入已经标准化且固定为 `x=1`，初始 `w=b=0`，优势为 `1`，学习率 `0.1`，熵系数为 `0`，没有梯度裁剪生效。这里不拟合单行 scaler；若用代码验证，显式使用均值 `0`、尺度 `1` 的变换。

分别手算 `a=1` 和 `a=0` 的一次更新。另设三行奖励为 `[-0.01,0.02,-0.01]`、`gamma=1`；第一行收盘动作只计之后两行的奖励。

- [ ] **验收：**`a=1` 时 `w=b=0.05`，新概率约 `0.524979`；`a=0` 时 `w=b=-0.05`，新概率约 `0.475021`。第一行收盘动作的后续回报为 `0.01`，不包含首行 `-0.01`。能说明真实训练还使用优势缩放、基线、熵项与裁剪。

训练补强：先检验奖励时序、动作覆盖和梯度方向，再扩大回合数。当前完整回合 REINFORCE 适合先诊断，不需要先改成 PPO 或大型深度网络。

## 6. 动作概率、0.5 门槛与现金倾向

- [ ] **要懂的概念：**随机采样与确定性决策；门槛附近的微小变化；合法现金动作和正收益验收条件。

源码入口：[models.py](../research/ml_selection/models.py) 的 `BernoulliPolicy.predict`、`act`；[selector.py](../research/ml_selection/selector.py) 的门控；[pipeline.py](../research/ml_selection/pipeline.py) 的验证与候选冻结。

本项目的 `p` 是策略选择候选的动作概率，**不是盈利概率**。确定性评估使用 `p >= 0.5`；微小负 logit 就可能不选。V3 三个 seed 的最佳验证均保持现金，不能据此说模型高度确信所有币都将亏损。

教材：[scikit-learn 官方决策门槛教程](https://scikit-learn.org/stable/modules/classification_threshold.html)。该教程讨论分类概率；这里只借它理解概率与决策的区别，不能把项目的动作概率解释成分类盈利概率。

- [ ] **练习：**对概率 `[0.499,0.500,0.501]` 判断确定性动作。再用固定 seed，对 `p=0.499` 随机采样一万次，计算选择比例；可以用 Bernoulli 随机数完成这个纯概率练习。

有 V3 本地产物时，检查最佳策略的验证动作与概率分布，并解释为什么零交易得到零收益、零回撤、零奖励，却仍未通过默认正收益要求。

- [ ] **验收：**确定性动作是 `[False,True,True]`；随机选择比例与 `0.499` 的差不超过 `0.02`。能解释“确定性全部拒绝”不等于“概率接近零”，以及奖励最高不等于资格通过。

训练补强：门槛、奖励权重与资格条件分别登记；门槛选择只能使用训练、验证。看过测试再降低门槛会产生新的研究尝试，不能沿用原测试验收身份。

## 7. 实际更新预算、seed、窗口与早停

- [ ] **要懂的概念：**候选行数、回合动作数、参数更新次数和独立市场时期；随机种子；最佳检查点、最大预算和早停等待。

源码入口：[pipeline.py](../research/ml_selection/pipeline.py) 的 `train_policy`、`train_policies` 与滚动评价；预算入口为 [config/ml_selection.yaml](../config/ml_selection.yaml)。

教材：[LightGBM 官方 early_stopping](https://lightgbm.readthedocs.io/en/stable/pythonapi/lightgbm.early_stopping.html)。最佳迭代与总预算不同；RL 的 patience 逻辑则直接阅读本项目实现。

- [ ] **练习：**从每个 seed 的 `models/policy_latest.json` 和 `rl_training.json` 重建实际 `update_count`，再计算 `trajectory_actions` 的最小值、中位数、最大值。没有本地产物时，可使用下表做手算，但不能声称完成了文件复核。

| seed | 各回合动作数 | 实际更新次数 | 最小 / 中位 / 最大 |
|---|---|---:|---|
| 42 | 244、446、448、435 | 4 | 244 / 440.5 / 448 |
| 43 | 421、266、415、425、407 | 5 | 266 / 415 / 425 |
| 44 | 460、411、287、428 | 4 | 287 / 419.5 / 460 |

再手算验证奖励 `[0,-0.1,-0.05,-0.02]`、`patience=3`、仅严格增加视为改善的停止位置；核对 LightGBM 的 `best_iteration=18`、`num_boost_round=150`。

- [ ] **验收：**统计与上表一致；教学奖励序列第 4 回合停止。能解释只把 `episodes` 增加到 30，仍可能在第 4 或第 5 回合停止；重放同一历史不会增加新的独立市场时期。

训练补强提案：先诊断目标、奖励和候选暴露，再考虑每 seed 每窗口 20—30 次实际更新、3 个 seed、2 个窗口，即 120—180 次更新。需重新登记 patience 和算力预算；这些数字是待评估提案，不是保证收益的配方。

## 8. PIT 币池、冻结实验与真正的前向证据

- [ ] **要懂的概念：**历史成员、上市与退市事实、事实首次可知时间；今天知道的历史与当时可知信息；回顾性重放、前向记录和独立最终样本。

源码入口：[dataset.py](../research/ml_selection/dataset.py) 的成员资格及 `forward_proxy_outcome`；[protocol.py](../research/ml_selection/protocol.py) 的冻结与身份校验；[pipeline.py](../research/ml_selection/pipeline.py) 的影子观察和结果成熟流程。

教材：[圣路易斯联储官方 Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)。该资料用经济数据版本说明“历史当时已知”的含义；迁移到币池时还需提供真实上市、退市与可交易成员来源。

- [ ] **练习：**构造上市事实日为 1 月 1 日、首次可知日为 1 月 3 日的币，确认 1 月 2 日不可纳入资格。再构造退市事实及其可知时间，检查资格随时间变化；不能用今天剩余币种反推整个历史币池。

检查一份预测观察文件和后续成熟结果文件，区分 `information_cutoff`、真实 `observed_at`、`entry_not_before`、标签可知时间；确认原预测、特征和身份没有被结果回写改变。没有新鲜数据时，只做合成检查并记录证据缺失。

- [ ] **验收：**未来才可知的上市、退市事实没有用于过去判断；入场不早于真实观察时间；成熟结果追加而不改写原观察；能解释历史 replay 为什么不属于新的前向证据，以及已看过的 test 为什么不能重复充当独立 final。

训练补强：补齐历史成员来源、可知时间与新鲜行情，再预登记新的窗口。真实 RL shadow 还需要账户状态和原策略候选状态桥接；当前父评分模型诊断文件不能替代冻结 RL 候选的前向表现。

## 可选的四周学习安排

这是自学顺序，没有固定日期或自动提醒；每周按自己的时间调整。

| 学习周 | 内容 | 可检查的产出 |
|---|---|---|
| 第 1 周 | 第 1—2 项 | 时间与成熟边界图；整组剔除练习；scaler 手算与保存一致性结果 |
| 第 2 周 | 第 3—4 项 | 排序诊断；候选到成交数量流程；一笔交易的权益和奖励账 |
| 第 3 周 | 第 5—6 项 | Bernoulli 梯度手算；reward-to-go 时序图；门槛与采样对照 |
| 第 4 周 | 第 7—8 项 | 更新预算核对；PIT 与前向证据缺失清单；一份新实验草案 |

先完成这些小练习，再按下一阶段 Roadmap 设计有限预算的实验。练习不要求 GPU；当前 Ridge、LightGBM 和 NumPy REINFORCE 的主要耗时来自 CPU 计算与完整引擎回测。
