# 论文研究深化：详细开发与测试计划（2026-10-25）

> 对应[专项 Roadmap](paper_research_roadmap_20261025.md)。日期采用 Asia/Singapore。
> 本文件规定拟实施工作包及完整流程。下文“拟新增”的文件、API和产物契约尚未实现；命令章节明确区分现有入口和待开发入口。
> 本次交付是计划文档及文档检查，历史测试回执不作为本次执行结果。

## 1. 开发范围与交付顺序

近期主线复用既有统计、回测、数据版本、Broker、风险、ML和研究证据框架。首轮不增加RL训练、不调正式策略门槛、不重建执行器。多期配置为条件支线。

所有改动先在独立研究范围实施；旧默认、旧协议和旧报告必须可读。涉及正式任务范围时，按现有[开发流程](../development_plan.md)、[优先级与完成契约](../roadmap_completion_contract.md)同步权威计划、详情、登记和验收清单。PR25编号是专项工作包，不代表全项目新增已关闭任务。

推荐实施队列：

1. PR25-01/02固定环境、当前工作树、完整研究合同；PR25-00同时核验文献。
2. 并行推进 PR25-03/04/05、06/07/22、08/09/10、11/12。共享文件的变更按接口约定串行集成。
3. 满足各自前置后推进13/14/16；17可用固定合成账户提前开发，但真实前向证据仍等待。
4. 18汇总有限编排与报告，19整体验收，20冻结新候选，21仅在相应证据真正到期后裁决。
5. PR25-15由14的瓶颈证据触发，单独估算/登记7–10人日，未启动时不阻塞近期主线。

关键共享文件：analysis/research_evidence.py由身份/统计任务统一协调；core/selection_v2.py先增加估计器接口再接成本provider；ML dataset/selector/forward_bridge由一个接口负责人集成。每个变更集只承担一个可单独验收的行为。

## 2. 单工作包的完整开发流程

| 步骤 | 必做动作 | 可复核输出 |
| --- | --- | --- |
| 1 现状确认 | 阅读任务卡、来源及受影响调用链；确认能力是否已实现；记录依赖和输入缺口 | current_state、affected_paths、已有测试、工作树/环境身份 |
| 2 固定合同 | 写清输入/输出、时间、单位、错误状态、兼容、主指标及预算 | contract_version、accepted_scope、hypotheses、候选和基准 |
| 3 最小反例 | 对新算法准备独立数学fixture；对行为缺陷固定可复现失败；文档只检查结构/引用 | 修复前事实、期望事实、固定种子/输入及比较依据 |
| 4 最小实现 | 复用现有模块，保持默认路径；新增字段版本化；必要的持久化与恢复同时交付 | 代码、迁移说明、失败/不足行为、可运行接口 |
| 5 专项验证 | 先跑新增与受影响测试，检查不变量及故障反例 | JUnit、日志、手算/参考对照、代码/数据摘要 |
| 6 有限研究 | 仅在登记后运行预定候选，保持训练/验证/最终分区；失败尝试保留 | 全部attempts、账户轨迹、统计与误差报告 |
| 7 集成验证 | 检查跨模块时序、账户桥、旧模式和恢复；运行适当静态/CI检查 | 差异、兼容、故障、集成及运行范围说明 |
| 8 复核与交接 | 独立复核方核对依据；分别给出工程、研究、外部证据状态 | 验收索引、未建模项、下一责任人和输入 |

代码修复须有能识别该缺陷的反例；新增估计器须有独立数学预期。不要用逐行复述实现的测试代替正确性依据。低影响文档变更不运行交易回归。

任一步失败：保留原run和日志，登记原因/影响范围，修复后使用新运行ID重跑受影响检查。输入或模型身份变化后，不沿用旧结果标为当前通过。

## 3. 共同数据、协议与状态契约

### 3.1 时间与数据模式

| 字段/模式 | 定义与要求 |
| --- | --- |
| event_at | 市场事件或经济事实发生时间 |
| observed_at | 本地实际首次观察时间；下载历史数据的今天不能改为当年 |
| available_at | 在选定knowledge规则下可用于该决策的时间 |
| source_publication | 可核对的供应商/公告发布时间证据，缺失保持未知 |
| decision_cutoff | 本次决策实际信息截止点，来自运行事件/既有策略cutoff；不在strict模式固定写成bar开盘+1日 |
| label_mature_at | 标签依赖事实、评价期限、费用和计算观察等条件全部满足的最早时间 |
| retrospective | 保留当前回顾性能力；名义收盘等假设显式记录，不称已证明历史可得 |
| strict/local | 按实际本地观察及可得时间查询；未知证据按既有exclude/raise政策 |
| strict/published | 按可证明发布/修订时间查询；与local语义分别测试 |
| prospective | 登记后实际接收的观察，需候选冻结与连续原始收据；不能由补下载历史数据获得 |

实现复用[TemporalDataPolicy与cutoff](../../core/temporal_data.py)、[DataVersionStore](../../core/data_versions.py)及[标签版本](../../core/signal_label_versions.py)。不同模式必须有不同身份和清晰报告。现有ML研究runner仍以retrospective协议为边界；完成适配不等于runner自动具有最终独立评价资格。

### 3.2 研究协议的最小字段（拟新增的统一封装）

~~~yaml
schema: paper-research-protocol/v1
study_id: unique-registered-id
registered_at: actual-timestamp
paper_refs: [L01, L03]
source_manifest_sha256: required
config_sha256: required
data_manifest_sha256: required
evaluation_kind: retrospective
information_policy:
  mode: retrospective
  knowledge: local
  unknown: exclude
candidate_ids: [fixed-before-running]
benchmark_ids: [fixed-before-running]
search_scope: current_local_family
historical_global_search_complete: false
primary_metric: declared-before-results
economic_or_precision_target: declared-with-rationale
training_selection_rule: declared
evaluation_windows: declared
cost_policy_id: required
risk_policy_id: required
resampling:
  method: stationary
  block_lengths: [5, 20, 60]
  iterations: 2000
  seed: 42
attempt_budget: fixed-before-running
resource_budget: calibrated-in-small-pilot
stop_rules: declared
final_access_policy_id: separate-if-applicable
~~~

上述块长/次数是承接现有研究的候选默认设计，需在训练/受控校准后冻结，不保证平稳性或统计功效。不可按测试最优p值选择块长。新协议是拟实施封装，不能直接传给尚不支持该schema的现有CLI。

独立收益主张需要登记统计方法、区间覆盖及多重比较政策；若拟采用95%区间，须在结果前固定，并使用与依赖/搜索范围相容的方法。已有正式准入门槛继续有效。样本量目标根据经济意义、误差精度与时间依赖设计，不能仅填一个方便达到的交易数量。

