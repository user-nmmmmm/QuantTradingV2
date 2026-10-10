# 机器学习选币:现状、操作与后续计划(合并)

> 合并自选币器的操作说明、工程整改、回测开关与下一阶段 Roadmap。历史运行与诊断见 [ml_selection_history.md](ml_selection_history.md)。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/ml_selection_roadmap.md -->
## 机器学习选币：训练入口与 Roadmap

本实现把历史数据准备、评分模型训练、原引擎组合对照、奖励驱动训练和影子评分串成一条可复现的离线流程。默认使用 60 个已登记币种、日线、现金现货和原有交易策略。选币器改变候选是否入选及排序；仓位、退出、风控、费用与成交继续由现有引擎处理。

**完成历史训练不等于证实策略有效。** 当前数据来自已经研究过的历史区间及静态币池，报告始终标记为 `retrospective_research_only`，不会将历史 `test` 分区标成独立最终验收，也不会启用实盘或提交真实订单。

本次完整运行已保存在 `reports/ml_selection_full_20261004_v3`，60 个币、129,423 条候选记录，训练主线及四个滚动窗口共用时 502 秒。RL 三个种子因验证早停分别完成 4、5、4 个回合。新增 142 项测试、原有 98 项回归测试及 6 个子测试通过；协议、源文件、数据和 586 个输出文件的身份检查通过，所有模型可重新加载。详细证据在该目录的 `execution_receipt.json`。

本轮有效性未通过。评分模型验证期 Ridge 为 −2.79%、LightGBM 为 −0.74%；三个 RL 种子的最佳验证策略均保持现金，冻结候选的 `validation_qualification_passed=false`。历史测试期原规则 −4.10%、Ridge +3.33%、LightGBM −2.99%、RL 0%；Ridge 的测试盈利可以作为后续研究线索，但不能看完测试再改选它。完整解释和四个滚动窗口结果见该目录 `report.md`。R8 已检查日志入口，当前冻结数据截止 2026-09-18，未取得新鲜前瞻证据；冻结候选为 RL，还需要账户与原策略状态的前向桥接。

### R0—R8 当前状态

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

### 模型如何学习

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

### 训练输入的交付前提

以下命令均从仓库根目录执行。默认配置引用的登记和行情位于被 Git 忽略的研究目录；代码仓库不分发本地行情、模型和训练结果。全新 clone 可以运行合成输入测试，默认历史训练需要先恢复可信的原始输入包：

- `reports/multicoin_100k_20261004/registration.json`。
- `reports/multicoin_100k_20261004/input/engine/<SYMBOL>.csv`，包含登记的 60 个币种。
- `reports/smart_capital_100k_20261004/registration.json`，包含原 smart 配置与引擎选项。

保持登记与 CSV 字节、文件名及目录关系一致；程序逐项核对文件和解析后行情身份。不能用新下载的数据覆盖旧登记或手工重写哈希。若使用其他可信数据包，应创建对应的新登记与实验配置。旧基线复现还需要原登记绑定的参考报告，详见 [V1 实施总结](ml_selection_history.md)。

只检查工程且禁止训练时，安装研究依赖后运行 `python scripts/check_ml_code.py --pytest-arg=-q`。该入口只运行显式清单，阻断真实模型拟合与策略更新；含合成模型训练的完整 ML 套件不在清单内。工程整改、数据与校准合同见[代码整改记录](ml_selection.md)。完整 `python -m pytest -q tests/test_ml_selection_*.py` 使用合成行情并包含真实拟合，不依赖上述历史目录；GitHub CI 单独安装可选 ML 依赖执行完整测试。

### 在这台电脑上启动

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

### 分阶段运行与恢复限制

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

### 输出怎样查看

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

### 核对旧历史基线

ML 入口采用现金现货的新研究账户；旧 `spot_margin` smart 基线继续使用原登记配置，禁止用新账户结果冒充旧基线。

独立复现入口如下，输出目录必须尚不存在：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\verify_ml_selector_baseline.py" --output "reports\ml_selector_baseline_check"
```

本次旧基线复现证据覆盖 2453 个日历日、664 次成交，权益最大绝对差为 0，并核对执行摘要、资金分配摘要及会计一致性。它证明默认旧基线在可选 ML 接口加入后得到保留，不证明 ML 模型有效。新训练实验仍需分别完成真实运行并检查结果。

### R8：提供新行情后进行影子评分

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

### 完成研究验收前的清单

- [ ] 补齐当时可交易成员、上市/退市及信息可得时间的 PIT 证据，避免静态名单选择偏差。
- [ ] 保持旧登记和旧结果不变。当前 `native` 对照是适配为现金现货的新研究配置，不等同于旧 `spot_margin` 账户结果；旧基线复现需使用原登记及原账户语义。
- [ ] 检查已预登记的四个评分窗口与三个 RL 种子真实运行结果，补充 RL 多窗口、候选与调参预算及多重试验处理。
- [ ] 审查重建持仓与固定 5 日分组的集中度结果，补充分组独立性和更多样本证据。
- [ ] 核对模型选择是否通过风控、实际仓位和成交发挥作用，并补充延迟与执行压力。
- [ ] 使用从未用于选参的最终样本及预登记门槛裁决；历史 `test` 只提供回顾性证据。
- [ ] 持续保存新鲜前瞻决策，追加成熟结果，完成前瞻表现与漂移评估；若最终候选为 RL，先接入完整账户与策略状态。

以上证据齐备前，工程状态可以报告为训练和历史对照完成；选币有效性、正式准入与前瞻验证保持待判定。

### V1 合并后的下一轮入口

后续六阶段实现使用独立的 `config/ml_selection_next.yaml` 和 `scripts/run_ml_selection_next.py`，详细命令、恢复范围及证据限制见 [2026-10-05 执行指南](ml_selection_history.md)，实际验收见 [下一轮 Roadmap](ml_selection.md)。原默认配置保留，新协议使用完整历史回合，不采用短回合采样。

新增内容包括统一候选／订单／持仓关联、净权益奖励对账、真实退出干预 probe、真正的 LambdaRank 及独立训练收益门槛、成熟的 12／24／36 月学习曲线、PIT 来源区间审计、至少 20 次实际更新和延迟早停、验证概率门槛冻结，以及开仓延迟、参与率和资金受限压力。正式预算按三个种子、两个训练／验证窗口登记为至少 120 次实际更新；以全部六个预算回执为准，pilot 不计入正式更新。

公开采集保留已收盘日线、当前资格原始回复、实际接收时间和内容身份；前向评价同时核对协议与候选冻结时间，要求预登记的 30 个有效真实决策日期及成熟结果。源码归档、历史训练完成和真实前向效力分别记录，历史成员覆盖缺口和未打开的最终样本不因工程完成而通过。


---

<!-- 合并自 docs/ml_selection_engineering_roadmap.md -->
## ML 选币工程整改：先完成代码，暂停训练

本批基于 `main` 的 `373cb6ae11c2ce11a38bfa59be6ea94b4d7d8fef`（PR #50）。执行范围为前次审查的工程缺陷、研究接口和验证工具。没有启动模型拟合、RL 更新、阈值搜索、行情下载或历史研究账户重跑。冻结权重和默认门槛保留作为后续对照。

### 问题与代码交付

| 阶段 / 问题 | 本批代码交付 | 后续需要的真实证据 |
|---|---|---|
| R0 / 近乎全拒绝、低活动难解释 | 候选、数据资格、实际评分、有效 gate 与拒绝原因的漏斗；概率明确为动作概率；端到端及准备 / 引擎 / 报告阶段计时 | 原 60 币输入与逐账户账本，同配置经济复验 |
| R0 / 原输入包缺失 | 只读 readiness 审计登记、文件与模型身份、训练 / 标签 / 行情水位、账户、PIT 和 final 状态 | 恢复登记绑定的原始 CSV、protocol 与账本；缺失保持 pending |
| R1 / V3 静默绕过 selector | CLI、网页准入、Engine 与共享 EventProcessor 拒绝未支持的组合 | 将来实现 V3 候选与目标权重桥接后再开放 |
| R1 / 多次上市接口不一致 | membership 审计与 dataset 共用非重叠区间和信息可得时间判定 | 完整、可核验的历史成员资料 |
| R1 / 数据来源和版本不清楚 | UTC 水位、原 frame hash、真实报价额 / 代理来源、可选严格资格要求 | 新数据版本与来源，不能覆盖旧登记 |
| R1 / 推理重复准备 | feature-only builder，BTC benchmark 特征单次构建；不计算未来标签 | 真实全量运行端到端性能测量 |
| R2 / 过滤、排序与资金权重混在一起 | 四项 selector contract，排名分数与资金 score 分离；只生成冻结消融计划或比较已有配对账本 | 同数据、账户、费用、退出与风控的独立账户重放 |
| R3 / 全币日代理标签偏离策略域 | 从权威 decision / order / fill / lot-close 构建策略候选数据；实际退出、代理、配对边际目标分开；未知与同日 cohort 保留 | 真实成熟候选、融资成本归属、足够完整竞争组 |
| R3 / 账户迁移、训练水位旧 | 显式训练 / 部署账户契约，近期滚动 train_start，独立早停 / 校准 / 开发检验范围，试验预算与样本不足记录 | 数据恢复后使用新协议；本批不训练 |
| R4 / RL gate 与奖励语义不清楚 | legacy policy-only 默认与 opt-in gate 交集分开；采样动作、实际有效 gate、账户奖励归属分开记录；可选策略 / 风险 / 竞争上下文 | 训练后验证增量价值和信用分配方案，不能把账户奖励当单候选利润 |
| R5 / 漂移、零成交和准入边界 | 只读缺失率、陈旧水位、候选覆盖、概率分布、拒绝原因与实际账户零成交日监测；复用现有前向证据合同 | 真实前向观察、标签成熟、独立模拟成交账户与执行压力 |

代码支持并不证明收益改善。模型效力、数据补齐和正式放行仍须对应证据；`formal_admission` 保持 false。终端流动性失败、未平库存或缺账本的最终收益保持未知。

### 无训练的验证入口

从仓库根目录执行，使用已安装正式 / 开发 / 可选 ML 依赖的 Python：

```bash
python scripts/check_ml_code.py --list-tests
python scripts/check_ml_code.py --pytest-arg=-q
```

这是显式审核过的工程测试清单，涵盖纯数据、模拟训练编排、原引擎与冻结模型推理。guard 在收集测试前阻断真实 fit、scaler 拟合、RL update、LightGBM 和 sklearn 训练调用；意外调用会使测试失败，不会跳过断言。它只保护当前测试进程，不是任意子进程的沙箱。

完整 `tests/test_ml_selection_*.py` 包含真实合成模型训练，暂停训练期间不要使用该通配入口。模型训练正确性的完整验收需留待用户重新授权训练。

### 只读检查与消融计划

```bash
python scripts/audit_ml_readiness.py --account-mode spot_margin \
  --output /tmp/ml-readiness.json
