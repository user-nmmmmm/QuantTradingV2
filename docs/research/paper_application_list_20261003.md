# 论文应用场景清单

最新补充：本文的 32 篇为早期处理子集，用户表格共 106 篇。用户后来选定的十项跨论文工作及最新工程实现，见[十项论文应用实现与验收](paper_roadmap_implementation_20261003.md)；下文历史基线不自动代表当前代码状态。

日期：2026年10月3日。适用项目：Still Water QuantTrading。配套文档：[论文应用路线图](paper_application_roadmap_20261003.md)。

本轮已实施近期场景的本地工程与首轮历史实验，详见[执行记录和结果](paper_application_execution_20261003.md)。下文“项目现状”记录调研启动时的基线；新增能力及仍未完成的依赖以执行记录为准。

对照32篇论文与当前代码、配置和实际研究产物，近期最有价值的方向是增强研究验证、状态条件信号、因子归因、交易标签、执行成本和funding接线。已有P0至P3、趋势组合、资金流函数和验证工具应优先复用。复杂配对、强化学习、订单簿做市、跨所和DEX需要各自的数据与执行前置，列为条件支线。

本清单给出可验证的应用假设及工程建议，不表示论文方法已在本项目证明有效。论文编号沿用用户提供的《量化交易与加密货币论文总结.xlsx》“论文汇总”A6:M37；Excel是文献索引和摘要资料，里面的阅读建议未作为额外操作指令。研究原始来源和阅读层级见文末覆盖表。

## 项目现状

1. **交易研究底座已经存在。** 当前具有多标的事件回测、次bar撮合、部分成交、共享参与率上限、费用/滑点/冲击、风险预算和实盘订单接口；验证包括冻结最终样本、Walk-Forward、purged CV、DSR、消融和实验登记。依据：[README](../../README.md)、[research_validation](../../analysis/research_validation.py)、[walk_forward](../../analysis/walk_forward.py)、[Broker成本模型](../../core/broker/cost_model.py)。
2. **当前没有已准入的正式策略。** 上涨状态路由TrendBreakout，其余状态默认Cash；趋势为paused_revalidation，做空和区间为paused_redesign，高波动为isolated_research。依据：[params.yaml](../../config/params.yaml)、[策略治理](../../core/strategy_governance.py)。
3. **已有元层的工程结果与有效性必须分开。** P1验证包有1,868条预测，allow 0、veto 43、abstain 1,825；P2的1,868条预测全部abstain，完整三轴模型0/66，归因权重全部等权回退。依据：[EV报告](../../reports/p23_signal_meta_20260918_verified/ev_report.md)、[P2报告](../../reports/p23_signal_meta_20260918_verified/p2_report.md)。这支持先检查样本和覆盖，尚不支持把P1/P2当成有效实盘模型。
4. **全市场研究的数据量充足不等于历史事实完整。** V3实际manifest中744个市场被发现、741个有K线、832,441根日线；上市与分类证据齐备仅61市场，内部缺失日3,532，融资证据覆盖0，historical_full_universe_verified为false。依据：[V3 manifest](../../reports/trend_portfolio_v3_20260921/market_data/manifest.json)、[数据覆盖审计](../../reports/trend_portfolio_v3_20260921/DATA_COVERAGE_ZH.md)。这些数值是已保存产物的覆盖口径，不是当前同时可交易资产数量。
5. **衍生品和微观数据存在明确接线缺口。** 已有funding/OI采集、资金流函数和融资账本，但默认CCXT数据入口未传market_type，默认spot OHLCV未自动合并funding，资金流函数未自动加入现有信号特征。WebSocket原型只有闭合K线缓冲，未接实盘引擎，没有交易所L2及排队模型。依据：[main.py](../../main.py)、[DataFetcher](../../core/data_fetcher.py)、[资金流因子](../../core/factors/capital_flow.py)、[融资账本](../../core/broker/financing.py)、[追加执行记录](../architecture_performance_followup_20261002.md)。

