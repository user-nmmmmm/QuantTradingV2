# S2 动态选币与受约束换仓契约 v1

适用：统一路线图第 6 节 S2、SYS-12 的本地工程能力；实现为
[`core/selection.py`](../core/selection.py)，专项验收为
[`tests/test_s2_selection.py`](../tests/test_s2_selection.py)。本模块默认不连接正式路由，
输出 `formal_routing_enabled=false`。本契约不改变 SYS-11、健康恢复、人工锁定或实盘准入条件。

## 输入、来源和时点

`select_targets` 接受三个有版本边界的输入：

- `MembershipFact`：标准化交易对、上市/退市动作、生效时间 `effective_at`、信息可得时间 `available_at`。
- `FactorObservation`：标准化交易对、因子观测时间 `observed_at`、信息可得时间 `available_at`、因子值和报价币成交额。
- `DataProvenance`：来源 URI、`real`/`synthetic` 分类，以及由 `selection_input_digest` 生成的输入内容 SHA-256。

哈希覆盖传入的完整成员事实与因子观测记录，读取时重新计算并核对；禁止 NaN/Infinity JSON。
哈希验证能够证明这些传入记录未偏离声明的内容身份，不能证明交易所历史本身真实，
也不等同于对原始下载文件的独立校验。真实数据仍输出
`provenance_authenticity=requires_external_source_verification`。下载文件、供应商时间语义、
缺漏下架标的及来源交叉核验属于外部验收；本轮测试均明确标为 `synthetic_fixture`，
须显式 `allow_synthetic=True` 才能使用，不提供伪造的真实研究成绩。

所有时间统一 UTC；兼容原系统的无时区时间按 UTC 解释。`available_at <= as_of` 的事实才可见。
因子还要求 `observed_at <= available_at`，先按观测时间、再按可得时间取最近可见修订；
缺失的新修订不能回填为旧版本的高分。同一交易对/观测时间/可得时间不允许重复。
每个市场身份允许一次上市和一次退市；重新上市必须另建市场身份，不默默覆盖历史。

追加合法的未来记录、未来修订或尚不可得的退市公告不会改变先前决策 ID、排名和目标。
完整数据 SHA-256 可以改变，因果决策 ID 只覆盖当时可见决策事实与政策。选择器不读取
已有 `PointInTimeUniverse.apply` 的未来末 bar `scheduled_exit` 标记，避免用历史全样本推算退出。

## 横截面与目标权重

过滤顺序包括 PIT 上市资格、可见退市公告、显式 `symbols` 范围、最小上市时长、
最新因子是否缺失/过期/早于上市、报价币成交额。因子或流动性缺失即排除。
`explicit_symbols=None` 使用全部合格成员，空列表明确选择现金；显式名单不能豁免 PIT 和数据校验。
原主程序固定 `--symbols` 行为没有变化。

横截面按分数从高到低排序，同分按标准化交易对确定顺序；平均秩百分位为
`rank(method=average, pct=True)`，zscore 为 `(score - mean) / population_std`。
常数横截面 zscore 为 null，并给出 `constant_cross_section`，不把零伪装成有效标准化证据。

TopN 缓冲首先保留位于前 `top_n + buffer_ranks` 的合格旧持仓，再由最高分补齐空位。
每个选中标的权重为 `min(max_symbol_weight, gross_target / selected_count)`，不重新分配
被单标的上限截去的资金，剩余全部记为现金。旧持仓、排除标的和已知退市目标明确为零。
该版本实现多头现金 TopN；分层组合与空头组合不在 v1 范围。

## 目标到订单提议

`plan_rebalance` 在严格晚于选币观测的时间运行，输入权威持仓、已确认挂单、
最新报价/已知成交量、最小名义额/数量精度、权益、已结算现金和风控层剩余批准买入额度。
必须显式 `facts_reconciled=True`；存在未知订单或未对账外部仓位时调用方不得声明为真。
本模块不发放风险批准，也不替代现有 `clamp_entry_qty`、止损预算或正式账户风险网关。

新买入数量同时受以下上限约束：

