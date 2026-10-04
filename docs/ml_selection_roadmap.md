# 机器学习选币：训练入口与 Roadmap

本实现把历史数据准备、评分模型训练、原引擎组合对照、奖励驱动训练和影子评分串成一条可复现的离线流程。默认使用 60 个已登记币种、日线、现金现货和原有交易策略。选币器改变候选是否入选及排序；仓位、退出、风控、费用与成交继续由现有引擎处理。

**完成历史训练不等于证实策略有效。** 当前数据来自已经研究过的历史区间及静态币池，报告始终标记为 `retrospective_research_only`，不会将历史 `test` 分区标成独立最终验收，也不会启用实盘或提交真实订单。

本次完整运行已保存在 `reports/ml_selection_full_20261004_v3`，60 个币、129,423 条候选记录，训练主线及四个滚动窗口共用时 502 秒。RL 三个种子因验证早停分别完成 4、5、4 个回合。新增 142 项测试、原有 98 项回归测试及 6 个子测试通过；协议、源文件、数据和 586 个输出文件的身份检查通过，所有模型可重新加载。详细证据在该目录的 `execution_receipt.json`。

本轮有效性未通过。评分模型验证期 Ridge 为 −2.79%、LightGBM 为 −0.74%；三个 RL 种子的最佳验证策略均保持现金，冻结候选的 `validation_qualification_passed=false`。历史测试期原规则 −4.10%、Ridge +3.33%、LightGBM −2.99%、RL 0%；Ridge 的测试盈利可以作为后续研究线索，但不能看完测试再改选它。完整解释和四个滚动窗口结果见该目录 `report.md`。R8 已检查日志入口，当前冻结数据截止 2026-09-18，未取得新鲜前瞻证据；冻结候选为 RL，还需要账户与原策略状态的前向桥接。

## R0—R8 当前状态

| 阶段 | 已实现的工程能力 | 仍需完成的研究或外部证据 |
|---|---|---|
| R0 冻结协议与基线 | 冻结设置、账户、引擎选项、数据与代码哈希；后续阶段检查身份；旧基线独立复现 | 登记新的实验预算与最终验收协议；旧历史基线保持原语义 |
| R1 历史候选 | 为完整输入币池生成因果特征、资格及排除原因 | 完整历史交易所成员、上市、退市及当时可得时间证据；当前为 `observed_history_only` |
| R2 标签 | 下一根开盘入场，固定窗口或 ATR 止损退出的扣费代理标签；清除未成熟和跨界标签 | 标签仍是假设小额完整成交的独立交易，不能相加当组合收益 |
| R3 评分模型 | NumPy Ridge、CPU LightGBM、训练期拟合预处理、验证诊断、模型 JSON；4 个预登记滚动窗口 | 根据真实运行结果判定稳定性、参数选择预算及失效环境 |
| R4 组合对照 | 原引擎运行原评分、资格过滤原评分、动量、多个随机种子和 ML；保存选择、成交与权益 | 证明改善来自选币能力；核对敞口、成本、风控批准及收益集中度 |
| R5 奖励与环境 | 完整原引擎回合、真实权益奖励、实际成交换手；保留风险终止与末尾费用 | 若需要 Gym 逐步接口，应另行实现并验证，不把当前回合接口当作它 |
| R6 强化学习 | NumPy Bernoulli REINFORCE；42、43、44 三个种子；每个完整回合后更新和保存，可从已完成回合继续 | 实际种子稳定性、更多 RL 时间窗口及相对预测模型的增量证据 |
| R7 历史评价 | 历史 `test`、4 个滚动评分窗口、1.5/2 倍成本；重建持仓净盈亏，按固定 5 日入场组检查去掉最大 5/10 组 | 独立最终样本、分组独立性、多重试验证据及更多执行压力条件 |
| R8 前瞻观察 | 冻结评分、真实观察时间与信息截点、内容身份；追加成熟代理结果，禁止改写原预测 | 新鲜行情与资格证据、标签成熟和前瞻评价；冻结 RL 候选的账户与策略状态仍待接入 |

每次完整运行输出 `roadmap_status.json` 和 `adjudication.json`。它们分别记录工程进度与证据边界；`formal_admission` 和 `production_enabled` 均保持 `false`。