以上是当前工作树及已保存产物的读取结果，不代表本轮重新运行了策略测试或真实交易。最新追加文档记录的一次本地历史回测收益为-5.2891%、固定基准为+34.3140%，仅10笔闭合交易；该单次结果支持继续验证，不能用来预测未来收益。来源：[10月2日追加执行记录](../architecture_performance_followup_20261002.md)。

## 应用列表

优先顺序按当前依赖与最小实验成本安排。“近期”指已有数据或已有能力可开展的研究；“接线后”指须先完成输入、成本或执行契约；“条件支线”指近期主线完成后按证据单独立项。各场景的效益均为待检验的项目假设。

| 场景 | 应用及论文 | 可复用能力 | 必须补齐 | 最小交付与判断 |
| --- | --- | --- | --- | --- |
| S01 近期 统一论文实验记录 | #1综述、#2 Qlib、#3 FinRL-Meta。将因子、标签、训练、验证和组合实验挂在统一研究流程。 | ExperimentRegistry、manifest、真实Walk-Forward及报告。 | 完整搜索族、失败试验、可得时间、源码/配置/数据版本及同口径候选收益面板。 | 一项假设可复现；全部尝试可追溯；不需要替换现有平台。 |
| S02 近期 回测过拟合与选择偏差 | #18 PBO、#19 DSR、#20 Reality Check、#21验证方法比较、#22 RL过拟合。评价“择优之后”的证据。 | holdout、purge/embargo、DSR、BH FDR；专项趋势block bootstrap和搜索完整性判断。 | PBO/CSCV、CPCV及Reality Check尚未找到实现；DSR有效独立试验数口径仍简化；推广专项block bootstrap。 | 同时输出原始表现、搜索范围和校正后证据；不把普通purged CV称CPCV。 |
| S03 近期 状态条件动量和反转 | #10日内动量/反转、#24风险与收益。检验趋势、跳跃、流动性及波动条件下不同信号。 | 规则regime、趋势/区间策略、P0/P1/P2、P3影子账户。 | 扩大跨时间块样本，诊断未知轴和弃权；预登记小时线/日线及状态定义。 | 比较规则状态、P1、P2等权/动态的共同样本净优势和风险匹配组合。 |
| S04 近期 共同因子归因与动量组合 | #8共同风险因子、#24风险与收益。区分市场上涨、动量暴露和策略增量。 | 趋势V2/V3、PIT选择、逆波动/收缩协方差及基准报告。 | 历史成员与退市，历史市值、规模因子、因子收益和回归检验。 | 市场/动量归因、单因子消融、成本后超额；缺历史市值不能声称完整三因子复现。 |
| S05 近期 三重障碍标签和信号净值 | #15信息驱动采样及标签。比较止盈、止损、时间退出与现有固定持有期标签。 | P0候选和成熟标签、保护止损、Broker成本、purged CV。 | label_end_time、障碍首次触发、事件重叠隔离、同bar双边触发口径。 | 固定期限与三重障碍共同候选对照；bar近似与逐笔复现明确分开。 |
| S06 接线后 信息驱动采样 | #15。比较time/volume/dollar bars及CUSUM，研究活跃行情与低活动时间的采样差异。 | MarketDataAdapter、因子和事件处理接口。 | 连续逐笔成交、交易ID、数量/金额单位、接收时间和缺口审核；非固定频率时钟。 | 小范围采样消融；禁止由OHLCV伪造逐笔序列后声称原方法复现。 |
| S07 近期 执行成本与回测兑现差距 | #7 PRIME、#17 Almgren–Chriss、#23回测到真实市场。测量信号、资金约束和执行的各段损失。 | cost_model、参与率上限、部分成交、execution指标、订单延迟工具、P0 Actual/Ghost及P3。 | 真实报价/ACK/成交样本；状态与规模分层校准；同版本同时间窗对照。 | 滑点/价差/成交率/时延误差报告和implementation shortfall；需要时再做拆单基线。 |
| S08 接线后 Funding和基差研究 | #9 Crypto carry、#30永续定价。先保证融资现金流正确，再测试拥挤、carry及基差假设。 | funding/OI采集、CapitalFlowFactors、perpetual融资/保证金原语。 | 合约行情身份、mark/index、特征可得时间与结算时间分开；日内全部结算；双腿风险。 | 价格PnL/funding/借币/费用分解；结算手算与重放通过后做费率/OI消融或carry实验。 |
| S09 条件支线 协整与Copula配对 | #11 Copula、#12 RL配对、#13动态多配对。先做稳定价差基线，再研究非线性关系与规模控制。 | 独立PairsTradingModel、真实成本和风险原语。 | 协整及稳定性筛选、联合保证金、双腿intent、部分成交与失配腿恢复；RL另需环境。 | Z-score与Copula同窗对照，扣双腿成本；相关性不能代替协整。 |
| S10 接线后 成本约束组合分配 | #14 EIIE，结合#8因子研究。比较组合权重方法、现金配置与换手惩罚。 | allocator、组合目标、V2/V3、波动率目标、相关簇和风险预算。 | 统一候选输入、风险匹配基准、权重变化约束及交易费用；学习模型只在独立账户实验。 | 等权/逆波动/现有控制器/学习权重共同对照；原批准风险预算不被模型扩大。 |
| S11 近期 数据质量和成交量可信度 | #27 Crypto Wash Trading。防止成交量排名、量价因子与流动性估计被异常数据污染。 | 数据manifest、缺bar检测、成交额门槛、参与率限制。 | 先检查单位、异常量与跨源一致性；原论文首位数、尾分布和取整检验需要逐笔数据。 | 资产/场所数据质量报告；不能用一项OHLCV异常断言刷量，也不能等同真实可成交深度。 |
| S12 条件支线 微观结构与库存做市 | #26微观结构、#16 Avellaneda–Stoikov、#5 ABIDES、#6 JAX-LOB。研究流动性/毒性、成交概率和库存约束。 | 风险、订单生命周期、执行质量原语；低频量价可作初步诊断。 | L2快照/增量、序号恢复、tick、撤单、queue和延迟校准；做市仿真独立建设。 | 先验证特征及确定性执行/做市基线；不能用bar成交器裁决高频做市收益。 |
| S13 条件支线 有约束的强化学习 | #3、#4、#6、#12、#13、#14、#22。研究单一再平衡、时机或执行任务。 | 共享决策/执行接口、P3账户、账本和既有风控。 | Gym式环境、状态/动作/奖励、完整成本、多seed、候选族记录和盲评。 | 同数据、同成本、同风险下优于确定性基线；GPU并行仅在训练瓶颈被测量后考虑。 |
| S14 条件支线 跨交易所价差 | #25交易与套利。先监控可执行价差及成交地，随后才研究跨所双腿交易。 | 多venue抓取及跨市场验证；每个LiveBroker仍是单venue。 | 同时可得bid/ask、预置库存、资金/转账成本、联合风险和失配恢复。 | 全摩擦后的价差观察；CCXT支持多个交易所不等于套利基础已完整。 |
| S15 条件支线 链上和稳定币风险特征 | #28链上网络、#29 Tether历史资金流。作为市场结构、集中度和事件风险的增量输入。 | 现有因子/研究接口。 | 链上采集、实体标签历史版本、确认/发布时点、内部转账过滤和数据成本。 | 一个预登记特征的as-of消融；历史链上关系不能直接当今日多空信号。 |
| S16 条件支线 DEX和AMM收益归因 | #31 Flash Boys、#32 LVR。研究gas/MEV/排序成本及LP手续费与套利损失。 | 研究治理和报告思想可复用，执行链路需独立设计。 | 池状态、链上账户、LP会计、gas、失败交易、区块排序和MEV。 | LP的价格暴露、费用、LVR和gas分开；CEX回测收益不能代表链上净收益。 |