### 3.3 状态分别记录

建议每个验收回执分别保存：

- engineering：pass/fail/pending，说明实际执行的检查；
- research：supported/not_supported/insufficient/not_evaluated，附主指标、区间和范围；
- external_evidence：complete/pending/invalid，逐项记录来源和缺口；
- admission：沿用既有准入系统的真实结果，研究模块不生成新放行；
- process：exit_code、超时/中断/恢复身份、产物完整性。

这是拟新增报告封装，不替换现有MetricResult或论文统计status枚举。必须建立显式适配，保留原始status、reason和null；不得把进程0、工具status=ok和候选通过混为一谈。

## 4. 逐项开发卡

### PR25-00 文献身份与适用范围

- 输入：已核对原始来源、新Excel可读副本、项目既有论文应用记录。
- 工作：保存题名、作者、版本/DOI/URL、表内编号、实际阅读层级、公式/章节、假设、目标与项目迁移差异。
- 交付：拟新增文献映射表；旧106篇编号和新Excel编号保持各自来源，缺失字段明确待证据。
- 验收：八篇来源可定位；未取得全文的不称完整复现；方法移植所需的关键公式未取得前，不宣称算法等价。
- 依赖缺口：Excel未可读时，仅此映射保持待证据。其余已确定来源的开发可继续。

### PR25-01 现状、环境与固定基线

- 复用：环境/锁检查、历史基线和既有研究产物。
- 工作：记录HEAD、受控差异、未跟踪的实际运行源文件、Python/NumPy/BLAS/求解依赖、平台、配置及数据身份。优先重放一个小型固定账户fixture，不先重跑110次历史矩阵。
- 交付：current_state.json、environment.json、baseline_diff.json（均为拟交付产物）。
- 验收：实际使用的代码和数据均可定位；当前失败与缺外部输入分开；开发环境与CI Python版本差异明确。
- 回归：已有固定账户/组合fixture；只对更改或疑点复验，不改旧历史回执。

### PR25-02 完整登记与源码身份

- 复用：[ResearchEvidenceRun](../../analysis/research_evidence.py)与[ExperimentRegistry](../../analysis/research_validation.py)。
- 已知缝隙：当前source_identity默认目录不覆盖research/ml_selection及全部ML依赖和测试定义；外传source_hashes只是记录，当前verify_identity不会替调用者核验来源。
- 工作：定义每个研究的依赖闭包，纳入实际研究代码、模型、配置、锁、协议、数据及必要测试/编排；本地核验实际字节。planned/started/completed/failed/interrupted/abstained均保留。
- 拟新增：analysis/search_scope.py；tests/test_search_scope.py。避免建立独立于现有记录器的第二套尝试日志。
- 验收：缺arm、日志截断、模型/配置/依赖改字节、未跟踪源文件改动、失败恢复均可识别。局部完整与历史全局完整分别输出；不补造旧搜索记录。

### PR25-03 时序数据与ML入口对齐

- 复用：TemporalOHLCV.as_of、strategy_decision_cutoff、dataset/selector/forward_bridge。
- 工作：增加拟议SelectionTemporalContext和桥接schema v2，贯通实际cutoff、来源版本与可见特征；v1保留回顾性读取。strict输入不再依赖缺省名义收盘时间证明可得。
- 前后行为：晚到bar在旧的名义收盘假设下可进入回顾性材料；strict模式只能在真实可得截止后进入。
- 测试：晚到/乱序/修订、cutoff前后1ns、local/published差异；更改未来输入不改变旧决策；关闭新能力后旧材料/选择结果兼容。
- 交付：时间契约、版本化adapter、跨层一致性报告；不重建已有版本库。

### PR25-04 成熟标签与细粒度歧义

- 复用：VersionedOutcomeTracker、OutcomeRevisionBook、chronological_split和现有首触解析。
- 工作：拟议materialize_training_labels仅物化截止前可用的标签版本；保存revision、成本与资格原因。先选择最新可见修订，再判断资格，撤销/失效后不得回落旧标签。
- 细bar：核对来源、完整度、父子OHLC及可得时间。不能解析的同bar双障碍继续单列/排除，不生成有利盘中路径。
- 测试：晚到中间bar、费用修订、标签撤销、同日早止损与未成熟标签整组隔离、截止前后的版本选择。
- 验收：标签时点和真实账户收益分开；finish不得强制造成熟；旧proxy材料保留独立研究身份。

### PR25-05 成员区间与历史因子输入

- 复用：membership审计、historical_factor_inputs、factor_registry、DataVersionStore。
- 已知缝隙：成员审计可处理多段区间，ML dataset目前每symbol最多一段；pipeline先构建材料再审计。
- 工作：共享拟议membership_as_of解析器，覆盖重新上市、空档、退市和公告可得时间；审计与构建消费同一资格结果。
- 因子：登记市值/供应量定义、历史成分和修订；缺数据保留未知；不将成交额替换市值。
- 测试：重上市两区间、重叠/矛盾区间拒绝、晚到公告、局部降级不得标full verified；未来价格/市值/修订不改变过去因子。
- 验收：局部代理可开展研究，完整全市场复现须有相应真实输入；采购/补采时间单列。

### PR25-06 搜索族与统计证据接线

- 复用：cscv_pbo、white_reality_check、deflated_sharpe_evidence、candidate_panel_evidence。
- 工作：根据事前候选与attempt日志核对族；同时间、资金、费用和持仓口径建立收益面板。失败arm不可删列、补零或默默缩小样本。
- DSR继续保留历史完整/事前登记等原条件；White与PBO的局部完整声明不能扩大到全项目。
- 测试：缺候选、重复ID、异步索引、零方差、失败/中断、截尾声明、同日多资产及重复seed不虚增独立时间。
- 验收：统计值连同完整性、样本、限制输出；admission_eligible等原有边界继续有效。

### PR25-07 数学验证、依赖与零假设校准

- 复用：[统计契约](paper_validation_contract_20261003.md)及现有手算fixture。
- 数学检查：小矩阵完整CSCV组合、并列规则、无优势White统计量、DSR每期/年化隔离、CPCV路径唯一性和每折独立拟合。
- 拟新增离线校准：固定协议的相关噪声/已知信号面板，记录候选数量、AR依赖、块长、假阳性和检出能力。Monte Carlo实验使用固定预算并给出二项/模拟误差区间。
- pytest只验证确定的数学和行为；统计频率校准作为离线报告，不以某个随机运行“必须显著”建立易抖动单测。
- 验收：CPCV事件预测路径与账户路径分开，不能用全样本拟合收益冒充折外预测；路径共享历史不能增加独立样本数。

