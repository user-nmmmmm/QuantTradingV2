# 第一阶段文献身份与适用范围核验（2026-10-27）

> **2026-10-28 导入边界**：以下是 `unpublished b82 worktree local evidence` 中保留的 2026-10-27 文献来源与实际阅读记录；本次只迁移引用路径，未重新阅读全文或取得 Excel 正文。它不代表当前 main 工程通过；当前提交检查见[主线验收回执](paper_research_main_acceptance_20261028.json)。
对应任务：PR25-00。依据：[专项 Roadmap](paper_research_roadmap_20261025.md)及[开发与测试计划](paper_research_development_plan_20261025.md)。机器回执：[JSON 映射](paper_research_literature_map_20261027.json)。日期采用 Asia/Singapore。

八篇计划来源均已定位并核验身份；六篇阅读到关键方法章节，两篇仅取得出版方摘要与元数据。本记录不声称完整论文复现、算法等价或策略增益。

## 状态与编号边界

- 工程：通过，八篇来源、版本、实际阅读层级、迁移差异及关联工作包均有记录。
- 研究：未评价；本任务没有新训练或收益实验。
- 外部证据：待补。已确认《量化交易论文分类与研究导航.xlsx》的文件身份（217,524 字节，修改时间 2026-10-25T09:46:07.250793Z），但正文读取没有可读内容；两次按规范取得副本均下载失败。因此表内编号、工作表、单元格以及是否收录这些论文均保持未知。
- L01–L08 是本专项编号。旧编号只依据[早期 32 篇来源记录](paper_applications.md)核对，不能冒认为已检查完整旧 106 篇表或新 Excel。
- 阶段 A 的合同允许“未读 Excel 明确待证据”；这项输入缺口不改写为完整表格映射通过。

## 来源索引