1. 目标数量减去当前持仓及净挂单数量。
2. 剩余已批准买入名义额度、单标的敞口和组合总敞口。
3. 已结算现金扣除待买单完整现金预留，计入不利滑点与费用估计。
4. 当期已知基础币成交量乘参与率，扣除所有策略共享的已预留参与数量。
5. 单轮双边总名义额换手上限，扣除同轮已经成交或承诺的 `committed_turnover_notional`。
6. 向下取整的数量精度和最小名义金额。

未成交卖单既不释放现金，也不释放组合敞口给新买入。每轮提议中的新买单共同扣减现金、
总敞口和换手余量；每个标的最多一条提议。无剩余风险额度不能买入。
未确认、仍在途中、已取消待核对或跨进程共享的挂单预留必须由既有权威订单投影提供，
不能通过传空字典清除。净挂单只防止重复数量规划，不构成真实成交。

正常换仓遵守 `min_rebalance_hours`。`proposal_id` 由决策、规划时点、持仓/挂单事实和订单内容
确定，同事实重放 ID 相同。运行层必须持久化去重并先核对该 ID 对应的订单；本纯函数不持有
运行数据库，也不会自行提交、重试或声称已成交。重启后根据真实成交更新剩余目标与预留。

费用和滑点为提议阶段估计，真实成交需由 Broker 的成交/lot/position 事实及账本对账。
`pending_targets` 记录受限或部分规划的剩余目标，`proposal_only_no_execution_claim` 不能
转写成执行完成。实际费用、更坏的跳空及交易所精度仍需执行时重新校验。

## 退市退出

退市公告在知识时间可见后立即把目标设为零，不等待从未来数据计算最后可成交 bar。
强制退出可绕过普通换仓频率和可选换手限制，但仍受真实持仓、剩余卖单、参与率、
最小名义额和精度约束；强制退出换手单独列示。退市持仓若仍有买单，先返回取消买单要求，
待订单核对完成再规划卖出。部分成交、无量和尘埃持仓均保留退出目标。

达到已知退市生效时间后禁止编造卖出成交，输出 `market_unavailable_pending_exit`。
若公告只能在退市后获得，本地模块也不能制造提前清仓。执行层必须在每轮刷新最新成员和
订单事实；旧选择快照不能替代最新停牌/交易所状态检查。

## 研究预登记与仍待外部证据

本地确定性测试验证的是因果、预算和执行提议契约，不验证因子有效性。
真实研究开始前另建冻结协议，固定候选因子、观察频率、TopN/缓冲/流动性参数、成本、
目标持有期及 train/validation/holdout 划分。至少预登记：

| 维度 | 冻结定义和验收 |
| --- | --- |
| IC / IR | 因子与未来扣费收益的 Spearman IC；按观测日横截面计算，披露同分/常数/样本不足；IR 的频率与年化规则先声明 |
| 分位收益 | 只用当时可见因子分组；Top/Bottom 对照、缺数据和退市损失在独立样本核验 |
| Coverage | 当期满足 PIT、时效和流动性条件的标的数 / 可见成员及已有持仓数；披露各排除原因 |
| Turnover | 成交双边总名义额 / 同口径权益；提议换手和实际换手分列，强制退出独列 |
| Decay | 预先冻结若干持有期，再比较对应 IC/分位扣费收益；不得按 holdout 选最优期限 |

真实 PIT 源数据的独立真实性核验、完整历史退市覆盖、因子生成过程的无前视审计、
独立样本统计支持、共享账户全链路接线及正式 SYS-11 准入仍需分别验收。
输入字段声明 `available_at` 不会自动证明上游因子计算未使用未来数据。
不得读取现有前瞻协议未成熟样本选参，也不得把本次新源码沿用旧候选哈希。

### 已实现的独立事后诊断器

