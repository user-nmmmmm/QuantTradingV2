# P2/P3：软状态、动态轴归因与有限资本影子回放

范围：将 Jimmy Hu 的 *Regime-Aware Multiaxial Signal Meta-Layer* 中状态条件化与元层思想，扩展到已有 P0/P1 研究链路。P2 负责冻结状态模型、条件 EV 与动态权重；P3 负责独立的交易政策回放。**默认关闭，不修改正式交易、健康状态、资金分配、风险阈值或策略准入。**

这不是论文全部实验的复刻，也不是把论文中的收益数字移植到本项目。当前没有跨信号 composite 合成器、自动状态数扩容、在线再训练或实盘接入。

## 1. P2 实现及防前视

每个账本由策略、信号版本、方向、时间周期、成本/观察快照和持有期限共同确定；不同方向和期限不混账。三个轴分别使用已知特征 `return_12`、`efficiency_ratio_10`、`volatility_ratio_8_48`。

每折使用预先声明的训练窗口（默认 365 天）、隔离段（20 天）和测试段（90 天）。训练窗口再次按时间切分：

1. 较早 120 天拟合状态原型，只纳入入场和标签均在拟合截止前的样本。
2. 对每个轴，用该特征的历史近邻构造净收益经验分位数。训练分布排除样本自身，推断分布只使用冻结训练集与当前已知特征。
3. 在分位数网格上近似一维 Wasserstein-2 距离，执行确定种子、多次初始化的 K-means；分位数均值是该离散表示的重心。软归属由负平方距离的 softmax 给出，温度来自训练期簇内距离。样本不足、常数特征或模型不可用时显式返回未知，不伪造高置信度状态。
4. 较晚训练窗口逐个信号重放：先入账严格早于当前入场时刻成熟的标签，再冻结当前入场的软归属及各轴预测；当前信号自己的结果不可用于预测自己。结果成熟时使用入场时保存的归属，不按新状态回写。
5. 动态归因仅使用截止前成熟的这些历史冻结预测，且所有轴使用同一支持充分的样本集。正 Spearman 相关的平方作为权重原料，经上下限、EMA 与有界单纯形投影，最终权重和为 1 且每轴保持在边界内。缺乏足够样本、有效时间块或正相关证据时等权回退；时间块不保证统计独立。
6. 外层测试期间模型、EV 账本与轴权重冻结，不用测试期标签更新。每次重新拟合状态模型都重建 EV 账本，不把不同质心的同名状态单元混在一起。

P1 的衰减、收缩先验、有效样本、时间块和保守下界机制被复用；每个有正概率的状态都需要满足支持门槛。动态加权不能绕过弃权条件。

### 与论文的明确差异

- 分布构造采用**历史特征近邻**，确保信号当时可推断；不是用当前信号的未来收益构造分布。W2 在有限分位数网格上近似，非任意精度直方图运输求解。
- 软归属不是已校准的真实状态概率。簇间距离、熵只描述模型，不证明预测有效。
- 负相关不取平方奖励；额外的有界投影保证平滑后的最终权重仍满足边界。
- 所有轴都按方向隔离；不复制论文某些轴的对称合并。
- 不使用论文的 1.5 倍放大仓位；P3 上限不超过固定名义基线。未实现跨策略信号合成或 native score 到 bp 的通用校准器。

## 2. 默认政策与接口

`core/signal_adaptive_types.py` 声明独立、冻结且可序列化的政策；不修改 `config/params.yaml`，以保留旧 P0/P1 配置摘要和精确复现。

| P2 参数 | 默认值 |
| --- | --- |
| 每账本每轴状态数 | 2 |
| 拟合子窗口 / 最少样本 | 120 天 / 40 |
| 近邻 / 分位数网格 | 20 / 21 |
| 重启 / 最大迭代 / 种子 | 3 / 50 / 20260918 |
| 归因窗口 / 最少样本 / 最少有效时间块 | 90 天 / 50 / 8 |
| 轴权重下限 / 上限 / EMA 更新率 | 0.15 / 0.60 / 0.25 |
| 其余 EV 支持与冻结窗口 | 继承 `EVPolicy` 默认值，不因历史结果放宽 |

