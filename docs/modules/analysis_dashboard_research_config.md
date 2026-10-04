# analysis/ · dashboard/ · research/ · config/ 模块说明

本文按职责说明研究分析、工作台、事件重放、配置加载和根目录入口。批量任务按[脚本索引](../../scripts/README.md)查找；各批次的研究结论、样本数量和验收状态以原始研究文档及产物为准。

## analysis/ — 离线研究与证据

`analysis/` 包含候选选择、统计诊断、论文方法对照、标签与账户对账、执行证据审计等模块。多数模块作为库被研究脚本或工作台调用；`python -m analysis.optimize` 是独立参数搜索入口。研究状态 `ok`、工程检查通过与交易准入是不同概念。

### 参数搜索与滚动验证

[`optimize.py`](../../analysis/optimize.py) 复用同一份行情，遍历 `ENTRY_WINDOWS=(20,30,50,100)` × `EXIT_WINDOWS=(5,10,15,20)`。每个组合创建新的趋势多空策略，同时配置均值回归和波动率策略，通过 `BacktestEngine` 计算账户结果。两种模式的边界如下：

| 模式 | 选择依据 | 输出与解释 |
| --- | --- | --- |
| 默认网格 | 按每个候选收益序列前 70% 的平均收益排序；全样本收益、Sharpe 等列仅作诊断 | `reports/optimization_<timestamp>.csv`，以及同名前缀的 `_evidence/` 登记、尝试日志和候选族统计。 |
| `--oos` | 沿用训练段选参，后 30% 只作最终评价；各候选训练收益的 bootstrap p 值参与 BH 校正 | 额外保存 `_oos.json`。这是既有历史上的分段研究，不证明独立留出集未被查看。 |
| `--walk-forward` | 每个窗口只按测试段之前的 validation 分数选择 | `reports/walk_forward_<timestamp>.json` 和 `_evidence/`。每窗重新运行引擎，输出选择过程及各候选的测试表现。 |

`--jobs N` 仅控制普通网格并发。worker 为可导入的模块级函数，兼容 Windows 创建独立进程；结果按登记网格顺序重排，再按训练得分稳定排序。失败候选保留在尝试日志和候选集合中，不通过删列制造完整研究族。

CLI 支持 `--symbols`、`--days`、`--start`、`--end`、`--source {synthetic,yahoo,ccxt}`、`--capital`、`--oos`、`--jobs`、`--walk-forward`、`--wf-train`、`--wf-validation`、`--wf-test`、`--wf-purge`。准确参数以 `python -m analysis.optimize --help` 为准；根回测 CLI 另支持本地行情，不应混用两者的数据源选项。

[`walk_forward.py`](../../analysis/walk_forward.py) 的 `run_walk_forward(data_map, candidates, config)` 对每个可用窗口、每个候选分别执行选择段和测试段。选择段收益在 validation 起点拆开，train 仅作对照，validation 决定获选候选；`selection_agrees` 记录两个分段是否选出同一候选。

调用方必须传入零参策略工厂，确保每次运行的健康与冷却状态独立。预热长度综合配置下限、工厂声明、策略窗口和止损所需历史逐候选确定；前置历史不足的窗口记入 `skipped_windows`，不会缩短预热。`step` 不能小于测试段长度，避免重复计入测试收益。

返回结果区分两个总体：`procedure` 汇总各窗口获选者的测试收益，`candidates` 汇总每个候选的测试收益并提供多重检验诊断。每个窗口从同一初始资本开始，窗口尾部按配置的期末政策处理；默认 `mark_to_market` 为无额外退出成本的估值转移，不能声称每窗都发生一次成本化强平。汇总是独立窗口实验，不代表一个账户跨窗口持仓和复利。候选失败、时间轴不完整或运行身份变化时，统计证据会降级，不能保留候选“通过”结论。

### 统一研究证据与统计工具

[`research_evidence.py`](../../analysis/research_evidence.py) 为网格和滚动研究提供共同的登记、日志与统计输出：