## 模型如何学习

评分阶段使用 21 个行情与市场特征预测独立代理交易的净收益。Ridge 提供简单基准，LightGBM 提供非线性评分。训练、验证、历史测试按时间切分，同一决策日的样本保持同组；标签必须在分区结束之前成熟。预处理仅用训练样本拟合。

完整流程先根据验证区间的实际组合奖励选择父评分模型，再训练强化学习。强化学习采用小型 NumPy Bernoulli 策略：根据行情及账户特征，对合格原策略候选决定是否允许新增交易；排序使用评分模型结果。它可以拒绝全部候选，保留现金。

一次训练回合会运行完整训练历史，积累动作与后续奖励，再更新一次策略参数。验证回合冻结参数，以奖励选择 `policy_best.json`。当前实现为 **完整回合 REINFORCE**，并非 PPO，也没有 Gym 的逐步 `reset/step` 接口。默认每个种子最多 6 个回合，包含验证早停；三个种子最多 18 个训练回合。这个预算是工程起点，不能据此认为训练充分或收益稳定。

最终候选在验证区间比较评分模型与 RL。先按预登记的正净收益、回撤及会计一致性条件筛选，再按奖励选择；若没有模型通过，仍保存研究候选，并明确标记 `validation_qualification_passed=false`。保持现金是合法动作，零收益保持现金不满足默认正收益门槛，不能据此宣称选币改善。历史测试结果不参与候选选择。

奖励为：

```text
reward = net_log_equity_return
         - drawdown_penalty × max(0, drawdown_increase)
         - turnover_penalty × actual_filled_notional / equity
```

净权益已经包含费用、成交价格影响和未实现盈亏，奖励不重复扣费。换手来自实际成交；未成交目标不产生换手。风险终止后的冻结展示尾部不继续参与奖励，末尾真实清算费用则保留。当前默认回撤权重为 `0.5`、额外换手权重为 `0`。

## 训练输入的交付前提

以下命令均从仓库根目录执行。默认配置引用的登记和行情位于被 Git 忽略的研究目录；代码仓库不分发本地行情、模型和训练结果。全新 clone 可以运行合成输入测试，默认历史训练需要先恢复可信的原始输入包：

- `reports/multicoin_100k_20261004/registration.json`。
- `reports/multicoin_100k_20261004/input/engine/<SYMBOL>.csv`，包含登记的 60 个币种。
- `reports/smart_capital_100k_20261004/registration.json`，包含原 smart 配置与引擎选项。

保持登记与 CSV 字节、文件名及目录关系一致；程序逐项核对文件和解析后行情身份。不能用新下载的数据覆盖旧登记或手工重写哈希。若使用其他可信数据包，应创建对应的新登记与实验配置。旧基线复现还需要原登记绑定的参考报告，详见 [V1 实施总结](ml_selection_v1_review_20261005.md)。

仅验证代码时，安装研究依赖后运行 `python -m pytest -q tests/test_ml_selection_*.py`；这些测试使用合成行情，不依赖上述历史目录。GitHub CI 单独安装可选 ML 依赖并执行这些测试。

## 在这台电脑上启动

这条流程使用 CPU。Ridge 与 REINFORCE 依赖现有 NumPy；LightGBM 的可选研究依赖单独放在 `requirements-ml.txt`，不改变正式运行依赖。当前默认 LightGBM 使用 2 个线程，不需要 CUDA、PyTorch 或 GPU 安装。

RTX 3050 Ti Laptop 不参与当前训练。16GB 系统内存适合先运行 8 个币、每个种子 2 个回合的检查，再扩到 60 个币；一次只运行一个实验。主要耗时来自原引擎多次回测，4GB 显存不会提升这部分速度。完整耗时以小规模实测为依据。

如果当前虚拟环境缺少 LightGBM，先安装可选依赖：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r "requirements-ml.txt"
```

首次完整检查使用 8 个币、每个 RL 种子最多 2 个回合，共最多 6 个训练回合。它仍完成训练、验证、历史测试、成本压力和 4 个滚动窗口，输出目录自动生成：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --max-symbols 8 --rl-episodes 2
```