```powershell
# P2；自动启用 P1 与 P0
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --adaptive-signal-meta --report-profile full --seed 20260918

# P2 + P3；自动启用全部研究依赖
.\.venv\Scripts\python.exe main.py --source local --data-dir data/binance/1d --symbols BTC/USDT ETH/USDT BNB/USDT --start 2019-01-01 --end 2020-12-31 --signal-meta-replay --report-profile full --seed 20260918
```

Python 接口为 `BacktestEngine(signal_adaptive={"enabled": True}, signal_meta_replay={"enabled": True})`，也可以传入完整政策对象。返回键为 `signal_adaptive` 和 `signal_meta_replay`；关闭时为 `None`。默认关闭以及随机滑点开关两种情况均有隔离回归。

P3 开启会依次打开 P2/P1/P0，但只改变这些依赖的 enabled 值，不暗改其参数。P3 持有期限必须已登记在 P0 horizons 中，不自动新增训练标签。

## 3. P3 的三个独立政策账户

各账户独立从 10,000 初始资本开始；同一政策内候选竞争真实资金和标的占用。固定名义 1,000，交易前持仓加待成交预留预算不超过标记权益。它不是正式账户，也不是为每笔交易重新充值的标签组合。

| 政策 | allow | veto | abstain |
| --- | --- | --- | --- |
| baseline | 固定名义 | 固定名义 | 固定名义 |
| gate | 固定名义 | 不开仓 | 不开仓 |
| sizing | 下界 / 100 bp，裁剪至 0.25～1 倍 | 不开仓 | 预先声明的 0.25 倍 |

弃权减仓不是正 EV 证据。基线仅排除 P0 预热、数据、universe 不合格候选，忽略正式路由等后续门禁；三政策都不使用事后标签或事后执行标志挑选订单。

- 同刻按 native score 降序、策略、标的和候选 ID 确定顺序，非因容器遍历顺序抢占资金。
- 收盘时决定订单，下一真实 bar 撮合；默认自首次成交计 5 根真实 bar，期满撤销未完成开仓，下根真实 bar 平仓。部分成交、订单 TTL、部分退出都保留真实账户路径。
- 手续费、滑点、融资成本使用隔离的研究 Broker；不共享正式 Broker 或随机数状态。研究账户采用配置中的确定性滑点率。
- 融资缺失、执行异常、保证金违约或候选数据缺失不能当作正常零收益；保留成交事实，停止受影响路径并使账户绩效未知。
- 尾部未完成候选保持删失，不强行平仓；报告分别列出已平候选损益、期末标记权益和未平持仓。缺行情标的使用最后已知标记并明确标为过时价格。

P3 只是预先声明的政策对比，不能将差值叫作因果收益提升；尤其 gate 不交易时的零收益不是模型成功证明。

## 4. 产物、复现与验收

P2 产物包括 `p2_predictions.csv`、`p2_evaluations.csv`、`p2_folds.csv`、`p2_cells.csv`、`p2_regime_models.json`、`p2_attribution.csv`、`p2_calibration_predictions.csv`、`p2_diagnostics.csv`、`p2_summary.json`、`p2_report.md`。

P3 产物包括 `p3_candidates.csv`、`p3_accounts.csv`、`p3_equity.csv`、`p3_fills.csv`、`p3_financing.csv`、`p3_execution_audit.csv`、`p3_summary.json`、`p3_report.md`。

三个报告 profile 都导出研究表；full 还写入数据快照及 manifest。manifest 独立登记 P0/P1/P2/P3 政策、摘要和产物。旧 manifest 明确关闭新功能；新研究缺少摘要/依赖时拒绝复现，摘要不符时返回失败。

P2 报告在共同有限预测、成熟可执行标签上比较 prior、P2 等权、P2 动态及可选 P1 参考的 MSE。每行单独报告共同样本数，不能跨不同分母排名；预测误差也不是交易收益。报告同时给出未知状态、熵、训练分离度、样本不足和等权回退，避免只看总平均。

```powershell
.\.venv\Scripts\python.exe scripts/run_portable_tests.py -q tests/test_signal_regime_model.py tests/test_signal_axis_attribution.py tests/test_signal_adaptive.py tests/test_signal_meta_replay.py tests/test_signal_adaptive_reporting.py tests/test_signal_adaptive_cli.py
.\.venv\Scripts\python.exe scripts/validate_signal_adaptive.py
# 用新验收目录中的 run_manifest.json 替换路径
.\.venv\Scripts\python.exe main.py --replay-manifest reports/<run>/run_manifest.json
```