python scripts/plan_selector_comparison.py --account-mode spot_margin \
  --output /tmp/ml-comparison-plan.json
```

两条命令不训练、不下载、不运行回测。没有原始登记时照样给出模型身份和明确的 pending 清单。恢复原协议后用 `--protocol` 提供 hash anchor；消融计划可用 `--controls` 提供完整账户条件。已有完整账户回执可通过 `--left-ledger` / `--right-ledger` 比较；合同、完整日历、会计或终端执行不匹配时净收益差为 null。

原条件对照依次包括 off、仅资格、仅排名、仅收益 gate、仅 RL gate、组合，以及动量 / 随机排名基线。排名方案另固定 original_score / selector_score 资金来源。`selector_enabled`、构造参数和四个布尔 flags 在计划中独立记录，后续执行不得将计划当已运行结果。

漂移审计可增加 `--reference`、`--observations`、`--activity`。前两者是已有特征记录，activity 是独立实际账户的逐日成交回执。selector 全拒绝与账户零成交分别统计；缺 activity 不能推断零成交。`--forward-store` 只验证已有前向证据。

### 候选数据与新协议

```bash
python scripts/build_selector_candidates.py \
  --run-dir reports/EXISTING_FROZEN_RUN --episode episodes/test_native \
  --labels-as-of 2026-10-04T00:00:00Z --target-type actual_exit \
  --output /tmp/NEW_CANDIDATE_ARTIFACT
```

源实验需要已有 protocol、dataset、decision、fill 与 close-event 账本。输出必须是新目录且位于源实验外。工具不以 symbol / 时间猜测订单联接，也不强制平仓补标签。实际成交目标是观察到的策略账户退出收益，存在成交选择条件；不是拒绝候选的反事实，也不是单候选组合边际贡献。非现货账户缺融资成本归属时目标未知。

候选数据通过 `load_candidate_training_rows()` 校验 contract、CSV、原协议与源账本哈希，只接受完整成熟的账户日组。没有完整组会明确报数据不足。新协议可登记下面的数据入口；拟合与早停读候选域，推理和账户评价仍使用独立的完整因果行情表，避免候选表缺失未来日期或重复策略行污染服务：

```yaml
training_data:
  candidate_dataset_directory: /ABSOLUTE/PATH/TO/CANDIDATE_ARTIFACT
  target_type: actual_exit
  data_identity: EXACT_VALUE_FROM_CONTRACT
```

冻结协议记录该外部产物的身份，恢复时再次校验。实际退出样本仍有成交选择条件，不因换了数据入口就成为独立结果或边际组合目标。

新实验可显式登记 `splits.train_start`、`splits.calibration_end` 和 `evaluation_protocol`：

```yaml
account_mode: spot_margin
splits:
  train_start: "2021-01-01"
  train_end: "2023-01-01"
  validation_end: "2024-01-01"
  calibration_end: "2024-07-01"
evaluation_protocol:
  deployment_account_modes: [spot_margin]
  maximum_validation_trials: 1000
  maximum_calibration_trials: 100
  minimum_event_groups: 6