### PR25-08 真实执行证据资格

- 复用：read_execution_ledger、read_fill_provenance、quote join与readiness报告。
- 工作：统一order/parent/child/fill、独立事前报价、请求、时钟、费用、可得时间和市场身份验证；价格证据与完整成本证据分别评级。
- 兼容重点：旧的预连接报价入口可能依赖调用者source声明；增加拟议evidence_verification_level，严格报告不能只继承旧real_venue_calibration布尔值。
- 测试：模拟/沙盒不能升格，合成累计成交排除，跨日费用成熟、同ID冲突、重复重放、时钟错误、盘口缺序及来源未知。
- 验收：真实库只读；缺费用可保留价格诊断；无真实样本时交付fixture工程通过与外部证据待满足。

### PR25-09 成本模型与留后日评价

- 复用：execution_sample_report及当前按整日划分的诊断；新增冻结模型协议，不随机按fill拆分。
- 第一轮：现有常数、按日稳健基线、一个简单正则/条件模型。决策时已知价差、波动、规模/可见深度、方向和市场身份可作输入。
- 同parent全部child/fill同组；训练价格/费用事实须在测试边界前成熟；样本外测试日不参与模型、阈值或特征选择。
- 输出：模型身份、训练截止、预测时点、有效期、支持域、方向/规模/状态分层、按日等权及名义金额加权误差、低估与区间覆盖。
- 测试：未知/域外规模unsupported、过期模型、未来特征拒绝、稀疏分层insufficient、已知偏差受控数据、测试集不可拟合。
- 验收：工程覆盖下限与统计精度分别判断；10/5日和每层5笔不自动批准校准/容量。停止规则和延长观察上限在评价前固定。

### PR25-10 实施差额、成本provider与账户桥

- 复用：Broker实际成本、Portfolio/lot事实及单期优化器。
- 拟新增ExecutionCostProvider：返回model/protocol hash、target/anchor/horizon、bps单位、预测分量、区间、available_at、expiry、支持域和不足原因。
- 分离价格shortfall、全意图实施差额、费用、借币/funding及残余库存；部分成交、拒单、撤单和零成交都保留。
- 预测成本进入目标函数，实际成本只由Broker/交易事实入账。实盘模型参数不由研究脚本改写。
- 求解器首轮保持现有单一非负trading_cost_bps接口：按训练期冻结的聚合方式，把域内预测转换成一个明确的保守标量，并记录信息损失。逐资产/方向向量属于另行登记的接口扩展，不能写成已经接入。真实返佣可为负，账本按事实保留；不能直接塞入当前要求非负的L1惩罚。
- 测试：买卖手算、返佣与第三币种费用转换、剩余未成交且缺终值时不足、全部成交无终值仍可计算、费用不重复、标量聚合与非负契约、风险减仓优先、pending预留、provider坏hash/过期/未来时点。
- 验收：目标不等于成交；现金、持仓、费用和权益桥在既有精度容差内，域外规模不被描述为可执行容量。

### PR25-11 显式协方差估计接口

- 复用：size_portfolio_targets及原有固定20%对角收缩。
- 拟新增纯函数模块（建议core/covariance_estimators.py，供运行内核与研究共同使用）和tests/test_covariance_estimators.py；不能让core导入analysis形成反向依赖。
- 初始三臂：sample、fixed_diag_020、lw_constant_correlation。若加入lw_scaled_identity，另登记候选与预算，并明确其目标不同。
- 统一输入：截止前共同完整简单收益；样本去均值；输出ddof=1口径、日频矩阵；年化只在声明边界乘365一次。若论文强度计算采用1/T矩，明确转换公式和参考验证。
- 审计：样本量、centering、ddof、单位、目标、强度、特征值、退化策略及算法版本。
- 测试：两资产手算；至少三资产、非均匀相关的独立强度参考fixture；强度0/1端点、对称/半正定、排列等变、单资产/零波动/共线/N>T、未来前缀不变。两资产常相关target=S，明确验证此时输出等价与强度分母为零的退化处理。
- 验收：保留现有60日、ddof1、365、20% fixture；默认关闭无结果漂移。增加forecast均值和协方差同一horizon的转换测试：日频输出不能重复年化；当前年度矩阵除365再乘horizon的接线须保持一致。均值线性缩放和协方差乘horizon属于既有近似，记录其自相关/复利限制。

### PR25-12 协方差风险与组合对照

- 实验一固定个体波动输入，比较各矩阵的组合风险预测与总敞口。常相关与对角收缩保持对角时，相对逆波动权重不应被声称改变。
- 实验二若有需要，再登记将矩阵接入同一成本优化器，观察相对权重、求解和成本结果；不根据实验一赢家扩大网格。
- 评价：后来共同期限的组合风险预测误差、目标波动偏差、换手、集中度、成本后收益/回撤；报告真实风险与平均敞口。
- 可先用既有BTC/ETH做退化工程复验，此时sample与论文常相关估计应一致；验证收缩强度和分散化结论须用至少三个异质相关资产以及有证据的多资产面板。
- 验收：数学/约束正确即可完成工程；未改善或区间过宽时保留原基线。

### PR25-13 因子归因与共同风险

- 复用：现有factor_attribution、因子定义/输入；拟新增可传factor IDs和版本的通用归因接口。
- 输出：alpha/beta、样本、秩、条件数、残差、HAC/时间块区间及因子/策略费用融资口径。
- 测试：满秩确定矩阵y=alpha+X beta手算、共线/常数列、缺失/重复/不同日期拒绝、滞后权重、未来修订不改变过去。
- 局部proxy与完整论文因子分开；缺成员/市值不静默重加权，不将市场残差解释为因果alpha。
- 验收：能解释原有策略的共同风险与增量不确定性，因子归因不足也完整交付。

### PR25-14 单期成本与调仓消融

- 复用：allocate_cost_aware与PortfolioTargetController，硬L1换手约束已在优化内部，避免依据旧文档重复开发。
- 第一轮固定forecast、risk、实际成本与候选，单独改变成本预测provider；第二轮才固定provider比较immediate/partial/band。
- 将mandatory减险与主动换手分开，检查未完成目标和恢复预留；更高成本导致低仓位的结果不能解释为费用越高收益越好。
- 测试：常数provider与旧结果一致、单资产解析解、硬换手下的非比例最优、预算/现金上限、风险退出、不可行和求解超时。
- 验收：成本来源、实际费用和风险匹配结果可追溯；盈利更高不是工程通过条件。

### PR25-15 多期配置条件支线