验收脚本使用已有 2019–2020 三币日线做开关对照，目录必须新建、不得覆盖旧证据；检查正式摘要、健康、分配与 P0/P1 完全相同，并保存新研究报告和快照。不使用结果选择参数，也不把已有历史叫作新的未见样本。旧 P1 验收目录仍须可精确重放。

自动测试覆盖 W2/重心/软归属、未来结果扰动、追加数据、同刻标签、冻结入场状态、模型重拟合隔离、归因稀疏回退、权重边界、真实资金竞争、两端 next-bar、部分成交/退出、融资错误、尾部删失及新旧 manifest。

### 2026-09-18 本地验收结果

最终证据目录：`reports/p23_signal_meta_20260918_verified/`；先读 `acceptance.json`、`p2_report.md`、`p3_report.md`。工程检查全部通过：

- 全项目测试：1,079 passed、1 skipped、46 subtests passed；覆盖率 87.99%，高于 55% 门槛。运行中有 2 条 SQLite 未释放资源警告，无测试失败。
- 未知轴熵最终统一为 `null`，不是 0；修订后 P2、P3、CLI 和报告共 93 项回归通过。全量与补充 JUnit 分别保存为 `tests_junit.xml`、`entropy_followup_junit.xml`。
- Ruff、关键运行时类型检查、依赖环境和锁文件校验通过。
- 相同种子、相同三币日线开关对照：正式账户 74 笔成交不变；成交、权益、基准、报告摘要、策略健康、资金分配、P0/P1 研究摘要全部一致。
- 旧 P1 manifest 保持精确复现；最终新 manifest 的正式结果与 P0/P1/P2/P3 摘要全部一致，退出码 0，记录在 `replay_verification.log`。
- manifest 登记的 47 份产物逐一核验 SHA-256 与大小，无不符。测试日志为额外附件，不重写已登记证据。

研究覆盖情况：467 个候选 × 4 个期限 = 1,868 条预测，4 个冻结测试折。1,100 条处于初始训练期；测试期的 768 条均因未知状态而弃权。66 个账本/折拟合记录中，没有一个三轴全部可用；趋势轴和效率轴各 6 个可用，波动轴 0 个可用。部分早期窗口中趋势/效率有 43～46 个样本，但波动轴仅 36～38 个，未达到预先声明的 40 个有效特征样本门槛；不能为消除弃权而事后降到 36。80 个归因账本全部等权回退。

| 影子政策 | 期末标记收益 | 最大回撤 | 成交笔数 | 期末未平持仓数 |
| --- | ---: | ---: | ---: | ---: |
| baseline | 25.10% | 8.15% | 254 | 2 |
| gate | 0.00% | 0.00% | 0 | 0 |
| sizing | 6.28% | 2.15% | 254 | 2 |

这些是既有历史上的政策路径，不是实盘业绩。gate 没有交易，因此不能宣称择时成功；sizing 在本片段中始终执行预设的四分之一弃权规模，更低回撤主要反映更低仓位，并未验证动态归因或门控带来增益。两项有交易政策的期末收益含未平仓标记损益，不能等同于已实现收益。

最终 P2 摘要：`7ac39d3816ae66fa8ef5162d8160bf7acd41ea9b17c12b098e930173b2d73474`。
最终 P3 摘要：`165ce7074ef7aa844915dc94fe10137f30c0578ec34aa09619b24a8ac648b8bb`。

首轮包 `reports/p23_signal_meta_20260918_final/` 保留未覆盖，但其中逐笔未知状态的熵仍为 0；随后仅修正该诊断字段、重新完整运行并生成上述 verified 包，没有调整模型参数或交易政策。当前源码应重放 verified 包，不以旧包的代码身份比较失败作为新结果失败。

```powershell
.\.venv\Scripts\python.exe main.py --replay-manifest reports/p23_signal_meta_20260918_verified/run_manifest.json
```

结论：P2/P3 工程能力和证据链完成；本片段仍不提供满足门槛的正净优势、动态权重有效性或正式策略准入证据。下一阶段应预先冻结数据窗口和检验方案，再积累足够样本，而不是根据本表调参。