正式历史研究使用默认登记的全部 60 个币及配置中的回合数：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml"
```

`--max-symbols` 取登记名单前 N 个币，仅用于控制规模；它不代表随机抽样或有代表性的研究币池。修改模型、回合数、时间边界或研究配置时创建新实验，不覆盖已冻结目录。

默认区间为 2020-01-01 至 2026-09-18；训练边界为 2023-01-01，验证边界为 2024-07-01。分区范围左闭右开，并额外清除跨界或未成熟标签。这个划分是历史开发对照口径。

## 分阶段运行与恢复限制

只生成协议、特征和标签，可以使用 `prepare`。指定的目录必须尚不存在：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage prepare --max-symbols 8 --rl-episodes 2 --run-dir "reports\ml_selection_prepare_example"
```

然后使用冻结协议训练评分模型，同时完成组合验证矩阵并冻结 `parent_model.json`：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage supervised --run-dir "reports\ml_selection_prepare_example"
```

在同一目录继续三个种子的强化学习，并冻结与评分模型比较后的最终研究候选：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage rl --run-dir "reports\ml_selection_prepare_example"
```

继续历史测试和成本压力：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage evaluate --run-dir "reports\ml_selection_prepare_example"
```

单独完成预登记滚动窗口：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage walk-forward --run-dir "reports\ml_selection_prepare_example"
```

滚动窗口分别以 2022、2023、2024、2025 年初为训练结束时点，随后半年验证、再半年测试。每个窗口只用自己的成熟训练历史拟合评分模型，以自己的验证区间选模型，并对照原评分、动量和随机种子。当前这些窗口验证的是评分模型；RL 多种子训练仍使用主分区，没有在四个窗口分别重训 RL。

后续阶段以 `run-dir` 中的冻结设置为准，命令中的 `--config` 不会覆盖它。不能再传 `--max-symbols` 或 `--rl-episodes` 修改冻结设置。代码、协议、已登记数据或输出身份不一致时，程序拒绝继续并要求创建新实验。

当前恢复能力如下：

- `supervised` 可以读取同一实验内已有的评分模型，缺少的模型才训练；随后运行验证并冻结父模型。关闭 RL 时，这一步也冻结最终研究候选。
- 正常分阶段顺序为 `prepare → supervised → rl → evaluate → walk-forward`；配置关闭 RL 时跳过 `rl`。
- `rl` 需要已有评分模型及 `parent_model.json`；`evaluate` 还需要冻结的 `candidate.json`。应按上述顺序继续。
- 重跑 `rl` 以每个种子 `rl_training.json` 中已完成的回合为准，从对应编号的 `policy_NNN.json` 恢复参数和随机数状态，并检查父模型及记录的检查点身份。`policy_latest.json`、`policy_best.json` 是便利别名；中断时可能超前，恢复会依据已完成记录纠正它们。回合中途的撮合、账户和策略状态未保存；当前未完成回合需要重跑。已达到冻结预算或早停条件的种子不继续增加训练。
- 最终候选一旦冻结，后续阶段不能换成另一个模型；若需要改变训练预算或候选，创建新实验。
- `evaluate` 会重新生成历史测试与压力报告。重复查看同一历史测试区间不产生新的独立证据。
- `all` 每次冻结新目录，不能在已有目录上自动跳过已完成阶段。中断后先查看 `progress.json` 和已生成文件，再决定可执行的阶段；不删除身份检查来强行恢复。

2026-10-05 提交前追加了检查点中断一致性和集中度重跑目录修复，保留了用于 2026-10-04 V3 结果的精确源文件归档。V3 结果与模型保持原记录；当前源码和其冻结源码不同，后续实验应创建新目录，不能删除源码检查强行继续 V3。

## 输出怎样查看