- 启动条件：单期消融已完成且存在可解释的预测衰减/重复换手瓶颈，有合格成本输入和明确预算。
- 拟新增allocate_multi_period，消费预测路径、协方差/成本路径、真实当前权重、pending预留及各期risk envelope。
- 只执行第一期目标；计划路径不产生成交，不把未来预测当已知事实。H=1须退化到验证过的单期问题。
- 测试：单资产手算、H=1、逐期现金/风险约束、零信号、失败/超时、未知订单保留预留、检查点恢复。
- 交付：第一目标、规划路径、求解状态、约束残差及模型版本；未满足启动条件保持后续扩展。

### PR25-16 现有ML与状态层支持诊断

- 复用：episode/funnel diagnostics、batch comparison、event_concentration和probability diagnostics。
- 工作：区分无信号、资格不符、模型拒绝、资金竞争、健康暂停、风险拒绝、未成交；按book/日期/状态/时间块报告支持。
- 比较：现有规则、Ridge、既有冻结RL与同参与率基线；必要时复用现有固定模型族，不扩大训练/阈值搜索。
- 保留现有P1/P2/归因最低支持政策，样本数、ESS、独立日期和事件分别报告；多seed不增加市场样本。
- 测试：无真实资金竞争的排名变化不称分配价值；零交易合法但无盈利证据；概率分数/净收益预测/入选概率的语义不同。
- 验收：模型有效性的不足可定位；不因支持不足降低门槛或反复调阈值。

### PR25-17 前向原引擎账户轨迹核验

- 当前缺口：evaluate_forward_store固定保留原引擎成交/权益待证据；shadow决策和proxy标签不能补齐组合收益。
- 拟新增ForwardAccountReceipt及load/verify入口，绑定协议、冻结候选、会话、原始接收、决策、intent、order、fill、lot、费用、持仓和equity。
- 前向数据必须来自真正登记后接收的收据；后来离线回放须标为“已记录前向输入的模拟账户重放”，单独记录何时生成决策，不能冒充当时实时决策。
- 测试：部分成交、资金不足、拒单、零交易、unknown订单、缺会话、坏hash、重启/中断恢复；decision-only和proxy无法提供portfolio_return。
- 验收：依据真实轨迹才更新账户pending；保留proxy与账户两个结果，仍不自动进入真实资金准入。

### PR25-18 有限研究编排与证据报告

- 复用：既有paper runner、研究证据与报告；新增统一协议reader/报告器，必要时拟新增scripts/run_paper_research_study.py。
- 每次运行前枚举完整job表、预算、输入与身份；试跑仅用于性能/资源，不查看最终效果后改网格。
- 限时、限内存、限并发、可恢复；失败/中断与输出不完整单列，不能重启成全新成功记录。
- 报告含coverage、统计、成本、风险、资金桥、局限与候选全表；各层原始状态保留。
- 测试：预算超限、重复候选、旧目录覆盖、源码漂移、缺结果、失败候选、恢复去重、非法JSON/NaN。
- 验收：计划job数=完成+失败+中断+尚待运行；无结果被静默丢弃；不自动打开最终样本。

### PR25-19 集成、迁移及故障验收

- 集成链：可见数据→特征→成熟标签→候选→收益面板/协方差/成本→目标→风险→真实模拟成交→账户→报告。
- 重点：三种时间模式隔离、v1/v2报告读取、默认关闭、pending/unknown恢复、共享参与率、成本只记一次、最终样本消费身份。
- 运行定向回归、必要完整CI、受影响固定账户差异；正式干净源码验收再按main_acceptance流程。
- 验收：无未解释账户差异和关键错误，失败/不足没有被上层改为pass；局部工程通过与研究/外部待证据分开。
- 状态同步：实际关闭范围写入权威计划/详情/登记与版本化完成证据；不把本计划23个工作包当成23个已验收项目任务。

### PR25-20 新候选冻结与前向协议

- 从本研究中仅按事前规则选定有限候选；无支持则交付负结果并停止，不强制产生新候选。
- 冻结源码、模型、配置、数据规则、基准、风险/成本、日期、访问政策和停止条件；按旧协议变更规则创建后继身份。
- 读取当前真实观测判断状态，不以今天日期推断成熟，不继承旧候选经过的天数。
- 测试：原协议不能篡改、未到期拒绝、身份漂移阻断、观察缺口/重复/时钟错误、后继身份和来源可追溯。
- 验收：协议工具工程与未来证据分别记录；不修改旧候选最终窗口。

### PR25-21 到期裁决与交接

- 前置：相应协议实际到期、标签/费用成熟、访问规则满足及所有必要账户证据齐全。
- 工作：一次受控评价，按预登记主指标/阈值和完整族范围给supported/not_supported/insufficient；交付全部失败与不足。
- 未到期时仅检查覆盖、完整性和采集健康，不查看禁止访问的绩效以改变模型。
- 外部等待时间单独记录；最终结果不理想不自动延长样本直到显著。
- 交付：候选结论、局限、是否保留研究分支和下一负责人。真实准入仍走既有独立流程。

### PR25-22 最终样本实际访问与单次消费

- 复用：HoldoutProtocol与HoldoutVault；当前文本purpose和单文件状态不足以独自约束并发/实际数据读取。
- 拟改：把身份校验和单次消费绑定实际评估入口，采用原子状态转换及确定的恢复身份；两个进程只有一个取得首次评价资格。
- 状态设计：registered→frozen→evaluating→completed/failed_consumed；崩溃后仅恢复同一固定evaluation_id，不恢复为可重新选参。
- 测试：并发打开、写入中断、重复调用、模型/数据hash漂移、未知用途、warmup与计分边界、测试标签变化不能改变训练选参。
- 本任务只用合成协议，不消费现有真实最终窗口。文件标志无法阻止人手动读取所有文件，应明确实际保护边界。
- 验收：完整访问日志、失败消费事实和恢复链可审计，旧已看历史仍为retrospective。

## 5. 数学口径和研究验收

### 5.1 实施差额

令s=+1表示买入、s=-1表示卖出，意图数量Q，事前参考价P0，实际成交为(qj,pj)，预登记终值PT。全部价格、币种及合约单位一致。

~~~text
executed_price_shortfall_bps =
  10000 * s * sum(qj * (pj - P0)) / (P0 * sum(qj))

order_implementation_shortfall_bps =
  10000 * (
      s * sum(qj * (pj - P0))
    + s * (Q - sum(qj)) * (PT - P0)
    + explicit_quote_fees
  ) / (P0 * Q)
~~~

第一项在零成交时不足。第二项需要正意图数量及完整费用/转换证据；只有仍有剩余未成交数量时，才需要预登记终值。此时终值缺失不能把机会成本补零。完成全部成交时机会成本为零，无关终值缺失不阻断该指标。借币/funding按其持有期现金流单列核对，不重复塞进价格偏离。