```

这是现有注册期内的代码配置示例，不是建议训练窗口或统计样本门槛。日期字段应使用字符串；所有滚动窗口也必须登记校准区间，试验预算须覆盖已登记种子 / checkpoint / 窗口 / 阈值尝试。先在早停验证区选 seed / checkpoint，再仅对冻结 winner 校准，开发检验不参与选择。旧协议会明确报告 validation 复用，不能自动改写为独立证据。

新 final 必须在真实开始之前预登记，位于所有开发数据之后。已有单候选、未打开的 final 合同保持其身份；新候选不能借用已开始的历史窗口。以上任务完成后，下一步是恢复和登记数据、执行公平对照，再决定是否训练；本批不执行这些步骤。

新 policy 可通过 `rl.context_features` 登记 `native_candidate_score`、`stop_distance_fraction`、`batch_candidate_count`、`health_risk_multiplier`、`market_risk_multiplier`、`portfolio_risk_multiplier`、`requested_notional_fraction`、`candidate_is_short` 的所需子集。值来自 selector 调用时的真实候选和运行状态，缺失会拒绝该候选并说明未知项；不会读取之后的 allocation 审计。上下文的初始缩放是明确记录的零参考 / 单位尺度，不宣称任意币日有这些历史策略状态。冻结旧 policy 的 21 个行情与 5 个账户输入保持原顺序。

`selection.policy_gate_mode` 可显式设置 `policy_only` 或 `policy_and_return`，新政策元数据随模型冻结。修改 gate 或阈值属于新研究配置，需要完整账户评价；本批没有通过调整门槛来制造更高成交数。前向 bridge 捕获和校验决策时上下文，使新输入可以逐候选回放。

### 本批验收

- 显式无训练 guard 清单：20 个模块、400 项通过，无失败或跳过；真实模型 / scaler 拟合与 RL 更新被阻断。
- 原引擎、共享运行时、网页任务及 V3 等运行时回归：171 项通过。
- Ruff、CI 必需的 3 文件 Mypy、另 8 个新增 / 修改核心源文件 Mypy、数据与 loader 4 文件 Mypy、依赖锁、49 份受保护历史文件、仓库保留规则、Roadmap 结构、diff whitespace 校验通过。
- 数据对照：1,439 行旧 / 新 21 个特征、资格与代理标签精确一致；冻结模型合成原引擎对照的权益和交易账本一致。该场景为 6 个 selector 决策、零成交，覆盖范围有限，不是实际收益改善证据。
- 真实冻结包 readiness CLI：两个模型文件 hash 与内部身份通过，`failed_reasons=[]`，状态为 `pending_external_evidence`。12 个消融 arm 均为 `planned_not_executed`。

扩大检查既有 Engine 和研究模块的 Mypy 仍有与 `373cb6a` 原始快照相同的旧错误；它们不在 CI 必需范围，本批没有声明这些模块全量类型检查通过。暂停训练期间也没有执行含真正拟合的完整 ML 测试。

机器回执、Junit 与只读 CLI 报告保存在本次云环境的 `/workspace/scratch/quant-ml-code-verification/`，不作为历史金融研究产物提交。环境配置草稿已保存可选 CPU ML 依赖与默认无训练启动检查，尚未发布。


---

<!-- 合并自 docs/backtest_coin_selector_20261005.md -->
## 回测选币器开关与原 10 万本金配置

已新增回测选项“开启选币器”，默认关闭。

- 关闭：使用原策略产生的候选交易，保留原有风控和资金分配。
- 开启：使用当前冻结的 LightGBM + RL 选币器筛选候选交易，保留同一次回测的账户、风控和资金分配设置。执行时不训练模型、不调整阈值。

### 页面使用

进入“回测实验室”，在初始资金下方选择是否“开启选币器”。

要复现原来的 17 万权益结果，点击开关下方的 **“复现原 10 万本金回测”**。该按钮使用原报告登记的 60 币、2020-01-01 至 2026-09-18、100,000 USDT 本金、原智能资金分配和账户设置；普通表单中的标的、日期、本金不会覆盖这套固定配置。取消勾选运行原策略，勾选运行当前选币器，两组输入相同。

“运行本地回测”继续使用普通表单中的自定义设置。自定义回测关闭选币器时使用原策略，但收益取决于填写的日期、标的和资金等参数，因此不必然等于 17 万。

任务记录、结果详情及复制参数会保留开关状态。复制固定配置的任务会恢复开关，并提示点击固定配置按钮。没有记录开关的旧任务按关闭处理。

### 同一固定配置的实际结果

2026-10-05 已重新执行两组完整回测：

| 项目 | 关闭选币器 | 开启当前选币器 |
| --- | ---: | ---: |
| 初始资金 | 100,000.00 USDT | 100,000.00 USDT |
| 期末权益 | 172,821.74 USDT | 116,391.52 USDT |
| 净利润 | 72,821.74 USDT | 16,391.52 USDT |
| 总收益率 | 72.8217% | 16.3915% |
| 最大回撤 | 8.6909% | 6.5011% |
| 成交记录数 | 664 | 85 |
| 权益日历 | 2,453 天 | 2,453 天 |
| 账户对账 | 通过 | 通过 |

关闭组的 2,453 天逐日权益与原报告完全一致，最大绝对差为 0；最终权益、成交摘要、资金分配摘要和成交数量均匹配。这里的“17 万”指期末总权益，利润约为 7.28 万。

开启组使用相同的 60 币、日期和原 `spot_margin` 账户，其收益与此前使用不同日期及现金现货账户的 ML 研究报告不同。本固定区间包含模型训练和验证期间，因此属于历史回顾对照，不能作为独立样本外收益。模型训练截止为 2023-01-01（不含），验证截止为 2024-07-01（不含）。

原始结果位于：

- 关闭：[summary.json](../reports/selector_toggle_verification_20261005/original_off/summary.json)
- 开启：[summary.json](../reports/selector_toggle_verification_20261005/original_on/summary.json)
- 原历史基准：[summary.json](../reports/smart_capital_100k_20261004/smart/summary.json)

### 命令行

在项目根目录执行，输出目录必须尚未存在：

```powershell
.venv/Scripts/python.exe scripts/run_selector_backtest.py --coin-selector off --output-dir reports/selector_off_new
.venv/Scripts/python.exe scripts/run_selector_backtest.py --coin-selector on --output-dir reports/selector_on_new
```

固定配置入口逐一核验原登记文件、121 个输入文件及 60 个行情数据表，保留原账户模式，并在关闭时校验原结果。

普通 `main.py` 回测也支持 `--coin-selector off` 和 `--coin-selector on`，默认 `off`。开启目前仅支持日线 `1d`。可用 `--selector-bundle` 指定已冻结的模型包，该参数仅允许在开启时使用。

### 模型与复查

当前模型包为 `config/ml_selector_current.json`，策略标识为 `9db8c77c77e58997798c005b016f34a07bfb85a3fb7d77eacdce8f25bb8b851f`，父模型标识为 `1e7f2d876452069b06dc2dc0702dfcc430c35c643d1b44688ccb98c7773cdb90`，筛选阈值保持 0.51。

页面任务在提交时保存所选模型的完整副本，排队期间更新当前模型不会改变已提交任务。所有报告模式均写入 `coin_selector.json`；开启时还保存 `coin_selection.csv` 和 `selector_inputs/`，记录候选筛选依据及实际模型文件。普通完整报告的重放从当次报告副本读取模型；缺失或损坏会明确失败。

验证覆盖默认关闭、真实引擎两组对照、三种报告模式、开启报告重放、模型副本与校验失败、页面开关与固定配置请求、任务历史及复制参数。关闭组未加载 ML 模型；新增选项仅作用于回测任务。


---

<!-- 合并自 docs/ml_selection_next_roadmap_20261005.md -->
## 机器学习选币：V1 合并后的研究 Roadmap

日期：2026-10-05（Asia/Singapore）。V1 已于当日 00:37 合并至 `main`，对应 [PR #48](https://github.com/user-nmmmmm/QuantTradingV2/pull/48)，合并提交为 `77f9ca4198f5cd52d5c1cc32c2d56629f1fad2fc`。

**合并基点的离线工程完成，V1 模型有效性未通过；本轮正式训练与历史研究报告已完成，正式效力准入仍未通过。** 合并基点三个 RL 种子的最佳验证策略均保持现金。本轮在静态 60 币注册范围完成六个正式训练 cell、124 次实际更新、主窗口十个账户与四滚动窗口 37 个账户，并完整报告四窗口 20 项费用/执行压力尝试，其中两项真实终端流动性失败保留未知最终指标。已完成训练或报告不等于压力全通过，也不替代完整历史 PIT、真实前向账户及独立最终样本证据。

合并基点的远端 quality 检查为 3,487 项测试与 111 个子测试通过、2 项跳过，覆盖率 88.32%；ML 专项 154 项通过。修复后旧基线再次复现 2,453 日、664 次成交，逐日权益差 0，会计、执行与资金分配摘要通过。这些是工程及基线证据，不能代替模型效力结论。

本轮最终 source6 本地工程验收为 **3,623 项测试、111 个子测试通过，1 项跳过，覆盖率 88.33%**（要求至少 55%），完整套件耗时 630.71 秒。Ruff、关键运行契约类型检查、依赖锁、环境、49 份受保护历史文件、仓库保留规则与统一 Roadmap 结构检查均通过；六个正式 cell 的 124 次更新、372 次登记验证门槛评价及 26 个模型文件独立核对成功。source6 桥接 144/144 完整一致且经济路径不变。完整工程结果见[质量回执](../reports/ml_selection_next_full6_verification_20261005/quality_receipt.json)；研究验收结果与外部未完成项见[最终验收记录](research/ml_selection_next_acceptance_20261005.json)。上述工程通过不改变 `formal_admission=false` 和 `production_enabled=false`。

本文是选币研究的后续专项安排，不替换 [项目统一 Roadmap](unified_roadmap.md)，也不改变正式策略、风控或实盘放行状态。已完成批次详见 [V1 实施与研究复核](ml_selection_history.md)，现有操作见 [训练入口与工程说明](ml_selection.md)。

### 当前证据与可复用能力

V1 合并实验的成熟训练数据覆盖 55 个币、1,077 个决策日期。日线数据量不等于独立学习机会：20 日窗口标签彼此重叠，同日币种也高度相关；反复重放同一历史不会增加独立市场样本。

按运行方案只读复核后，验证期原规则与合格候选原规则均为 **51 个候选日期 / 21 个竞争日期**；原文的 52/21 对应 LightGBM 方案，不能概括为所有方案的原策略机会。历史测试期原规则为 **60/21**，其他方案分别报告。实际产生横截面竞争的机会远少于完整特征表行数。复核表见[候选日期对账](../reports/ml_selection_diagnostics_verified_20261005/candidate_date_review.csv)。

主要交易方案的平均敞口约为 1.96%—1.97%。RL 最佳验证概率约在 0.488—0.500 以下，确定性 `p≥0.5` 门槛使候选全部被拒绝。这些结果同时涉及原策略机会、账户状态、资金分配、策略健康、风控和学习目标，不能仅解释为模型不够复杂。

| V1 可以复用的已有功能 | 本轮新增及仍待取得的证据 |
|---|---|
| 原引擎可选候选 hook，默认关闭 | 新运行关联标识、分层漏斗及同批次原 allocator 对照已实现；旧运行缺失的上游归因保留未知 |
| 因果特征、代理标签、成熟时间和跨界清除 | 退出干预契约与原引擎探针已验证；真实退出目标未替换训练代理标签，组合边际价值未测量 |
| Ridge、CPU LightGBM 回归与模型身份 | 真正 LambdaRank、独立训练期净收益 gate、排序/校准及三模型 12/24/36 月共 9 条实际曲线完成；正式主窗口与滚动对照已报告，曲线来源仍为 source3 |
| 完整原引擎回合、净权益奖励与真实成交换手 | 奖励分解、预登记门槛、最低更新、延迟早停与恢复预算已实现；本轮采用完整回合 |
| 三个 RL 种子、已完成回合 checkpoint | 9 个注册奖励/种子 pilot 单元完成 18 次更新、正式贡献 0；正式三种子、主窗口及 WF4 共六 cell 完成 124 次实际更新，全部达到至少 20 次 |
| 四个评分滚动窗口、成本压力及集中度检查 | 四个窗口 9/9/9/10 个 test arm 与 20 项费用/执行压力尝试全部报告；WF2/WF3 低参与率退出失败最终指标为 null，事件集中不足和未通过裁决完整保留 |
| 影子观察和成熟代理结果追加入口 | 8 币 pilot 的 52 个历史快照一致；source5 的完整 audit 136/144 不一致记录保留；source6 修复审计装饰后 144/144 动作与完整 audit 一致、0 验证错误，经济路径字节和七个标量精确相同；真实 30 日期、成熟结果和模拟账户仍未完成 |

V1 的 LightGBM 使用回归目标。本轮新增的 `lambdarank` 使用真实 LightGBM 排序目标，将同一 `as_of` 的候选排列成连续组，并冻结组内整数相关性构造。排序模型的 `predict` 只表示相对次序；`predict_net_return` 使用仅在成熟训练材料拟合的独立 Ridge gate，验证标签不参与收益 gate 拟合或校准。排序分数、预测净收益和策略选择概率分别报告，RL 的入选概率不解释为盈利概率。

### 本轮已取得的证据与待判定事项

本节记录已完成的正式历史研究与此前有界验证，不改变原六阶段依赖和正式策略准入。8 币 pilot、旧回合只读复核、退出探针修复验证以及 source3/source4/source5/source6 分别保留独立产物；失败运行和旧身份不覆盖。当前最终汇总入口是 source6，历史评估的实际来源仍为 source4/source5。

| 范围 | 已完成证据 | 解释边界 |
|---|---|---|
| V1 旧回合只读对账 | 77 个旧回合的权益、成本、奖励和终止对账全部成功；22 个回合净收益为正但总奖励为负 | 惩罚影响了优化目标；旧上游拒绝记录缺失时不能重建完整漏斗，旧历史仍为开发材料 |
| 正式监督、学习曲线与分配对照 | 静态 60 币注册范围的回归、LambdaRank、训练期收益 gate 与主/滚动组合评价实际运行；9 条学习曲线来源 source3。正式同批次对照测量 12 个批次，11 个预算受限、1 个批准集合/数量变化 | matching_executed=false、portfolio_return_difference=null；allocator 影响已测，配对撮合收益与组合边际价值未测；早期 8 币 4 个批次/0 个预算受限是另一范围 |
| 正式原策略退出探针（来源 source3） | 56 币诊断子集，12/12 完整原信号复现；11 个真实成交并闭合目标，1 个 below_minimum_notional 未成交未知目标；原健康与保护政策保留 | 56 币子集不是全 60 币逐币验证；此前独立修复验证的 9/3 是另一范围；未成交不写成收益 0，组合边际未测，训练仍使用代理目标 |
| 注册奖励与种子资源 pilot（来源 source3） | 9 个预登记单元完成、0 个失败，共 18 次实际参数更新；进程生命周期工作集峰值 933,494,784 bytes | 正式贡献为 0；这是静态 60 币 run 的 pilot 资源，不能沿用早期 8 币约 334 MB；工作集峰值不等于 cell 增量内存 |
| 正式 RL 训练预算 | 主窗口 20/23/20、WF4 21/20/20，共 124 次实际更新；六 cell 达标并按真实 validation_early_stop 停止，source3 103 + source4 21 + source5 0 | 导入训练保持原协议和原回合来源，未证明 source4/source5 重训位级等价；pilot、winner receipt 与恢复调用不重复计数 |
| 四窗口与执行稳健性 | 主窗口十个账户，四滚动窗口 9/9/9/10 个 test arm；四窗口 20 项压力尝试全部报告，其中 WF2/WF3 低参与率为 failed_end_window_liquidity | 两个失败情形的最终净收益、回撤、reward、accounting 和风险终止均 null；37 个 test 账户账务通过、无风险终止，但四个窗口均 retrospective_checks_passed=false |
| RL 状态桥接 | source6 144/144 个固定策略原引擎快照的动作、decision-facts 和完整 audit 一致，all_decisions_identical=true、verification_errors=[]；source5 的 8 个 audit 不一致原报告仍保留 | 只修复已经拒绝策略行的风险审计装饰；权重、门槛和财务候选不变，新增训练与门槛搜索为 0；历史桥接不是新前向账户收益 |
| 真实前向与独立 final 登记 | 一次公开行情覆盖 55/60：32 合格、23 流动性不足、5 缺资格；冻结候选与协议在 2026-10-06 开始前登记，最大候选 1，final 未打开 | recorded_observations=1、verified_forward_decision_dates=0、mature_labels=0，模拟账户收益 null；完整历史 PIT、至少 30 个真实日期、真实账户/策略状态、成熟执行结果与独立 final 证据仍缺 |

证据入口：[77 回合只读对账](../reports/ml_selection_diagnostics_verified_20261005/audit.json)、[早期 8 币 pilot 回执](../reports/ml_selection_next_pilot2_20261005/pilot_execution_receipt.json)、[正式同批次资金分配对照](../reports/ml_selection_next_full5_20261005/fixed_batch_allocation_comparison.json)、[正式退出探针](../reports/ml_selection_next_full5_20261005/exit_label_probes.json)、[注册奖励资源 pilot](../reports/ml_selection_next_full5_20261005/pilot_resource_report.json)、[当前 source6 最终执行收据](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)、[完整稳健性解读](ml_selection_history.md)、[source5 原桥接不一致报告](../reports/ml_selection_next_full5_20261005/rl_bridge_verification.json)、[实际 source6 桥接](../reports/ml_selection_next_full6_20261005/rl_bridge_verification.json)、[桥接经济路径精确对照](../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)、[当前真实前向证据](../reports/ml_selection_next_full6_20261005/forward_evidence.json)。这些链接指向本地研究产物，完整输入和结果归档仍需单独保存。

正式研究的当前最终汇总入口为 `reports/ml_selection_next_full6_20261005`，协议 `b79dc942e38c70c40c00fa6f8bedcdeb63511cb05051899d972cd9f6896d6eb6`；历史矩阵由 source5 协议 `6f41249b58f54e3ed2719637b003d59dbd509a599231d2fbc64163e309db05df` 完整报告。第一正式运行在 next-2 阶段中断，第二目录在监督曲线阶段中断；两者均未发生 RL 更新，其旧产物与冻结源码保留。source3 之后完成五个训练 cell 的 103 次更新，但在 WF1 低参与率因空 lot 账本对应极小持仓残余而中断；仅修复 core/portfolio.py 的数值投影归零、84 项针对性检查和真实 WF1 压力重放通过后，source4 继续研究。

source4 新训练 WF4 seed42 完成 21 次，六 cell 正式预算共 124 次；主窗口与 WF1 评估完成，但 WF2 低参与率的真实期末流动性无法完成退出而中断。source5 只修改研究汇总路径，把精确预期的终端流动性失败保存为未知最终指标并继续其余尝试，不改原引擎撮合与终端约束。source5 直接继承 source4 已完成主窗口/WF1 评估，只新评 WF2/WF3/WF4 与桥接；训练来源保持 source3 103 + source4 21 + source5 0。旧训练、学习曲线与探针保持原封存目录和 hash，103 次旧更新不是 source4/source5 重训；五个导入 best policy 按原门槛固定验证在 source4 下 5/5 精确一致，也不证明全部训练轨迹等价。注册勘误与训练资源归档完整保留。

来源证据：[source3 中断](../reports/ml_selection_next_full3_20261005/interruption_receipt.json)、[source4 中断与完整训练](../reports/ml_selection_next_full4_20261005/interruption_receipt.json)、[注册勘误](../reports/ml_selection_next_full4_20261005/recovery_registration_erratum.json)、[五份固定策略迁移验证](../reports/ml_selection_next_full4_20261005/repair_compatibility/fixed_policy_validation_report.json)、[source5 混合来源登记](../reports/ml_selection_next_full5_20261005/recovery_provenance.json)。不能把最终汇总完成改写为 source3/source4 已完整成功。

source5 的正式桥接动作集合 144/144 一致，但完整 audit 仅 136/144 一致。source6 只修复 research/ml_selection/forward_bridge.py：对已被策略拒绝的行保留原审计事实，只在原选中行被风险预算撤销时添加风险拒绝装饰。source6 真实重放后动作与完整审计 144/144 一致，0 验证错误；与 source5 的 equity、trades、rewards、fill_ledger、closed_positions 五份经济文件 SHA 完全相同，净收益、回撤、敞口、换手、reward、fill_count、accounting 七个标量相同。新训练 0、新门槛搜索 0、新财务候选 false；四窗口和主窗口均直接继承原评估，source6 没有重评整个历史矩阵，也未证明重训等价。证据：[source6 来源](../reports/ml_selection_next_full6_20261005/recovery_provenance.json)、[经济路径精确对照](../reports/ml_selection_next_full6_20261005/bridge_repair_economic_equivalence.json)。

### 六个阶段的顺序与依赖

默认执行顺序为 **候选流诊断 → 标签与监督排序 → PIT 数据与学习曲线 → 奖励与 RL → 滚动稳健性 → 真实前向观察**。PIT 来源整理可提前并行准备，但后续效力结论必须说明采用的成员证据和数据范围。

| 阶段 | 优先级与依赖 | 主要交付物 | 推进条件 |
|---|---|---|---|
| 1 候选流与奖励诊断 | P0；以合并基点为起点 | 候选漏斗、执行归因、概率与奖励分解 | 能解释低敞口、全拒绝及真实竞争机会 |
| 2 标签与监督排序 | 依赖阶段 1 | 标签契约、回归/排序对照、预算一致的组合实验 | 学习目标对应可执行决策，排序影响实际选择 |
| 3 PIT 与学习曲线 | 依赖阶段 2 的目标定义 | 成员与数据证据、12/24/36 月学习曲线 | 有效样本可追溯，数据增加的影响可解释 |
| 4 奖励、门槛与扩大 RL | 依赖阶段 1—3 | 预登记奖励实验、更新预算、多个种子与窗口 | 奖励及评估行为可信，再投入更大预算 |
| 5 滚动与执行稳健性 | 依赖冻结的阶段 2—4 候选 | 4—6 个窗口、成本与集中度结果 | 稳定性、证据不足和失败都完整报告 |
| 6 真实前向影子 | 依赖冻结候选与阶段 5 | 实际时钟决策、账户状态、成熟结果与漂移记录 | 取得真实新数据和时间顺序完整的前向证据 |

以下勾选只表示注明范围内已取得对应验收证据。未勾选项继续开放；实现、pilot、正式预算完成、模型有效性和正式准入分别保留状态，门槛在新实验开始前冻结。

### 阶段 1：P0 候选流、奖励和概率诊断

先把一次决策连成可核对的链路：历史成员 → 数据合格 → 原策略信号 → 模型入选 → 排序 → 资金计划 → 风控批准 → 挂单 → 部分/全部成交 → 持仓 → 退出与权益。

统计每层的候选数、日期数、同批候选数、拒绝原因、目标与批准数量、未成交数量、现金和敞口。分别解释原策略没有机会、模型拒绝、资金不足、健康暂停、相关性/风险约束和实际撮合限制，避免将它们全部归入“模型未选中”。

对相同候选集合比较原评分、动量、模型评分和随机评分，检查预算真正不足时排序有没有改变入选或仓位。没有资金竞争的批次不能单靠排序变化证明选币价值。

逐回合保存奖励三项、概率分布、确定性门槛两侧数量、动作数及成交数。核对现金奖励、正收益但总奖励为负的回合，以及轻微概率变化为何造成全入选或全拒绝。

**保持生产风控与策略健康契约。** 通过诊断记录解释限制的作用；隔离研究消融使用新协议和清晰对照，不能把关闭保护后的结果当作正式系统能力。

验收与交付：

- [x] 新运行的决策关联标识、候选、批准数量、订单和成交链路已实现并通过原引擎回放验证；旧运行缺失的上游记录保持未知。
- [x] 输出按方案复核的候选/竞争日期及现金、敞口诊断：验证原规则与合格候选原规则 51/21、LightGBM 52/21，测试原规则 60/21；旧上游记录不足时明确标记归因未知，进一步低敞口归因由新运行完整漏斗补充。
- [x] 77 个旧回合的奖励账本与原引擎权益、成本、终止状态全部对账；22 个正收益但负总奖励回合单独报告。
- [x] 解释概率接近 0.5 导致确定性全拒绝的行为，预登记训练随机动作及验证门槛 0.49/0.50/0.51；门槛不使用历史 test 选择。

### 阶段 2：标签对应真实决策，先验证监督排序

保留当前固定窗口/ATR 代理标签作为明确基准，同时登记与原策略退出规则一致的标签实验。真实退出标签需要定义干预：在同一时点、同一可得信息和固定研究账户状态下，加入或替换哪个候选，用什么名义金额、风险预算、资金竞争与退出规则。

独立影子交易、实际成交结果和组合中的候选边际价值分别记录。不能将“旧系统没买”“资金不足”或“订单未成交”直接写成净收益 0 的训练目标；这类结果需要缺失、资格或执行标签，避免把选择偏差教给模型。

先比较简单回归、现有 LightGBM 回归和同一决策时点分组的排序学习。若新增 ranker，明确相关性标签构造及排序组，不能把现在的回归代码改名为排序模型。

排序指标只能验证候选相对次序；是否值得交易还需要另行检查预期净收益、真实成本和门槛校准。对照各组使用相同候选资格、账户、预算、退出和执行条件。

验收与交付：

- [x] 冻结代理标签与真实退出标签契约、固定现金账户/名义金额/风险预算、原策略退出和原引擎真实费用口径；本轮真实退出探针用于开发诊断，训练仍使用冻结代理目标。
- [x] 保留候选、完整冻结信号及拒绝原因。正式 source3 探针为 56 币诊断子集、12/12 原信号复现、11 个真实成交闭合目标、1 个 below_minimum_notional 未成交未知目标；信号不复现、未闭合或风险终止均不写成 0 收益。此前独立修复验证的 9/3 属于另一范围，不与正式探针拼合。
- [x] 静态 60 币正式范围输出同日排序、训练期收益 gate、校准与主窗口十 arm / 四窗口 37 arm 的完整原引擎组合结果，分清排序诊断与真实组合绩效；窗口资格未通过、负收益和失败压力均保留，效力未作通过判定。
- [x] 正式同批次原 allocator 对照已测量 12 个相同候选批次：11 个预算受限，1 个批准集合/数量变化，未测批次为 0；matching_executed=false、portfolio_return_difference=null，配对撮合收益和组合边际价值仍未测量。早期 8 币 4 个批次的零影响结果另行保留。

### 阶段 3：补 PIT 数据并做学习曲线

以当时可交易成员为依据扩大覆盖，纳入退市、交易停止和后来消失的资产，并保留上市/退市生效时间、消息可得时间及来源。当前静态 60 币不是完整历史币池。

可研究从 60 个币扩大至约 100—200 个历史成员，但这是范围提案，**不是验收数量门槛**。优先保证成员和行情时点可信；增加今天仍存在的币不能修复幸存者偏差。

在训练材料内部登记 12、24、36 个月的学习曲线，使用固定目标、特征、评价区间及预算，比较训练量、有效日期/事件组、排序和组合表现。学习曲线属于开发验证，不使用新的独立最终样本反复选窗口。

记录重叠 20 日标签、同日相关币及相同市场阶段的依赖。报告原始行数和按时间/事件分组后的样本范围，避免把更多行数当成等量独立证据。

`reports/ml_selection_next_full3_20261005` 已生成 Ridge、LightGBM 回归和 LambdaRank 各 12/24/36 月的 **9 条实际学习曲线**，由 source4/source5 保留原始结果与回合来源；没有将它们宣称为后续源码下的新拟合，也没有完成历史成员扩展。三种模型使用相同的 23,104 行成熟开发验证材料，评价决策区间为 2023-01-01 至 2024-06-10，标签成熟截点为 2024-07-01；各模型在三个训练长度之间保持自身参数、特征和评价预算一致。12/24/36 月对应成熟训练行数 16,142 / 34,316 / 41,980，决策日期 346 / 711 / 1,077，20 日时间块 19 / 37 / 55。时间块不是独立样本数量；重叠标签、同日币种和相同市场阶段仍有依赖。

| 模型 | 训练月数 | 原引擎验证净收益 | 最大回撤 | 实际成交次数 |
|---|---:|---:|---:|---:|
| Ridge | 12 | −3.7634% | 7.1018% | 20 |
| Ridge | 24 | −1.8264% | 7.8881% | 125 |
| Ridge | 36 | −2.7891% | 8.2604% | 124 |
| LightGBM 回归 | 12 | 0.0000% | 0.0000% | 0 |
| LightGBM 回归 | 24 | −0.7384% | 7.0081% | 132 |
| LightGBM 回归 | 36 | −0.7384% | 7.0081% | 132 |
| LambdaRank + Ridge 收益 gate | 12 | −3.7634% | 7.1018% | 20 |
| LambdaRank + Ridge 收益 gate | 24 | −2.8600% | 8.6943% | 126 |
| LambdaRank + Ridge 收益 gate | 36 | −4.6808% | 9.9851% | 116 |

增加训练量**未一致改善验证组合收益**：Ridge 和 LambdaRank 从 12 到 24 月损失减少，但 36 月再次恶化且最大回撤继续增加；LightGBM 的 12 月方案保持现金，24 和 36 月均为约 −0.7384%，不能把无成交的 0 收益当作活跃交易有效性。全部 9 个结果均保留，未按最好月份挑选结论。各回放会计检查通过且没有风险终止，但这些事实不代表模型已有效。

排序和校准诊断与组合表现分别解释。LambdaRank 的同日平均 Spearman 为 0.2332 / 0.2485 / 0.2358，Ridge 收益 gate 的 RMSE 从 26.94 个百分点降至 25.45、24.78 个百分点，组合净收益仍全部为负；排序相关性或代理目标误差改善不能代替真实资金分配及组合收益改善。本次使用静态历史研究范围，完整历史 PIT 条件为 **false**，没有独立最终样本，也不绘制无独立性依据的误差带或置信区间。

数据见[9 条学习曲线原始结果](../reports/ml_selection_next_full3_20261005/learning_curves.json)与[source5 中保留的曲线](../reports/ml_selection_next_full5_20261005/learning_curves.json)；可分享的标准 Matplotlib 双面板图见[验证净收益与最大回撤](research/ml_selection_next_learning_curves_20261005.png)。图中各点对应独立重置账户的同一区间评价，未拼接账户路径。曲线本身不代替 RL 或滚动证据；当前正式 RL 更新与四窗口报告的完成事实分别见阶段 4/5，完整 PIT 与独立泛化仍未完成。

验收与交付：

- [ ] 取得完整历史成员范围及上市/退市证据并按决策时点追溯。来源、生效/可得时间验证与缺证据排除/降级已实现，当前静态币池仍未补齐退市和后来消失资产，不声称完整 PIT 覆盖。
- [x] 12/24/36 月曲线已实现，8 币 pilot 和 full3 的三模型共 9 条曲线均实际运行：只使用严格早于训练边界成熟的完整同 `as_of` 组，独立拟合训练预处理；目标、特征、评价区间和各模型自身参数预算保持一致。
- [ ] 完成历史成员扩展后，核对扩展前后候选资格、账户、预算、退出与执行口径；尚未把新增今天仍存在的币当作幸存者偏差修复。
- [x] 已解读本次固定研究范围的 9 条训练量曲线：增加训练量未一致改善开发验证组合收益；原始行数、决策日期、时间块及同日/市场阶段依赖均报告。完整历史 PIT 与成员扩展仍未完成，独立泛化和最终效力不作通过判定。

### 阶段 4：奖励与门槛确认后，再扩大 RL

先预登记纯净权益对数奖励，以及不同回撤加深惩罚权重的对照。真实成本继续进入净权益，额外换手惩罚的经济目的单独说明。现金保持合法，不通过固定惩罚迫使模型交易。

预登记随机训练动作、确定性评估和门槛选择的关系。检查奖励尺度、熵、梯度和概率，明确收益门槛、入选概率门槛与排序分数的差异；门槛只在训练/验证材料中选择并冻结。

减去市场基准可以用于报告相对表现，或构造策略梯度的方差控制基线。在相同评价时段中，减去与动作无关的常数不会自动改变最优策略；单纯减掉基准收益并不能解决现金胜出。若终止时段或基准暴露随动作变化，要重新审查目标含义。

扩大训练的提案为：每个种子 20—30 次**实际参数更新**，三个种子、两个训练/验证窗口，共约 120—180 次更新。这个预算以阶段 1—3 的结果为前提，不承诺收益，也不等于新增独立样本。

最低实际更新数、延迟启动早停、已完成更新 checkpoint 和可恢复预算现已实现并测试。`--rl-episodes` 仍表示回合上限，正式完成以各单元实际更新数和回执为准，不能只看回合上限。本轮使用完整原引擎回合，短回合/分段采样为 N/A；如后续采用，须另建协议核对账户、热身、持仓路径、终止价值和抽样偏差。

验收与交付：

- [x] 奖励与评估门槛预登记，现金保持合法，无固定强迫交易奖励；9 个奖励/种子 pilot 单元完成，纯净权益及不同回撤加深惩罚分别报告，费用不重复扣除。
- [x] 最低实际更新、延迟早停与可恢复预算已实现、测试并实际完成：静态 60 币注册范围六个正式 cell 共 124 次更新，全部达到至少 20 次；pilot 的 18 次更新贡献为 0，winner receipt、辅助执行副本和 0-update 恢复调用不重复计数。
- N/A（本轮未采用）：若后续采用短回合，另行核对状态起点、成本、未平仓价值和抽样偏差。
- [x] 已比较三个种子、主窗口及 WF4 两个训练/验证窗口，并报告最佳 checkpoint、原验证门槛、真实更新、种子差异与合法现金结果；主窗口预算 20/23/20、WF4 21/20/20，全部 validation_early_stop 在至少 20 次之后生效。训练是重复历史参数步骤，不是六份独立市场样本。

| 种子 | 主窗口实际更新 / 来源 | WF4 实际更新 / 来源 | 最低 20 次是否分别达标 |
|---|---|---|---|
| 42 | 20 / source3 | 21 / source4 | 是 / 是 |
| 43 | 23 / source3 | 20 / source3 | 是 / 是 |
| 44 | 20 / source3 | 20 / source3 | 是 / 是 |

正式实际总数为 124，source5/source6 新训练均为 0。主窗口最佳验证三种子均为正，但收益、接受数和敞口差异明显；WF4 seed42 的最佳验证为正，seed43/44 最佳验证为合法现金 0 收益。两个现金账户不能计为正盈利效力，多个 checkpoint/种子/门槛之间的验证选择也不消除乐观偏差。详见[六 cell 预算、三种子验证及原始资源](ml_selection_history.md)与[当前最终实际训练收据](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)。

### 阶段 5：滚动、成本、执行及收益集中度

沿用已有滚动框架，预登记约 4—6 个评价窗口，每个窗口只使用过去成熟训练材料、验证选择和冻结候选。本轮登记并报告四个滚动窗口，其中只有 WF4 登记 RL；RL 训练/验证窗口为主窗口与 WF4。评分模型与 RL 分别记录，不将四个评分滚动窗口改称为四个 RL 验证窗口。

比较原规则、合格候选原规则、简单动量、随机种子、监督模型和 RL；同时报告净收益、回撤、敞口、换手、费用、成交和风险终止。不能因某个窗口最好而只呈现该段，也不随意拼接独立重置账户。

保留 1.5/2 倍成本对照，并明确检验成本假设、成交延迟及预算受限执行。收益集中度使用重建持仓和交易/事件组，比较去掉最大若干盈利组后的表现；固定 5 日组和不同随机种子不自动证明统计独立。

**已经查看的历史 `test` 此后只作为开发期材料。** 不再将同一期间改名为新的独立最终验收。新的最终样本、候选数量、实验预算及多重试验口径需在打开结果前登记。

验收与交付：

- [x] 四个预登记窗口 9/9/9/10 个完整 test arm 共 37 个对照账户、20 项费用/执行压力尝试全部报告，没有选择性删段。WF2/WF3 低参与率真实期末流动性退出失败以 failed_end_window_liquidity 保留，最终净收益、回撤、reward、accounting 和风险终止均 null；报告完成不等于全部压力执行成功。
- [x] 主窗口及 WF4 三种子、四窗口真实持仓组与去除最高收益组结果均报告；37 个 test arm 的连通持仓事件集中度全部 insufficient，independent_events_proven=false，独立性不足不改写为通过。
- [x] 多重试验矩阵、最低 120/最高 180 正式更新预算、最大 1 个冻结候选及 2026-10-06 至 2027-01-01 独立最终区间已在打开前登记；实际收据为 preregistered_future_sample_not_opened，旧历史 test 继续只作开发材料，未来 final 尚未打开。
- [x] 主窗口与四窗口分开裁决收益、风险、会计及证据条件。四窗口均 retrospective_checks_passed=false、formal_admission=false；WF2/WF3 的低参与率账务未知单列，不能写成全压力通过，正式效力与生产启用保持 false。

主窗口仍冻结 source3 main43 的 RL，模型 ID `9db8c77c77e58997798c005b016f34a07bfb85a3fb7d77eacdce8f25bb8b851f`，父模型 ID `1e7f2d876452069b06dc2dc0702dfcc430c35c643d1b44688ccb98c7773cdb90`，门槛 0.51。主窗口 test 为 +2.8052%，只有两个实际开仓、四行成交、固定 5 日块两个 cohort，集中不足。各窗口候选只按本窗口开发验证冻结，没有按 test 改选或替换主候选。

| 窗口 / 实际评估来源 | 冻结候选 / 验证 qualification | test 净收益 | test 最大回撤 | 压力与会计事实 | 证据裁决 |
|---|---|---:|---:|---|---|
| WF1 / source4 | Ridge / false | −3.1590% | 5.9182% | 费用 1.5/2.0 倍均负；test 与全部压力账务通过、无风险终止 | 3 个连通事件，insufficient；回顾检查未过 |
| WF2 / source5 | LightGBM / true | −1.8908% | 6.6851% | 费用两倍数均负；低参与率 95 行部分成交、终端退出未完成，最终账务未知；其余压力实际完成 | 3 个连通事件，insufficient；回顾检查未过 |
| WF3 / source5 | LightGBM / false | −2.8233% | 5.5844% | 费用两倍数均负；低参与率 138 行部分成交、终端退出未完成，最终账务未知；其余压力实际完成 | 4 个连通事件，insufficient；回顾检查未过 |
| WF4 / source5 | LightGBM / true | +6.8967% | 4.4982% | 费用 1.5/2.0 倍均正，低参与率实际收益 −0.2253%；test 与全部压力账务通过、无风险终止 | 2 个连通事件，insufficient；去掉正收益事件后为负，回顾检查未过 |

WF4 Ridge/LambdaRank 的 test 约 +32.39%，高于冻结 LightGBM，但没有按 test 改选。固定 5 日块统计与连通重叠持仓事件不同，块数足够不证明事件独立。source5 最终收据为 completed_historical_matrix_including_real_failed_execution_scenarios；主窗口与 WF1 是 source4 完成评估的直接继承，WF2–WF4 是 source5 新评估，训练没有新增。

完整九/十 arm、每项压力真实净收益/奖励/账务、失败账户原持仓和订单，以及分开的裁决见[稳健性解读](ml_selection_history.md)、[source5 四窗口原始结果](../reports/ml_selection_next_full5_20261005/walk_forward.json)与[当前 source6 最终执行收据](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)。source6 只汇总这些已完成历史评估并修复桥接审计，不改变两个失败压力的未知最终指标。

### 阶段 6：真实前向影子与 RL 状态桥接

先提供新鲜已收盘行情和当时成员资格，使用实际时钟记录冻结模型的输入、选择和执行条件。沿用观察内容身份、信息截点、追加成熟结果及禁止原预测回写的机制。

冻结候选为监督模型时可复用现有诊断评分；冻结候选为 RL 时，需要桥接原策略候选、持仓、现金、挂单、权益高水位、策略健康和风险余量。父评分模型的输出不能替代 RL 的实际决策。

真实观察若在 UTC 日内，代理交易最早从观察后的下一可执行日线开盘开始，使用观察时冻结 ATR，排除已经过去的行情。标签按真实可得时间成熟；代理结果与模拟执行账户结果分开评价，历史重放继续标记 retrospective。

这一阶段仍是离线影子和前向证据建设，不自动启用实盘。记录缺数据、陈旧信息、资格不明、状态不完整和无法成交，避免用计划仓位收益替代执行结果。

验收与交付：

- [ ] 取得真实新行情及资格证据，观察使用冻结候选和实际可得输入。
- [x] 8 币 pilot 的 52 个固定策略原引擎状态快照和重放决定全部一致，无验证错误；此为历史桥接验收，真实前向缺状态时仍拒绝声称策略表现。
- [x] 正式固定 RL 的 source6 原引擎历史桥接 144/144 个动作与完整 audit 一致、0 验证错误；source5 的 8 个完整审计不一致原报告保留，修复前后五份经济文件 SHA 与七个标量精确相同。这个验收不代替真实前向时间或账户表现。
- [ ] 原预测不回写，未来结果成熟后追加，并区分代理与实际模拟成交口径。
- [ ] 完成预登记前向观察及漂移评价，未取得证据时保持待判定。
- [ ] 取得至少 30 个核验通过的真实前向 decision 日期、真实账户/策略状态及成熟标签，完成原引擎模拟成交与权益评价。
- [ ] 在冻结候选与 PIT 等证据满足后完成已登记且未打开的独立最终样本；当前只完成预登记，不提前打开或判定有效。

当前公开输入为 `public_binance_20261004T181036479390`，一次实际采集覆盖静态研究范围的 55/60：32 个候选满足当前资格与流动性条件，23 个流动性不足，5 个缺资格证据；缺数据和资格不足未静默补成合格。该采集不修复完整历史 PIT 或幸存者偏差。source6 实际前向证据仍为 pending_forward_evidence：已记录 1 条 observation，核验通过的真实 decision 日期为 0，成熟 proxy 标签为 0，模拟账户收益 null。2026-10-06 开始的前向窗口尚未开始，冻结 RL 仍缺真实账户和策略状态；最大 1 个候选的独立 final 已在开始前登记但未打开。一次采集和 144 个历史桥接快照不能代替真实前向时间、模拟账户执行或最终效力证据。见[实际前向证据](../reports/ml_selection_next_full6_20261005/forward_evidence.json)与[当前最终身份](../reports/ml_selection_next_full6_20261005/next_execution_receipt.json)。

### 每轮执行前的冻结与资源预算

每次改变目标、特征、标签、奖励、币池或训练预算，创建新 `run`，冻结代码、数据、成员证据、配置、实验矩阵和验收门槛。保存旧 V1 结果与旧账户基线，不覆盖它们，也不修改历史候选身份来追求更好的展示结果。

合并代码不包含本地大规模研究输入和结果归档。新机器需要准备匹配的注册数据或建立自己的新协议；缺少输入时不能跳过身份校验。运行与恢复按现有指南执行，最低实际更新、延迟早停及完整回合恢复预算已完成；本轮未采用短回合，后续采用时仍须另建协议和更新操作说明。

当前 Ridge、LightGBM/LambdaRank 与 NumPy REINFORCE 使用 CPU，3050 Ti Laptop 足以继续当前方案，无需为此配置 GPU。早期 8 币 pilot 的约 334 MB 属于旧范围；静态 60 币 run 的 9 个注册奖励/种子 pilot 实际完成 18 次更新、0 个失败、正式贡献 0，原始报告的进程生命周期工作集峰值为 933,494,784 bytes。六个正式 cell 完成 124 次更新，三个种子、两个训练/验证窗口全部达到最低 20 次，预算已完成；source5/source6 导入恢复新增更新均为 0。

真实训练资源采用恢复前保存的原始 archive，不用 0-update resume 的近零耗时。source3 main44 的正式训练调用为 1,659.7413 秒，WF4 seed43/44 为 1,279.1449 / 1,436.5593 秒；source4 WF4 seed42 的 21 次更新调用为 1,317.4711 秒。对应原始进程生命周期峰值分别为 850,399,232 / 1,123,414,016 / 1,121,742,848 / 1,127,063,552 bytes；不能相加成研究并发峰值。资源完整解释与原收据链接见[预算与资源核对](ml_selection_history.md)。

阶段 1/2 的诊断与监督研究、阶段 3 固定研究范围的 9 条曲线、阶段 4 的真实预算、阶段 5 的完整尝试与裁决、阶段 6 的历史桥接均已按注明范围交付。阶段 3 的完整 PIT/成员扩展和阶段 6 的真实前向时间、账户/策略状态、成熟执行结果与未打开最终样本仍开放。当前正式效力与生产身份继续为 formal_admission=false、production_enabled=false；源码存在、训练完成、历史报告完整、收益改善和正式准入分别保留状态。