- `ResearchEvidenceRun` 先保存候选、参数、数据与源码哈希，再追加各次执行状态。指定输出目录时写入 `registration.json` 和 `attempts.jsonl`，目录必须是新的；未指定时仅保存在内存。
- `verify_identity` 复核数据以及默认采集的本地源码身份。调用方传入外部 `source_hashes` 时，本模块不自行验证这些源码，返回 `source_unchanged=None`，不能当作核验通过。
- `candidate_panel_evidence` 要求候选收益采用完全相同的日期轴，不取交集、不补零、不删除失败候选。默认基准是零收益现金；PBO 使用长度可整除 `pbo_groups` 的前缀并披露尾部排除日期，DSR 和 Reality Check 保留完整时间轴。
- 本次结果前登记不补齐历史全局搜索记录。局部统计保留 `diagnostic` 和 `admission_eligible=False`；源码哈希匹配也不证明数据在历史决策时已经可得。

[`paper_validation.py`](../../analysis/paper_validation.py) 实现 CSCV/PBO、CPCV 事件拆分及预测路径组装、联合块重采样的 White Reality Check、DSR 与依赖敏感性。输入、状态、并列处理及公式见[论文统计验证接口契约](../research/paper_validation_contract_20261003.md)。CPCV 返回事件预测路径，需另行固定交易和成本规则后才能回放成收益；各路径共享历史，不能按路径数扩充独立样本量。

[`research_validation.py`](../../analysis/research_validation.py) 提供窗口、purge/embargo、搜索试验登记、留出集协议和准入证据评价；[`validation.py`](../../analysis/validation.py) 提供训练段选择、最终段评价和邻域稳定性包装。它们本身不负责真实订单提交。

### 论文方法、标签与连续账户

| 模块 | 主要输入与输出 | 解释边界 |
| --- | --- | --- |
| [`paper_data_audit.py`](../../analysis/paper_data_audit.py) | 审计显式 manifest、OHLCV 及标的生命周期，输出数据问题与身份 | 只读审计，不自动补齐缺口。 |
| [`paper_study.py`](../../analysis/paper_study.py) | 校验并冻结论文实验输入、参数和任务登记，提供固定对照及覆盖诊断 | 历史资料仍是回顾性研究。 |
| [`paper_portfolio.py`](../../analysis/paper_portfolio.py) | 按 `PaperSpec` 生成目标，通过既有 Broker 重放资金、成交和成本 | 现金出资、只做多现货；OHLCV 流动性仍是 bar 级模型。 |
| [`paper_labels.py`](../../analysis/paper_labels.py) | 将 P0 候选转换为三重障碍标签及成本情景 | 同一 bar 同穿止盈止损时保守按止损优先定价，标记歧义并排除训练。 |
| [`paper_label_execution.py`](../../analysis/paper_label_execution.py) | 用细周期事实细化标签；将标签与观测现金流对照 | 新证据保留真实可用时间，不能回填为历史已知。 |
| [`paper_label_signals.py`](../../analysis/paper_label_signals.py) | 生成固定期限成本标签与按时间推进的常量 EV 目标 | 只使用决策前已可用的标签，是研究基线，不是生产 P1/P2 模型。 |
| [`label_account_reconciliation.py`](../../analysis/label_account_reconciliation.py) | 候选→开仓订单→权威 FIFO 平仓分摊→权益桥接 | 缺标签仍可保留现金流归属；成本未完整或库存未平时，不生成完整候选净收益比较。 |
| [`return_followup.py`](../../analysis/return_followup.py) | 生成多周期目标、年度选择日志与配对收益差诊断 | 调用方以连续 Broker 账户执行目标并承担切换成本；不拼接不同账户收益。 |
| [`paper_risk.py`](../../analysis/paper_risk.py) | 经验尾部风险、联合压力情景与库存清算路径 | 压力情景及模型成本不等于已观测真实成交。 |
| [`factor_registry.py`](../../analysis/factor_registry.py)、[`historical_factor_inputs.py`](../../analysis/historical_factor_inputs.py) | 冻结因子定义、来源和历史输入，构造有明确口径的代理变量 | 代理变量不能冒称完整论文因子，事后收集不认证历史时点可得性。 |
| [`overlay_study.py`](../../analysis/overlay_study.py) | 比较固定覆盖层与参考交易的归因诊断 | 参考交易是代理对照，差异不能直接声称为实际节省。 |

账户对账依赖 Broker 的 `lot_closes`，不能把退出归给最新信号。融资与成交同刻时需要明确先后，否则保留为未分摊费用；账户权益桥接同时需要期初资本、期末权益和未平库存估值。`valuation_only` 的期末估值转移保留为库存，而非实现成交。