模型验收比较日级误差差值和区间，检查平均低估、尾部误差、预测区间覆盖及支持域。预登记允许的bps低估/精度预算由策略净优势和用途确定；不能从测试结果倒推。

### 5.2 协方差

~~~text
S = centered_returns' * centered_returns / (T - 1)
fixed_diag_020 = 0.8 * S + 0.2 * diag(diag(S))
constant_correlation_target:
  diagonal(i) = S(i,i)
  off_diagonal(i,j) = average_pairwise_correlation * sqrt(S(i,i) * S(j,j))
daily_covariance_to_annual = 365 * daily_covariance
daily_volatility_to_annual = sqrt(365) * daily_volatility
~~~

收缩强度严格按选定论文/估计器定义及参考结果核验；不要将不同软件默认目标、ddof和中心化方式混为一体。必要数值修复需记录修复前后特征值及政策，不能静默把不合法输入修成“正常”。

### 5.3 有限实验预算

| 实验族 | 第一轮计算预算 | 控制 |
| --- | --- | --- |
| COV | 3臂×3个固定已见窗口×2成本情景=18次账户回放 | 60日回看固定；真实多资产输入不足时先只验工程，不作分散化推广 |
| COV-OPT | 仅有理由时再登记相同最多18次 | 固定同一forecast/provider/约束，只改变矩阵 |
| ALLOC-PROVIDER | 3个成本provider×3窗口×2成本情景=18次 | 三provider为旧假设、校准点预测、校准保守值；缺真实数据则只作明确假设敏感性 |
| ALLOC-REBALANCE | 一个冻结provider×3调仓方式×3窗口×2成本情景=18次 | 不与provider组成无限笛卡尔积 |
| META | 沿用已登记现有模型族及对照，总计至多10个arm；新增RL训练0次 | 同参与率对照计入10臂预算，结果前固定角色/seed；不得结果后删失败臂 |
| FACTOR | 最多7个预登记策略×2套固定因子集合×3个历史窗口=42次归因拟合 | 两套为现有代理/合格子集因子；数据不足的集合保持待证据，不临时换定义 |
| RISK-CONTROL | COV、ALLOC-PROVIDER、ALLOC-REBALANCE各最多另18次，总计最多54次账户回放 | 事前固定训练期风险/敞口匹配方法与参数，不用评价期实际波动倒推杠杆 |
| STAT | 手算fixture与有限相关过程校准，模拟次数由误差目标固定 | 仅研究校准，不消费实际最终窗口 |

风险匹配比较须在初始协议固定算法与预算：依据训练期风险或当时可得预测设定共同目标，并保留原风险上限，不用整个评价期已实现波动事后重设杠杆。未被初始预算覆盖的追加分析另登记为探索性，不能用于确认同一已见样本上的优势。条件COV-OPT若涉及收益优势主张，也须另行事前登记其风险对照预算。

上述预算是新的建议上限，未运行。窗口可沿用2021–2022、2023–2024、2025–2026-09-19作为历史诊断，全部注明已经看过；实际协议必须记录准确起止、时区、热身和重置/连续账户规则。多个窗口重启结果不得拼成连续可执行收益。

真实成本模型的每个时间外窗口必须有更早且已成熟的训练证据，并满足model/provider在决策前可得。新采集的2026年10月成交拟合模型不能作为2021年至2026年9月的当时预测；若用其数值重放旧窗口，只能以声明的假设配置开展回顾性参数敏感性，不伪造provider的available_at，也不据此评价时间外预测能力。

候选ID描述固定方法/参数/模型身份，窗口属于run_id；同一方法在不同窗复验不按窗口把收益列补零拼接。每窗建立同轴局部面板，跨窗比较的主张按预登记规则计入搜索范围；窗口重启收益不拼成连续账户。

成本情景须分开“实际撮合成本”与“优化器所见预测”。主比较固定实际成本只变预测；联合改变两者仅作另行声明的压力情景，不能据此归因预测改善。

### 5.4 结果处理

| 结果 | 操作 |
| --- | --- |
| 工程通过，研究不足 | 可保留默认关闭的实现；列明缺日期、支持、费用、来源或精度，继续独立工作 |
| 工程通过，研究不支持 | 保留原基线与负结果；结束该候选族，不扩搜索直到出现赢家 |
| 研究指标变好但风险/敞口不同 | 执行预登记风险/敞口匹配对照；事后新增仅属探索性，未满足证据前不归因方法优势 |
| 无合格真实成本 | 历史假设研究可继续；真实校准/容量保持待证据 |
| 输入非法/账户不守恒/身份漂移 | 阻断依赖验收，保存失败，不用null/0覆盖错误 |
| 最终评价已消费后失败 | 保留消费和失败；同一身份仅允许协议规定的恢复，不重新调参后重开 |

## 6. 完整测试矩阵

下表已有文件均可复用；括号内“拟新增”仅是计划名称。执行前核对实际落地命名。