[`analysis/selection_research.py`](../analysis/selection_research.py) 提供
`evaluate_selection`，接口版本为 `s2-research-diagnostics/v1`。它不被选择器、
权重规划或交易路由调用，不含候选优化、参数搜索、最佳期限选择或自动准入功能。
每次调用必须声明 `train`、`validation` 或 `retrospective`。
本版本明确拒绝 `final`，即使标签已成熟且由调用方传入也不能在此产生合法 final 证据；
最终样本只能走既有正式单次裁决入口，避免新增第二条准入通道。
所有输出均为 `model_selection_allowed=false`、`research_effectiveness=not_adjudicated`。
本模块不打开任何文件、不读取现有前瞻观察窗。

每条 `ResearchObservation` 提供当时已知分数、eligible、真实目标权重、
预登记 `horizon_hours`、`mature_at`、`label_available_at` 和已扣费简单前瞻收益。
要求 `score_available_at <= as_of`、`mature_at >= as_of + horizon_hours`、
`label_available_at >= mature_at`。未到成熟/可得时间的标签值完全不参与检查和计算，
即使输入同时携带未来标签；可得的非法标签明确报错。上游仍须保证 net 标签真实计入
费用、滑点、退市结算等冻结成本，不能用选择器产生收益标签。

所有截面必须包含当期完整候选成员，包括缺分数和不合格成员；同一时点的不同持有期
必须具有相同成员、当时分数和目标权重。任一合格有分数成员尚未成熟或缺少已成熟标签时，
整个截面 IC/分位收益均待成熟或不足，避免只计算提前成熟/幸存成员。

- **Rank IC**：同一 `(as_of, horizon_hours)` 的分数和扣费前瞻收益分别按平均秩，
  再做 Pearson 相关，即 Spearman；默认至少 3 对观测。常数分数/收益、标签不足输出
  null + 状态 + 原因 + 样本量。
- **IC IR**：同一持有期多个有效日期 IC 的均值 / IC 样本标准差（`ddof=1`）；
  未年化，至少 2 个非恒定 IC。IR 单位明确为
  `unannualized_ic_mean_over_sample_std`，不把短样本缩放成年化显著性。
- **分位净收益**：分数从低到高的平均百分位秩乘预登记分组数，再向上取整。
  相同分数留在同组，不靠交易对字母排序捏造 Top/Bottom。组内按成员等权简单净收益；
  空组或成员少于分组数为不足。Top 减 Bottom 的单位是简单净收益差。
- **Coverage**：当期合格且有分数成员 / 输入完整当期成员；分母不因标签未成熟而缩小。
- **真实换手**：报告区间内唯一成交 ID 的 `sum(quantity * fill_price)`（买卖均计），
  除以同区间已观测权益的算术均值，未年化、不除以 2；费用另计为报价币实际费用。
  报告窗使用 UTC 闭区间，必须结束于诊断时点之前或当时。成交和权益须从权威事实导出，
  重复成交 ID 或重复权益时间明确拒绝；本模块不能独立认证调用方所称的“真实成交”。
- **目标变化**：从初始全现金起，累计所有标的相邻目标权重绝对差，现金权重不重复计入。
  多个 horizon 不重复计算同一目标；该字段不代替成交确认，也不等于漂移后实际换手。
- **Decay**：按全部预登记 horizon 列出均值 IC、IR、Top-Bottom 均值及各自有效样本量，
  不输出最佳期限。不同 horizon 的未成熟比例必须同时阅读，不能据此补选参数。

对应 [`tests/test_selection_research.py`](../tests/test_selection_research.py) 用合成固定输入
手算验证公式、同分分组、跨期限一致性、未来标签不变性、缺失截面、单位、真实成交字段
换手计算及 final 直接拒绝。其通过只证明诊断实现符合上述契约。

## 本地验证与兼容

新增模块为 opt-in，不修改原回测引擎、策略路由、现有 universe CSV 格式或已冻结配置。
源数据接口和提议接口分别标记 `s2-selection/v1`、`s2-rebalance/v1`。
专项测试覆盖手算排名、前缀不变、来源声明、缺数据、显式符号、缓冲、现金/费用/风险额度、
总敞口、参与率/换手共享预留、挂单与重放、退市/无量/精度及未对账拒绝。

验证命令：`.venv\Scripts\python.exe scripts/run_portable_tests.py tests/test_s2_selection.py tests/test_selection_research.py -q`。