年度选择首年固定候选，后续只评价上一日历年中、收益可用时间早于预先规定截止点的样本；不足或非正分数时保持现金。目标在收盘生成，由调用方在后续开盘按原调仓节奏执行。配对超额区间使用日收益差的年化算术均值，不是累计收益区间或未来盈利概率。

### 执行证据与既有专项

[`execution_calibration.py`](../../analysis/execution_calibration.py) 离线关联规范事件、请求回执、独立报价及成交来源，并审计日期覆盖与成交分层。它区分决策、提交、确认、成交和报价时钟；公开报价不能代替订单或成交证据，ACK 延迟也不能当成撮合延迟。使用入口见[脚本索引中的执行证据](../../scripts/README.md#公开资料执行证据与后续观察)。

[`market_speed.py`](../../analysis/market_speed.py) 统计已收盘 bar 的价格变化速度，与网络请求耗时分别报告。深度采集由 `core/binance_depth_observer.py` 提供日志与同步校验；可见盘口容量仅为诊断，不代表下单到达时仍可成交。

既有策略审查、趋势组合和 S2 诊断分别位于 `strategy_review*.py`、`trend_portfolio*_validation.py`、`selection_research.py`。复现这些专项应先阅读其原始协议和冻结输入，不用新批次结论改写历史验收。

## dashboard/ — 运维展示与本地研究工作台

目前有两个入口，完整参数、持久化、任务约束和结果口径见 [Dashboard 使用说明](../../dashboard/README.md)。

- `python -m dashboard` 是只读 CLI，消费实盘状态快照及告警，状态缺失或无效时返回失败状态。
- `python -m dashboard.web` 提供本地网页工作台，除读取监控与报告外，还能提交离线回测和滚动研究任务、保存实验档案与预设。因此不能把整个 `dashboard/` 包描述为只读组件。

工作台的研究结果不改变实盘准入；监控快照与历史研究报告也应按各自来源解释。

## research/ — 已记录执行事件重放

[`research/replay.py`](../../research/replay.py) 的 `replay_execution_events(adapter, events, *, apply_fills=True)` 委托 `RecordedExecutionAdapter.replay(...)` 重放已记录的 `EventEnvelope`。它不重新提交交易所订单；`apply_fills=True` 会把成交事实应用到适配器持有的本地组合，因此调用方应明确使用哪个组合实例。该模块供导入使用，没有独立 CLI。

## config/ — 配置加载

[`config/config.py`](../../config/config.py) 加载并验证 [`params.yaml`](../../config/params.yaml)。`ConfigLoader()` 复用默认路径的单例，显式传入路径则创建隔离实例。构造时立即校验：文件缺失、YAML 无法解析、顶层不是映射、缺必需键、非有限数值或字段约束错误都会抛出 `ConfigLoadError`。

旧 `phase4` 配置可映射到当前所属节，但新旧值冲突时拒绝加载，不能静默择一。`get(section, key=None)` 缺键返回 `None`，`require(...)` 缺键抛错。模块导入时创建默认 `config`。

入口和装配层向运行时传递配置及依赖；回测引擎仍消费默认配置。研究任务使用独立参数或配置快照时，应保留其身份与输出目录，不能把实验参数等同于基础配置已被更新。

## 根目录入口

| 入口 | 作用与产物 |
| --- | --- |
| [`main.py`](../../main.py) | 回测 CLI，无参显示帮助并以退出码 2 结束。协调数据获取、质量检查、`BacktestEngine` 和报告生成；支持合成、Yahoo、CCXT 和本地行情。`--output-dir` 指定报告目录，`--report-profile {workbook,compact,full}` 默认 `workbook`；完整账本、事件与重放证据由 `full` 路径输出。 |
| [`run_live.py`](../../run_live.py) | 沙盒/实盘 CLI，默认沙盒。装配启动安全策略、持久订单保护、Broker 和实盘引擎；`--preflight-only` 生成启动检查报告而不进入主循环。`--live` 另需固定身份的 R8 证据、回滚快照及显式风险上限等检查。 |

最小离线回测示例：`python main.py --source synthetic --days 365`。完整选项分别用 `python main.py --help` 和 `python run_live.py --help` 查看。命令行支持某种市场或账户模式，不表示该模式已获真实资金准入。