| 层级 | 要验证的行为 | 主要既有测试 | 新增重点 |
| --- | --- | --- | --- |
| T0 文档/计划 | 链接、任务唯一/依赖无环、文献/任务覆盖、文件hash | 本次文档检查；原Roadmap结构脚本 | 不要求交易测试 |
| T1 数学/单位 | CSCV/DSR/White、协方差、成本公式、优化解析解、因子回归 | [paper_validation](../../tests/test_paper_validation.py)、[cost_aware_allocation](../../tests/test_cost_aware_allocation.py)、[paper_study](../../tests/test_paper_study.py) | covariance_estimators、factor_attribution（拟新增），独立手算与参考 |
| T2 时间/身份 | 迟到/修订/撤销、成熟/同日cohort、源码闭包 | [data_versions](../../tests/test_data_versions.py)、[temporal_data_integration](../../tests/test_temporal_data_integration.py)、[temporal_labels](../../tests/test_temporal_labels.py)、[temporal_retractions](../../tests/test_temporal_retractions.py) | ML桥实际cutoff、未跟踪源文件/模型hash漂移 |
| T3 数据/统计合同 | 族完整、失败保留、索引、模型隔离、成员区间 | [unified_research_evidence](../../tests/test_unified_research_evidence.py)、[phase5_research_validation](../../tests/test_phase5_research_validation.py)、[historical_factor_inputs](../../tests/test_historical_factor_inputs.py)、[ml_selection_dataset](../../tests/test_ml_selection_dataset.py) | search_scope、holdout_lifecycle（拟新增）；并发消费与重上市 |
| T4 执行与账户 | 报价/费用资格、部分成交、风险/现金、成本桥 | [execution_readiness](../../tests/test_execution_readiness.py)、[execution_calibration](../../tests/test_execution_calibration.py)、[quote_observations](../../tests/test_quote_observations.py)、[v3_portfolio_execution](../../tests/test_v3_portfolio_execution.py) | provider、机会成本、失效/超时、第三币种费用 |
| T5 组合/兼容 | 默认一致、约束、风险优先与恢复 | [trend_portfolio_v3](../../tests/test_trend_portfolio_v3.py)、[cost_aware_allocation](../../tests/test_cost_aware_allocation.py)、[live_risk_action_lifecycle](../../tests/test_live_risk_action_lifecycle.py) | 新估计器关闭兼容、旧检查点/新provider |
| T6 ML/前向 | 参与率、概率、竞争、proxy/账户分离 | [ml_selection_diagnostics](../../tests/test_ml_selection_diagnostics.py)、[ml_selection_batch_comparison](../../tests/test_ml_selection_batch_comparison.py)、[ml_selection_next_forward](../../tests/test_ml_selection_next_forward.py)、[ml_selection_bridge_audit](../../tests/test_ml_selection_bridge_audit.py) | forward_account_trajectory（拟新增）及模式v2 |
| T7 故障/恢复 | 日志截断、磁盘失败、并发、坏hash、断线/时钟 | [binance_depth_observer](../../tests/test_binance_depth_observer.py)、[temporal_retractions](../../tests/test_temporal_retractions.py)、[ml_selection_stress_failures](../../tests/test_ml_selection_stress_failures.py) | 原子holdout、采样故障不改变交易返回、恢复不复制订单 |
| T8 集成/复现 | 原引擎端到端、报告/账本一致、相同输入重放 | [paper_application_integration](../../tests/test_paper_application_integration.py)、[paper_roadmap_runner](../../tests/test_paper_roadmap_runner.py)、[walk_forward](../../tests/test_walk_forward.py) | 跨模式全链路与固定账户差异 |
| T9 真实外部证据 | 时间外成本、真实日期、成熟标签、运行连续性 | 离线验收工具与不可变实际收据 | 单元/合成/沙盒测试不能代替 |

测试执行顺序：新增fixture→受影响单模块→跨模块专项→有限账户复验→必要完整CI。检查通过后仅在有新改动、新失败或未解决疑点时扩大/重复；不通过重复全套测试替代研究样本。

## 7. 现有可执行命令与执行条件

本节为开发者后续使用的命令模板，本次编写计划未执行这些交易/研究测试或数据命令。所有命令从仓库根目录运行。

### 7.1 新运行目录和环境

~~~powershell
Set-Location 'D:\QuantTradingV1'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$runName = (Get-Date -Format 'yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N')
$runRoot = Join-Path 'reports\paper_research_20261025' $runName
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

& $py --version
& $py scripts/check_environment.py
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed' }
& $py scripts/verify_lock.py
if ($LASTEXITCODE -ne 0) { throw 'Lock integrity check failed' }
~~~

环境检查与严格运行锁检查不同；需要严格复现时按[依赖管理](../dependency_management.md)使用strict-lock。CI当前Python3.11/Node22，仓库本地版本另行记录。不为运行计划命令无条件升级环境或重装GPU/ML依赖。

### 7.2 分组专项（现有测试）

运行与任务相关的组即可。每条命令后检查退出码并记录日志；新增测试落地后把实际文件加入对应组。

~~~powershell
$statTests = @(
  'tests/test_paper_validation.py',
  'tests/test_unified_research_evidence.py',
  'tests/test_phase5_research_validation.py',
  'tests/test_walk_forward.py'
)
& $py scripts/run_portable_tests.py -q @statTests --junitxml (Join-Path $runRoot 'statistics.xml')
if ($LASTEXITCODE -ne 0) { throw 'Statistics tests failed' }

$dataTests = @(
  'tests/test_data_versions.py',
  'tests/test_temporal_data_integration.py',
  'tests/test_temporal_labels.py',
  'tests/test_temporal_retractions.py',
  'tests/test_historical_factor_inputs.py',
  'tests/test_ml_selection_dataset.py'
)
& $py scripts/run_portable_tests.py -q @dataTests --junitxml (Join-Path $runRoot 'temporal-data.xml')
if ($LASTEXITCODE -ne 0) { throw 'Temporal data tests failed' }

$executionTests = @(
  'tests/test_execution_readiness.py',
  'tests/test_execution_calibration.py',
  'tests/test_quote_observations.py',
  'tests/test_binance_depth_observer.py',
  'tests/test_cost_aware_allocation.py',
  'tests/test_delayed_execution.py',
  'tests/test_live_risk_action_lifecycle.py'
)
& $py scripts/run_portable_tests.py -q @executionTests --junitxml (Join-Path $runRoot 'execution.xml')
if ($LASTEXITCODE -ne 0) { throw 'Execution tests failed' }

$portfolioTests = @(
  'tests/test_trend_portfolio_v3.py',
  'tests/test_cost_aware_allocation.py',
  'tests/test_paper_portfolio.py',
  'tests/test_paper_study.py',
  'tests/test_v3_portfolio_execution.py'
)
& $py scripts/run_portable_tests.py -q @portfolioTests --junitxml (Join-Path $runRoot 'portfolio.xml')
if ($LASTEXITCODE -ne 0) { throw 'Portfolio tests failed' }

$forwardTests = @(
  'tests/test_ml_selection_next_forward.py',
  'tests/test_ml_selection_bridge_audit.py',
  'tests/test_ml_selection_integrity.py',
  'tests/test_strategy_forward_intake.py'
)
& $py scripts/run_portable_tests.py -q @forwardTests --junitxml (Join-Path $runRoot 'forward.xml')
if ($LASTEXITCODE -ne 0) { throw 'Forward tests failed' }
~~~

run_portable_tests在Windows使用独立可写临时目录，测试入口本身不改变生产ACL。现有ML测试需要requirements-ml中的可用CPU依赖；缺依赖必须报告，不能静默跳过后称该组通过。

### 7.3 本地执行证据资格审计（现有，只读）

输入库路径由实际证据清单指定。下面的样例路径不代表文件必然存在；先检查再运行。输出是新目录。

~~~powershell
$orderStore = 'reports\live_orders.db'
if (-not (Test-Path -LiteralPath $orderStore -PathType Leaf)) {
  throw 'Supply an existing order evidence store'
}
$readinessDir = Join-Path $runRoot 'execution-readiness'
& $py scripts/audit_execution_readiness.py --order-store $orderStore --output $readinessDir
if ($LASTEXITCODE -ne 0) { throw 'Readiness process failed' }