| 文件或目录 | 用途 |
|---|---|
| `protocol.json`、`artifacts.json` | 冻结配置、输入与代码身份及输出哈希 |
| `dataset.csv`、`dataset_summary.json` | 样本、特征、标签成熟时间、排除原因及分区数量 |
| `models/ridge.json`、`models/lightgbm.json` | 评分模型、特征顺序、预处理及训练边界 |
| `supervised_validation.json` | 预测与排序诊断 |
| `parent_model.json`、`candidate.json` | 父评分模型，以及通过验证门槛比较后的冻结研究候选 |
| `rl_seeds/*/models/policy_*.json`、`rl_seed_comparison.json` | 每个种子的模型、回合恢复状态及验证比较 |
| `models/policy_best.json`、`rl_training.json` | 验证选出的 RL 策略及其训练历史 |
| `episodes/*/selection.csv` | 每次候选评分、选择理由及账户状态 |
| `episodes/*/trades.csv`、`equity.csv`、`rewards.csv` | 实际成交、权益与奖励账本 |
| `episodes/*/closed_positions.csv` | 重建持仓的净盈亏及入场时间，用于集中度检查 |
| `validation_comparison.json`、`test_comparison.json` | 原评分、动量、随机、ML 和可选 RL 的组合对照 |
| `cost_stress.json`、`adjudication.json` | 成本压力、历史检查和未完成证据 |
| `walk_forward/*`、`walk_forward.json` | 四个独立训练时间边界下的历史滚动对照 |
| `report.md`、`roadmap_status.json`、`progress.json` | 用户报告、阶段状态与运行进度 |

应一起查看扣费收益、回撤、平均敞口、实际成交、奖励和风险终止。单独看训练奖励上升，或者仅看收益最高的模型，无法判断选币是否有效。

集中度检查从重建持仓的 `net_pnl` 出发，按入场时间的固定 5 日块合并，计算去掉最大 5 组和 10 组后的剩余净盈亏。默认历史门槛使用“去掉最大 5 组后为正”，10 组结果同时报告。分组数量不足时明确为证据不足；固定时间分组尚不能证明组之间独立，也不自动处理多重试验。

## 核对旧历史基线

ML 入口采用现金现货的新研究账户；旧 `spot_margin` smart 基线继续使用原登记配置，禁止用新账户结果冒充旧基线。

独立复现入口如下，输出目录必须尚不存在：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\verify_ml_selector_baseline.py" --output "reports\ml_selector_baseline_check"
```

本次旧基线复现证据覆盖 2453 个日历日、664 次成交，权益最大绝对差为 0，并核对执行摘要、资金分配摘要及会计一致性。它证明默认旧基线在可选 ML 接口加入后得到保留，不证明 ML 模型有效。新训练实验仍需分别完成真实运行并检查结果。

## R8：提供新行情后进行影子评分

冻结候选为评分模型时，影子入口使用该模型记录最新预测，只将行情特征送入模型。冻结候选为 RL 时，用 `--account-state` 提供原引擎 hook 当时的完整候选、账户、挂单、权益高水位、策略健康与风险余量；固定动作状态重播已经接入。缺状态时仍标记 `requires_account_and_strategy_state_for_frozen_RL_candidate`，父评分模型诊断不替代 RL 决策。决策桥接不生成账户成交或权益收益，影子入口不提交真实订单。

新行情目录中的文件名沿用登记名单，例如 `BTC_USDT.csv`。CSV 必须包含 `timestamp,open,high,low,close,volume`；`timestamp` 为 UTC 午夜日线开盘时间，不能用收盘时间替代。可提供 `quote_volume`、`close_time` 和 `available_at` 等同名字段；显式提供的可得时间缺失时会排除样本。请保留至少 61 根连续已收盘历史，使用同一数据口径，包含冻结名单中需要评分的币及 BTC 市场特征。

使用下面命令前，将 `run-dir` 替换为已完成 `all` 的真实实验目录，并准备指定的新行情目录：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage shadow --run-dir "reports\ml_selection_completed_run" --market-data-dir "data\ml_forward"
```

记录区分实际创建时间 `observed_at` 和信息截点 `information_cutoff`。默认截点为当前 UTC，也可用 `--as-of` 指定 ISO 时间；未来截点和不晚于模型训练标签成熟时间的截点会被拒绝。过去日期的重新评分明确标记 `retrospective_scoring`。

程序只保留该截点前已经收盘的数据，合格最新样本还必须达到该截点的 UTC 当日；旧行情得到 `awaiting_fresh_market_data`。不提供新行情目录时会尝试冻结历史输入，陈旧历史不会变成前瞻证据。若冻结候选为 RL，账户与策略状态缺失的标记优先于一般评分状态。