## 近期实验建议

### 状态条件实验

用相同原始候选和预先固定的时间窗，比较原始趋势、规则状态过滤、P1条件EV、P2等权及P2动态权重。先看成熟样本、有效时间块、预测覆盖和弃权原因，再比较保守净EV与有限资金组合。不要只展示模型能输出预测的少数样本，不将候选平均收益当成组合收益。

论文10研究日内动量和反转随跳跃、流动性等条件变化，论文24报告其历史样本中的时间序列动量和关注度关系。因此把状态条件作为研究维度是合理工程推断；两篇论文不证明本项目的Wasserstein状态、动态轴权重或当前参数有效。[日内研究摘要](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253)、[风险与收益摘要](https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024)。

### 因子归因实验

先在有证据的资产子集上构建市场和动量对照，检查已有趋势和选币收益是否只来自共同暴露。论文8研究市场、规模和动量三因子，阅读到的开放作者稿是2019年版本，不等同于2022年期刊全文。市值和历史成分缺失时，只交付部分复现；不能用成交额作为未注明的规模替代。[作者稿](https://economics.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf)。

### 标签实验

论文15同时研究逐笔采样、CUSUM和三重障碍标签；本项目可先拆出标签部分，用已有bar级候选做保守近似。为每个标签保存信号时点、可执行起点、最早障碍触发、成熟时间、成本和质量状态，purge覆盖完整标签区间。OHLC同时穿越上下障碍时，不假定有利顺序。[论文全文](https://link.springer.com/article/10.1186/s40854-025-00866-w)。

### 执行和融资实验

论文23的三个评估阶段是历史回测、前瞻交易所Paper与真实交易；对本项目最有用的是固定候选后的同市场时间段比较。其短期实盘实验不能推出所有AI方法长期无效，也不能把不同时段的表现差全部归因于滑点。[论文方法和结果](https://arxiv.org/html/2609.34510v1)。

论文30用于核对永续价格变化和funding现金流关系；论文9的主要对象是到期期货基差。两者应分别建模，永续正费率不是承诺的套利收益。实际双腿carry还涉及保证金、费率变化、强平和资金约束。[永续定价全文](https://arxiv.org/html/2310.11771v2)、[BIS Crypto carry](https://www.bis.org/publications/working-paper-1087-crypto-carry)。

## 原文限制与项目判断

| 论文 | 核对到的限制 | 对项目的处理 |
| --- | --- | --- |
| #7 PRIME | 原文为定性proof of concept，精确经验调参留待后续。 | 用于提出冲击和回落情景；不可复制参数后宣称本交易所校准完成。 |
| #12 RL配对 | 实验是BTC-EUR与BTC-GBP；摘要年化措辞与正文2023年12月单期收益口径有差异。 | 不把报告收益直接写成年化，也不当作BTC/ETH不同币配对的结论。 |
| #13 动态多配对 | 2026年预印本已核实存在；主要Sharpe/Sortino优势约p=0.06，95%区间跨0；费用和强平有简化。 | 只作探索性方法参考，模型保留确定性风险屏障，不用于本项目收益承诺。 |
| #14 EIIE | 原文假设零滑点、零市场冲击。 | 复现必须保留本项目真实成本、容量和next-bar执行。 |
| #15 标签与采样 | 数据为2018年至2023年的BTC/ETH逐笔；交易费用设置不等于完整的借币/滑点/容量模型。 | 区分bar近似与逐笔复现，保持自己的成本和验证约束。 |
| #18 PBO与#19 DSR | PBO需要完整候选表现矩阵；DSR的试验数涉及独立性，重复相关配置会改变口径。 | 全量记录搜索历史，报告有效试验数敏感度；不是只对最终赢家算一次指标。 |
| #21 验证比较 | 比较结论来自其合成受控环境及特定真实数据实验。 | CPCV用于附加研究诊断，不能因此删除按时间顺序的最终未见样本。 |
| #22 RL过拟合 | 测试期短，明确忽略滑点；文中的PBO不是普通收益显著性p值。 | 借鉴全候选选择偏差治理；不照搬其10%阈值或忽略项目成本。 |
| #26 微观结构 | 本次阅读开放2024年作者稿，非2026年期刊终版；目标主要是价格动态分布变化。 | 波动/流动性预测与可盈利方向信号分开验收。 |
| #27 刷量 | 原文针对29家历史交易所，识别方法依赖逐笔统计。 | 不将历史平均刷量比例或单一数字检验推广成当前交易所的事实。 |
| #28和#29 链上研究 | 依赖实体映射和历史样本，#29主要针对2017年。 | 保留标签版本与available_at，不据此断言当前USDT或当日市场状况。 |
| #31和#32 DEX/AMM | 链上排序、gas、MEV、LP价格暴露和LVR属于独立机制。 | 单独研究和会计，不能使用普通CEX成交模型直接兑现收益。 |

上述事实对应下方原始来源；应用和优先级是本项目工程推断。

## 32篇论文覆盖与来源

“关键章节”表示打开可访问的原论文全文，阅读与项目有关的方法、假设或结果段落，不表示逐页通读。“摘要/部分内容”表示本次只取得作者或出版方的摘要、研究说明或可见章节，没有据此声称全文复现。无法访问的终版以已读版本明确标注；全部32篇均已完成对应场景判断。

| 编号 | 论文或简称 | 本次阅读层级 | 主要应用 | 原始来源及版本说明 |
| --- | --- | --- | --- | --- |
| 1 | Cryptocurrency trading comprehensive survey | 开放全文关键章节 | S01、S03 | [Financial Innovation 2022](https://link.springer.com/article/10.1186/s40854-021-00321-6)，综述用于研究地图。 |
| 2 | Qlib | 全文关键章节 | S01 | [arXiv 2020 v1](https://arxiv.org/html/2009.11189v1)，工作流、因子表达式及缓存。 |
| 3 | FinRL-Meta | 摘要 | S01、S13 | [arXiv](https://arxiv.org/abs/2211.03107)。 |
| 4 | FinRL-Podracer | 摘要 | S13 | [arXiv](https://arxiv.org/abs/2111.05188)，股票训练架构。 |
| 5 | ABIDES | 摘要 | S12 | [arXiv](https://arxiv.org/abs/1904.12066)，多代理延迟仿真。 |
| 6 | JAX-LOB | 摘要 | S12、S13 | [arXiv](https://arxiv.org/abs/2308.13289)，GPU订单簿仿真。 |
| 7 | PRIME | 全文关键章节 | S07、S12 | [arXiv v1](https://arxiv.org/html/2305.07559v1)。 |
| 8 | Common Risk Factors in Cryptocurrency | 开放作者稿关键章节 | S04 | [Yale 2019稿](https://economics.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf)，期刊版为2022。 |
| 9 | Crypto carry | BIS摘要及研究说明 | S08 | [BIS WP1087](https://www.bis.org/publications/working-paper-1087-crypto-carry)，PDF入口重定向，未将其记为全文通读。 |
| 10 | Intraday momentum reversal | 作者摘要及出版方可见内容 | S03 | [SSRN作者条目](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253)、[期刊内容](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833)。 |
| 11 | Copula cointegrated pairs | 全文关键章节 | S09 | [arXiv v2](https://arxiv.org/html/2305.06961v2)，formation/trading划分和成本结果。 |
| 12 | RL Pair Trading Dynamic Scaling | 全文关键章节 | S09、S13 | [arXiv v2](https://arxiv.org/html/2407.16103v2)。 |
| 13 | Dynamic Multi-Pair RL | 全文关键章节 | S09、S13 | [arXiv 2026 v2](https://arxiv.org/html/2606.04574v2)，已核实身份。 |
| 14 | Deep RL portfolio EIIE | 全文假设章节及摘要 | S10、S13 | [arXiv v2](https://arxiv.org/html/1706.10059v2)。 |
| 15 | Information bars triple barrier deep learning | 开放全文关键章节 | S05、S06 | [Financial Innovation 2025](https://link.springer.com/article/10.1186/s40854-025-00866-w)。 |
| 16 | Avellaneda Stoikov | 全文关键章节 | S12 | [NYU作者PDF](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)。 |
| 17 | Almgren Chriss | 原始稿关键章节及期刊身份 | S07 | [期刊DOI](https://doi.org/10.21314/JOR.2001.041)、[原始稿PDF镜像](https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf)；作者站旧链接未取得。 |
| 18 | Probability of backtest overfitting | 作者稿关键章节 | S02 | [2015修订作者PDF](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)，期刊版2017。 |
| 19 | Deflated Sharpe Ratio | 作者稿关键章节 | S02 | [2014作者PDF](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)。 |
| 20 | Reality Check for Data Snooping | 出版方摘要 | S02 | [Wiley摘要](https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152)，未取得开放全文。 |
| 21 | Backtest overfitting in ML era | 出版方摘要及可见方法内容 | S02 | [Knowledge-Based Systems](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)，未取得完整终版。 |
| 22 | RL practical backtest overfitting | 全文方法及假设章节 | S02、S13 | [arXiv v6](https://arxiv.org/html/2209.05559v6)。 |
| 23 | Can AI Make Money in Crypto | 全文方法及结果章节 | S07 | [2026-09-28 v1](https://arxiv.org/html/2609.34510v1)，近期预印本。 |
| 24 | Risks and Returns of Cryptocurrency | 出版方摘要与作者目录 | S03、S04 | [RFS摘要](https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024)，未取得全文。 |
| 25 | Trading and arbitrage in crypto markets | 摘要及出版方可见内容 | S14 | [MIT作者条目](https://dspace.mit.edu/entities/publication/7f91bfb5-ba77-4d0e-9c79-ec75e104e6cc)、[出版方](https://www.sciencedirect.com/science/article/pii/S0304405X19301746)。 |
| 26 | Microstructure and market dynamics | 2024作者稿关键章节 | S12 | [Cornell作者PDF](https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf)，非2026期刊终版。 |
| 27 | Crypto Wash Trading | 2021稿关键章节及摘要 | S11 | [arXiv原稿PDF](https://arxiv.org/pdf/2108.10984)，期刊版2023。 |
| 28 | Blockchain Analysis of Bitcoin Market | 作者/机构摘要 | S15 | [NBER](https://www.nber.org/papers/w29396)、[MIT研究摘要](https://mitsloan.mit.edu/cfi/blockchain-analysis-bitcoin-market)。 |
| 29 | Is Bitcoin Really Untethered | 出版方摘要 | S15 | [Journal of Finance](https://onlinelibrary.wiley.com/doi/10.1111/jofi.12903)。 |
| 30 | Perpetual Futures Pricing | 全文关键章节 | S08 | [arXiv v2](https://arxiv.org/html/2310.11771v2)。 |
| 31 | Flash Boys 2.0 | 摘要 | S16 | [arXiv](https://arxiv.org/abs/1904.05234)。 |
| 32 | Automated Market Making and LVR | 摘要与版本记录 | S16 | [arXiv](https://arxiv.org/abs/2208.06046)，初稿2022，最新v6为2026修订。 |

## 优先投入建议

第一批选择S01、S02、S03、S04、S05、S07，并同时定义S08数据契约；先完成完整搜索记录、可信资产子集、样本覆盖和执行测量。S08接线通过后，才研究funding/OI新增因子或carry。S10优先比较已有权重基线，学习模型按独立研究处理。

S06、S09、S12至S16不全部并行开展。主线得到结果后，选择一条有明确数据来源和可证伪假设的支线。近期最重要的交付是明确哪些方法值得继续，以及哪些在真实成本、数据和样本约束下应当淘汰。