$readiness = Get-Content -LiteralPath (Join-Path $readinessDir 'readiness.json') -Raw | ConvertFrom-Json
if ($readiness.status -eq 'invalid_inputs') { throw 'Execution evidence inputs are invalid' }
$readiness.status
$readiness.blockers
~~~

可追加实际存在的request/quote/depth证据，重复选项以现有CLI为准。该脚本可能正常退出但输出gaps/invalid_inputs，必须读取JSON；gaps保留待证据，不能由进程0推导通过。原始库不写入，目录名或校准报告不替代真实成交来源。

### 7.4 离线成本诊断（现有）

~~~powershell
$executionInput = 'reports\execution_input.json'
if (-not (Test-Path -LiteralPath $executionInput -PathType Leaf)) {
  throw 'Supply an existing identified execution input JSON'
}
$calibrationFile = Join-Path $runRoot 'execution-calibration.json'
& $py scripts/calibrate_execution.py --input $executionInput --output $calibrationFile --prequote-at submit --max-quote-age-seconds 1
if ($LASTEXITCODE -ne 0) { throw 'Calibration process failed' }
$calibration = Get-Content -LiteralPath $calibrationFile -Raw | ConvertFrom-Json
if ($calibration.status -in @('invalid', 'invalid_inputs')) { throw 'Calibration inputs are invalid' }
$calibration.status
$calibration.coverage
$calibration.calibration.status
$calibration.calibration.reason
~~~

必须同时解释顶层和嵌套calibration的status/reason；顶层diagnostic不保证内部已完成拟合，内部insufficient继续待证据，null不能当作0或通过。

输入JSON的source/as_of和证据必须真实；source声明本身不证明来源。严格真实诊断还需raw_quotes/fill_provenance及对应账本等证据。当前CLI没有protocol、split-date或minimum-train-days参数，不得把计划中的配置示例当作现有可执行参数。价格/真实成本/容量与production_approved分开解释。

### 7.5 现有历史研究runner

[scripts/run_paper_roadmap.py](../../scripts/run_paper_roadmap.py)当前支持manifest、observations、output、fine-manifest、execution-input。它的默认observations指向历史本地产物，干净检出可能不存在；其固定矩阵不是本计划新的18次实验。开发者须先验证输入及运行预算，再按脚本帮助运行；不作为本次文档校验步骤。

拟新增统一研究runner、协方差研究命令和多期配置CLI均需先实现、测试并加入[脚本索引](../../scripts/README.md)，本文件不提供可误执行的假命令。

### 7.6 集成与CI

依据[现有CI](../../.github/workflows/tests.yml)：

- 环境/运行锁、Ruff、历史基线、仓库卫生、Roadmap结构；
- 关键runtime类型检查，前端受影响时执行dashboard Node测试；
- 全量pytest覆盖core/backtest/live_trading，当前覆盖下限55%；
- 独立CPU ML研究job使用合成输入，现有超时15分钟；
- sandbox测试发现/凭证缺失的跳过须按真实边界报告，不作为真实交易所通过证据。

后续集成可运行：

~~~powershell
& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { throw 'Ruff failed' }
& $py scripts/verify_baseline_archive.py
if ($LASTEXITCODE -ne 0) { throw 'Historical archive verification failed' }
& $py scripts/check_repository_hygiene.py
if ($LASTEXITCODE -ne 0) { throw 'Repository hygiene failed' }
& $py scripts/verify_roadmap_completion.py --structure-only
if ($LASTEXITCODE -ne 0) { throw 'Roadmap structure failed' }
& $py -m mypy core/domain.py core/runtime.py live_trading/execution_adapter.py
if ($LASTEXITCODE -ne 0) { throw 'Runtime type check failed' }

& $py scripts/run_portable_tests.py -q --cov=core --cov=backtest --cov=live_trading --cov-fail-under=55 --junitxml (Join-Path $runRoot 'integration.xml')
if ($LASTEXITCODE -ne 0) { throw 'Integration tests or coverage failed' }
~~~

structure-only只检查结构，不核验所有历史产物，也不替代新工作包验收。完整main_acceptance要求已提交的干净源码快照和其特定环境，按[scripts/main_acceptance.py](../../scripts/main_acceptance.py)执行；未满足时记录待执行，不能为了消除检查而忽略用户改动或重写受保护历史。

## 8. 证据包、迁移与保存

### 8.1 目录与身份

拟议每次运行目录：

~~~text
reports/paper_research_20261025/<unique-run-id>/
  registration.json
  source_manifest.json
  environment.json
  input_manifest.json
  attempts.jsonl
  protocol.json
  checks/
  models/
  account_trajectories/
  metrics/
  comparison.csv
  acceptance.json
  SHA256SUMS
~~~

字段为拟交付要求，不表示目录已经生成。报告目录必须全新；中断恢复保留原run关系与attempt序号，不能覆盖失败结果。注册/日志应避免写入密钥及账户敏感明文。

每个验收至少包含task_id、run_id、accepted_scope、contract_version、实际源码/配置/模型/数据hash、环境、正反例/故障/兼容/集成检查、主指标和限制。旧报告读取需显式adapter，不把schema缺字段补成真实已满足。

### 8.2 兼容与回滚

| 变更 | 兼容检查 | 回退策略 |
| --- | --- | --- |
| 新估计器/provider | 默认路径与旧fixture一致；旧配置和报告可读 | 关闭研究开关回到旧算法；保留旧模型/报告身份 |
| 时间/标签/成员v2 | v1回顾性读取不漂移；strict显式选择 | 选择旧研究模式并明确假设；不能将strict失败静默降级为正式合格 |
| forward账户回执 | 旧decision-only继续pending，不伪造收益 | 不读取新回执，仅保留原决策/proxy诊断 |
| holdout状态 | 迁移保持已消费事实 | 修复/恢复同一evaluation_id；禁止回滚成“未打开” |
| 控制器/检查点 | pending/unknown预留和风险退出可恢复 | 旧模型保留已批准订单事实；不凭切换模型删除持仓或重复下单 |

现有不可变批准风险、保护止损、现金/参与率、unknown订单与急停契约继续有效。所有回滚均保留实际订单与成交事实。

### 8.3 资源与保留

- CPU确定性算法及小数据fixture优先；本阶段RL新增训练预算为0。
- 并发、最大运行时、磁盘上限在小型性能试跑后写入协议；共享BLAS线程与内存计入总预算。
- 缓存键绑定代码/数据/配置/种子；变更后禁止跨身份复用。同一原始数据可用内容寻址快照复用，验收前验证引用可取回。
- 临时渲染、可复算特征和中间checkpoint可按保留策略清理；候选登记、失败尝试、最终模型、实际观察原文和验收所需输入不得自动删除。
- 不采用“训练一次删除所有材料”的做法。先有可验证归档/重建路径，再清理允许清理的缓存。
- 遵守[文件保留规则](../file_retention.md)，不清理旧受保护证据，不覆盖历史研究目录。