观察保存在 `shadow/observation_*.json`，包含输入内容 SHA、实际特征、模型身份和覆盖全部观察内容的 `observation_id`，相同观察路径禁止覆盖。新 CSV 仍不等于历史币池已验证。尚未成熟的结果标记 `pending_future_data`。

保留原历史、追加未来已收盘行情后，用下面的入口解析成熟代理结果：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\train_selector.py" --config "config\ml_selection.yaml" --stage resolve-shadow --run-dir "reports\ml_selection_completed_run" --market-data-dir "data\ml_forward"
```

解析前检查原观察的内容身份及协议，并重新核对原特征；若历史被修改而特征不同，程序拒绝静默替换。结果追加到新的 `shadow/outcomes_*.json`，原预测文件保持不变。默认 20 根日线窗口仍需未来行情及标签可得时间成熟；没有成熟结果时记录待等待数量。结果属于独立交易代理，诊断评分也不代表账户实际成交收益；历史重放与前瞻诊断分别标记。

前向标签的起点必须晚于真实观察时点。若在 UTC 日内记录预测，当天午夜开盘已经过去，代理收益最早从观察之后的下一可执行日线开盘开始；不能把已经过去的开盘价或当天观察之前的行情加入收益。止损使用观察时已经可得、随特征记录冻结的 ATR，后续新增数据不能改变它。举例：在 UTC 10:00 记录预测，最早入场是次日 UTC 00:00，之后再计算退出及成熟时间。历史重放仍标记为 `retrospective`，不能借回填过去的收益变成前瞻证据。

## 完成研究验收前的清单

- [ ] 补齐当时可交易成员、上市/退市及信息可得时间的 PIT 证据，避免静态名单选择偏差。
- [ ] 保持旧登记和旧结果不变。当前 `native` 对照是适配为现金现货的新研究配置，不等同于旧 `spot_margin` 账户结果；旧基线复现需使用原登记及原账户语义。
- [ ] 检查已预登记的四个评分窗口与三个 RL 种子真实运行结果，补充 RL 多窗口、候选与调参预算及多重试验处理。
- [ ] 审查重建持仓与固定 5 日分组的集中度结果，补充分组独立性和更多样本证据。
- [ ] 核对模型选择是否通过风控、实际仓位和成交发挥作用，并补充延迟与执行压力。
- [ ] 使用从未用于选参的最终样本及预登记门槛裁决；历史 `test` 只提供回顾性证据。
- [ ] 持续保存新鲜前瞻决策，追加成熟结果，完成前瞻表现与漂移评估；若最终候选为 RL，先接入完整账户与策略状态。

以上证据齐备前，工程状态可以报告为训练和历史对照完成；选币有效性、正式准入与前瞻验证保持待判定。

## V1 合并后的下一轮入口

后续六阶段实现使用独立的 `config/ml_selection_next.yaml` 和 `scripts/run_ml_selection_next.py`，详细命令、恢复范围及证据限制见 [2026-10-05 执行指南](ml_selection_next_execution_20261005.md)，实际验收见 [下一轮 Roadmap](ml_selection_next_roadmap_20261005.md)。原默认配置保留，新协议使用完整历史回合，不采用短回合采样。

新增内容包括统一候选／订单／持仓关联、净权益奖励对账、真实退出干预 probe、真正的 LambdaRank 及独立训练收益门槛、成熟的 12／24／36 月学习曲线、PIT 来源区间审计、至少 20 次实际更新和延迟早停、验证概率门槛冻结，以及开仓延迟、参与率和资金受限压力。正式预算按三个种子、两个训练／验证窗口登记为至少 120 次实际更新；以全部六个预算回执为准，pilot 不计入正式更新。

公开采集保留已收盘日线、当前资格原始回复、实际接收时间和内容身份；前向评价同时核对协议与候选冻结时间，要求预登记的 30 个有效真实决策日期及成熟结果。源码归档、历史训练完成和真实前向效力分别记录，历史成员覆盖缺口和未打开的最终样本不因工程完成而通过。