| 专项 ID | 文献 | 实际阅读 | 旧表编号（早期记录） | 新 Excel 编号 | 对应工作包 |
| --- | --- | --- | --- | --- | --- |
| L01 | [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) | 关键章节 | 18 | 待可读副本 | PR25-02、PR25-06、PR25-07 |
| L02 | [The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | 关键章节 | 19 | 待可读副本 | PR25-02、PR25-06、PR25-07 |
| L03 | [A Reality Check for Data Snooping](https://onlinelibrary.wiley.com/doi/10.1111/1468-0262.00152) | 摘要与元数据 | 20 | 待可读副本 | PR25-06、PR25-07、PR25-18 |
| L04 | [Optimal Execution of Portfolio Transactions](https://www.risk.net/journal-risk/2161150/optimal-execution-portfolio-transactions) | 摘要与元数据 | 17 | 待可读副本 | PR25-08、PR25-09、PR25-10 |
| L05 | [Honey, I Shrunk the Sample Covariance Matrix](https://econ-papers.upf.edu/papers/691.pdf) | 关键章节 | 未核验 | 待可读副本 | PR25-11、PR25-12 |
| L06 | [Common Risk Factors in Cryptocurrency](https://economics.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf) | 关键章节 | 8 | 待可读副本 | PR25-05、PR25-13 |
| L07 | [Multi-Period Trading via Convex Optimization](https://web.stanford.edu/~boyd/papers/cvx_portfolio.html) | 关键章节 | 未核验 | 待可读副本 | PR25-14、PR25-15 |
| L08 | [The Dataflow Model: A Practical Approach to Balancing Correctness, Latency, and Cost in Massive-Scale, Unbounded, Out-of-Order Data Processing](https://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf) | 关键章节 | 未核验 | 待可读副本 | PR25-03、PR25-04、PR25-05、PR25-17 |

## 逐篇核验

### L01 The Probability of Backtest Overfitting

作者：David H. Bailey；Jonathan M. Borwein；Marcos López de Prado；Qiji Jim Zhu。
版本：Author manuscript, February 27, 2015; revised February 2015。

阅读定位：§2.1, PDF pages 9–10；§2.2 / Algorithm 2.3, PDF pages 10–12。
方法与目标：固定同步 T×N 候选收益矩阵；CSCV 枚举互补分割，对样本内赢家的样本外相对排名取 logit，计算退化频率。

适用限制：所有候选逐期同步，绩效指标能在子样本上估计；失败候选不能删去或补零。 分割保留时间结构并不证明未来独立；局部候选族不等于完整历史搜索。
项目迁移：PR25-02 先登记候选与全部失败尝试；PR25-06/07 再验证同轴面板、并列排名与完整组合。 本项目保留诊断/准入边界；不将本次局部完整扩展为全局完整。

原始来源：[已读取版本](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。

### L02 The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality

作者：David H. Bailey；Marcos López de Prado。
版本：Author manuscript, first April 15, 2014; read version July 31, 2014。

阅读定位：Expected maximum Sharpe, equation (1), PDF page 7；The Deflated Sharpe Ratio, equation (2), PDF pages 8–9。
方法与目标：在 PSR 的样本长度、偏度和峰度修正基础上，用搜索族 Sharpe 分布及有效独立试验数调整比较阈值。

适用限制：有效独立试验数及未入选尝试的分布必须有依据。 增加 seed 或同日资产不等于增加独立市场时间；依赖与非平稳性仍须单独诊断。
项目迁移：PR25-02 保存有限搜索范围；PR25-06/07 验证每期/年化单位及有效试验数限制。 历史尝试不完整时继续 diagnostic；当前登记不能补造过去试验。

原始来源：[已读取版本](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)。

### L03 A Reality Check for Data Snooping

作者：Halbert White。
版本：Econometrica 68(5), September 2000, pages 1097–1126; publisher web record says first online December 10, 2003。 DOI：10.1111/1468-0262.00152。

阅读定位：Publisher abstract and issue/citation metadata。
方法与目标：摘要给出的检验目标：声明搜索中最优模型是否优于预先固定的基准。

适用限制：本轮未取得方法正文，关键公式、正则条件及重采样实现尚未从该全文核验。
项目迁移：PR25-06/07/18 核对完整候选族和固定基准；具体算法等价需方法全文与独立数学验证。 本项目既有 circular block bootstrap 是已命名的工程实现；未以本轮摘要阅读宣称等价复现原论文。

原始来源：[已读取版本](https://onlinelibrary.wiley.com/doi/10.1111/1468-0262.00152)。

### L04 Optimal Execution of Portfolio Transactions

作者：Robert Almgren；Neil Chriss。
版本：Journal of Risk publisher identity / DOI 10.21314/JOR.2001.041; manuscript/year variants must remain distinct。 DOI：10.21314/JOR.2001.041。

阅读定位：Publisher abstract。
方法与目标：摘要说明在永久/暂时冲击成本和波动风险间求执行前沿，可用二次效用或流动性调整 VaR 选择执行策略。

适用限制：本轮只取得出版方摘要，未重新核验正文的冲击参数、离散路径及公式。 原文风险与成本结构不能证明本项目特定场所的参数已经校准。
项目迁移：PR25-08/09/10 先建设真实执行资格、留后日成本预测和实施差额账户桥。 不在阶段 A 开发拆单器；需真实订单、报价、费用与时钟证据后再标定。

原始来源：[已读取版本](https://www.risk.net/journal-risk/2161150/optimal-execution-portfolio-transactions)。

### L05 Honey, I Shrunk the Sample Covariance Matrix

作者：Olivier Ledoit；Michael Wolf。
版本：Read UPF author manuscript, June 2003, 21 pages; journal publication Summer 2004, JPM 30(4), pages 110–119。 DOI：10.3905/jpm.2004.110。

阅读定位：§3, equation (2)；Appendix A, equation (3)；Appendix B, equation (5)；Appendix C。
方法与目标：Σ̂=δF+(1−δ)S；F 对角保留样本方差，非对角由平均相关及标准差构造；强度用估计误差项并截断至 [0,1]。

适用限制：作者稿附录使用时间 iid、有限四阶矩及固定 N 的 T 渐近。 附录 C 的加噪已实现收益是模拟预测，不能当作项目真实 alpha。
项目迁移：PR25-11/12 比较 sample、fixed_diag_020、lw_constant_correlation，并验证 1/T 与 ddof=1 转换及 γ=0。 常相关目标不同于 scaled identity；两资产目标可能等于 S。 日频/年化及期限单位只转换一次；时间依赖不由论文渐近自动解决。

原始来源：[已读取版本](https://econ-papers.upf.edu/papers/691.pdf)；[补充身份/版本入口 1](https://ledoit.net/honey.pdf)；[补充身份/版本入口 2](https://doi.org/10.3905/jpm.2004.110)。

### L06 Common Risk Factors in Cryptocurrency

作者：Yukun Liu；Aleh Tsyvinski；Xi Wu。
版本：Read Yale author manuscript, April 15, 2019, 47 pages; NBER WP 25882 May 2019; journal version 2022 is separate。 DOI：10.3386/w25882。

阅读定位：§2, PDF pages 5–6；§4, PDF pages 16–17；Table 11 / equation (4), PDF page 26。
方法与目标：以历史市值、周度市场收益及规模/动量分组构造 CMKT、CSMB、CMOM，再检验三因子暴露。

适用限制：2019 稿与 2022 期刊版的显著特征数量不同，不能拼接版本结论。 跨场所聚合、周度再平衡及长短组合与项目单场所日频和成本约束不同。
项目迁移：PR25-05/13 固定历史成员、市值定义、修订/可得时间和因子版本后归因。 成交额不能无声明替代市值；缺退市或历史市值时只报告局部代理。 因子暴露和残差不等于可执行的成本后因果 alpha。

原始来源：[已读取版本](https://economics.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf)；[补充身份/版本入口 1](https://www.nber.org/papers/w25882)；[补充身份/版本入口 2](https://doi.org/10.1111/jofi.13119)。

### L07 Multi-Period Trading via Convex Optimization

作者：Stephen Boyd；Enzo Busseti；Steven Diamond；Ronald N. Kahn；Kwangmoo Koh；Peter Nystrup；Jan Speth。
版本：Author page: August 2017, Foundations and Trends in Optimization 3(1), pages 1–76; PDF series header labels 2016 and copyright 2017。 DOI：10.1561/2400000023。

阅读定位：§4.1, equation (4.2), printed pages 24–25；§5.2, equations (5.1)–(5.2), printed pages 47–49。
方法与目标：在收益预测、风险、交易/持有成本和约束间优化；多期只执行首期目标，H=1 退化到相应单期近似。

适用限制：凸性取决于成本、风险与约束；多期权重传播采用小收益近似。 论文不提供收益预测；未来预测不能写成已知事实。
项目迁移：PR25-14 先做单期消融；PR25-15 只有衰减/重复换手瓶颈证据与预算齐全才启动。 计划路径不生成成交；实际现金、pending 预留及风险强制退出由既有内核负责。

原始来源：[已读取版本](https://web.stanford.edu/~boyd/papers/cvx_portfolio.html)；[补充身份/版本入口 1](https://web.stanford.edu/~boyd/papers/pdf/cvx_portfolio.pdf)。

### L08 The Dataflow Model: A Practical Approach to Balancing Correctness, Latency, and Cost in Massive-Scale, Unbounded, Out-of-Order Data Processing

作者：Tyler Akidau；Robert Bradshaw；Craig Chambers；Slava Chernyak；Rafael J. Fernández-Moctezuma；Reuven Lax；Sam McVeety；Daniel Mills；Frances Perry；Eric Schmidt；Sam Whittle。
版本：Proceedings of the VLDB Endowment 8(12), 2015, pages 1792–1803。

阅读定位：§1.3 time domains, printed pages 1794–1795；§2.3 Triggers & Incremental Processing, printed page 1797。
方法与目标：区分事件时间与处理时间，用窗口、触发器和增量修订表达晚到/撤回；水位线通常是启发式进度估计。

适用限制：处理时间和事件时间不同；水位线不能证明全部数据已到。
项目迁移：PR25-03/04/05/17 对齐 event_at、observed_at、available_at、成熟标签及历史修订。 available_at 与交易 knowledge 政策是本项目的显式领域契约；论文不是其可得性证明。 复用本地版本库与重放，不引入分布式平台。

原始来源：[已读取版本](https://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf)；[补充身份/版本入口 1](https://research.google/pubs/the-dataflow-model-a-practical-approach-to-balancing-correctness-latency-and-cost-in-massive-scale-unbounded-out-of-order-data-processing/)。

## 未满足项与后续输入

1. 取得同一 Excel 的可读授权副本后，读取全部工作表；保存原文件 SHA-256、版本、表内 ID、题名和确切单元格，再逐篇对照。不得凭旧编号或题名推测新编号。
2. L03、L04 后续若主张方法等价，须先取得对应版本方法正文、关键公式和假设，再由独立数学 fixture 验证。摘要级材料可以支持方法用途说明，不能支持完整算法复现声明。
3. L05 保留 June 2003 作者稿与 Summer 2004 期刊身份的区别；L06 保留 2019 作者稿、NBER 版本和 2022 期刊版的区别；L07 保留作者页 2017 与 PDF 系列头 2016 的差异。
4. 不调整既有风险门槛，不消耗最终样本，不生成前向天数或运行准入。