## 9. 代码审查和关闭清单

实现PR描述应让未参与讨论的复核者看清触发问题、前后行为、接口、风险和实际验证。每项关闭前核对：

1. 代码行为与论文版本/公式/迁移假设对应，新增接口在文档中标明已落地版本。
2. 实际源文件依赖闭包包括ML/research、模型和配置；运行前后身份一致。
3. 正例、拒绝非法输入的反例、失败/恢复和旧模式兼容有可定位证据。
4. 账户桥、单位、时间、成本及主指标一致；未建模项和缺证据明确。
5. 全部候选/失败/中断与预算对得上，没有按结果删臂或改基准。
6. 工程/研究/外部/准入各维度真实，null和insufficient未被上层掩盖。
7. 最终窗口访问与候选身份符合原协议；没有提前消费真实最终样本。
8. 相关文档、脚本索引及正式任务验收范围同步；所用工具的结构通过未被称为完整证据通过。
9. 输出可打开、hash可核验、恢复和下一责任角色所需输入齐全。
10. 结果未改善时保留负结果和原基线，后续工作依据真实瓶颈安排。

本计划的交付完成标准是文档的链接、编号、依赖、覆盖和hash检查通过；上述开发任务及研究/运行验收等待其后实际实施与证据。

## 10. 首批开发与验收清单

[机器可读任务计划](paper_research_task_registry_20261025.json)提供本文件23项任务的计划视图，包括依赖、责任角色、估算、已有/拟新增文件、测试范围和证据要求。它不是正式项目任务登记，不能直接传给现有roadmap_priority或完成验收脚本。全部planning_status初始为planned，实际执行/验收证据为空；依赖图根节点不代表已经具备验收资格。

本文件和Roadmap约定内容出现不一致时，应复核后一起修订计划视图，不能据此静默覆盖正式任务状态。JSON单向记录两份计划文档最终字节的SHA-256；Markdown只链接JSON，不嵌入其hash。

### 10.1 第一迭代：固定基线与登记合同

范围固定为PR25-01→PR25-02，PR25-00并行核对来源。预计3.5–6人日，属于总估算的一部分，不额外相加；若依赖、环境或实际源码范围超出预期，先重估，不按时间宣称完成。

| 次序 | 工作 | 具体交付 | 判断及停止条件 |
| --- | --- | --- | --- |
| 1 | 核对任务与工作区 | HEAD/受控差异、未跟踪源码范围、已知用户改动、环境/依赖身份 | 未识别的执行源文件继续补齐；保留用户改动 |
| 2 | 选定小型固定输入 | fixture/data manifest、配置、种子、账户初资及比较字段 | 不消费实际最终样本，不以大型搜索作为环境检查 |
| 3 | 保存当前基线 | current_state.json、environment.json、baseline_diff.json及未解决失败清单 | 旧失败独立记录；准备基线不等于正式验收 |
| 4 | 固定研究身份合同 | 包含research/ML/模型/配置/依赖/编排的依赖闭包及实际字节检查规则 | 外传hash不能替代本地核对；缺执行依赖阻断身份完整结论 |
| 5 | 固定全部尝试合同 | 小候选族、planned/started/completed/failed/interrupted、恢复关系和范围声明 | 不删除失败，不补造此前历史；日志缺失不得称族完整 |
| 6 | 运行针对性验证 | 身份变更/未跟踪源码/失败恢复等最小反例、必要JUnit与差异 | 精确记录新增/复用测试；失败先修复再跑受影响范围 |
| 7 | 独立复核交接 | accepted_scope、检查索引、限制、下一批输入 | 工程、研究、真实外部证据分开；只有真实通过的范围可登记关闭 |

本迭代的文献交付：原始来源版本/阅读层级可以先完成；Excel编号映射未取得可读副本时保持待证据，不能将8篇候选自动当成完整表内筛选结果。

### 10.2 各阶段最低验收证据

下面是拟交付证据的内容要求，文件名允许在正式任务卡登记后调整；不能仅创建空文件就视为满足。

| 阶段 | 至少交付的证据 | 允许保留的不足 |
| --- | --- | --- |
| A | 环境、源码/数据/配置身份、有限协议、完整尝试合同、基线差异 | Excel映射或实际环境问题分别列明 |
| B | 迟到/修订/撤销反例、标签版本/成熟资格、成员区间与覆盖 | 不完整历史成员、不可解析细bar及原始发布时间未知 |
| C | 数学fixture、完整候选面板、依赖校准、原子单次消费/恢复 | 完整历史搜索不可证明，实际有效样本不足 |
| D | 来源/时钟/费用资格、成本预测/残差、账户资金桥 | 无真实成交、方向/规模覆盖或时间外精度不足 |
| E | 估计器定义、三资产强度参考、单位/期限测试、固定账户差异 | 多资产外部证据不足或研究未改善 |
| F | 因子定义、有限比较全表、支持矩阵、风险/敞口匹配及负结果 | 因子仅proxy、独立事件或收益证据不足 |
| G | 原引擎账户轨迹、模式/版本兼容、故障与恢复、集成/CI回执 | 真正前向市场观察尚未到达或成熟 |
| H | 冻结协议、实际收据、到期/成熟核验、单次裁决与限制 | 未到期继续pending，不用日历过去推断完成 |
| BX | 启动理由、H=1退化、多期约束与第一动作/成交分离 | 没有证据证明单期瓶颈时保持后续扩展 |

### 10.3 任务交接记录模板

以下是拟使用的交接内容，不是已经完成的回执：

~~~yaml
task_id: PR25-01
accepted_scope: fill-only-after-actual-review
executor: null
reviewer: null
run_id: null
source_manifest_sha256: null
config_sha256: null
input_manifest_sha256: null
checks:
  positive: {status: not_run, evidence: null}
  negative: {status: not_run, evidence: null}
  reconciliation: {status: not_run, evidence: null}
  compatibility: {status: not_run, evidence: null}
  fault: {status: not_run, evidence: null}
  integration: {status: not_run, evidence: null}
engineering: pending
research: not_evaluated
external_evidence: pending
limitations: []
unresolved_failures: []
next_owner_role: null
~~~

执行时按任务风险选择必要检查；不适用项必须说明理由。纯文档任务使用links/ids/coverage/hashes，不要求无关行为测试。已有正式完成契约和MetricResult枚举仍为权威，模板需显式适配，不能凭填写pass关闭正式任务。
