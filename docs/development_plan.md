# QuantTrading 开发计划 v3.0

> 公开发布视图：保留任务编号、状态、依赖及技术契约；私人邮件正文、逐条审查摘录和邮箱映射未发布。原始本地登记仍是权威资料。`reports/`、`outputs/`、`tmp/`、`docs/archive/` 及邮件审计文件均为仅本地引用，不随源码发布；公开 CI 的 structure-only 结果不代表这些历史证据已验证。

> 基线日期：2026-09-20。项目阶段以[统一Roadmap](unified_roadmap.md)为准；每个任务的可执行说明见[开发详情](development_details.md)。
> [结构化任务登记](development_task_registry.json) · [旧计划/邮件映射](roadmap_traceability.md) · 历史资料与邮件（仅本地引用：`docs/archive/2026-09-roadmap-rebaseline/README.md`，不随源码发布）

## 1. 本轮范围

原始基线共40个任务：20个邮件缺陷工作包（FIX）、18个历史能力补齐工作包（SYS）、1个验证任务（VER）、1个文档整合任务（DOC）。52条未闭环邮件意见进入20个FIX；1条证据不足意见进入VER；24条已修复意见单列回归登记，不重复开发。40是工作分解数量，不是完成率分母。

原始审计基线仅DOC-01已验收；本次实现后，FIX-01–20、VER-01、SYS-01、SYS-02已完成各自本地工程验收。其余系统按实际边界保留状态；整个R0–R8尚未全部验收，策略仍为paused_revalidation。见[本轮执行报告](r_series_acceptance_20260920.md)与逐项验收索引（仅本地引用：`reports/roadmap_v3/acceptance_index.json`，不随源码发布）。

第5、6节追加交付：九项冲突规则机器验收、准入门槛及前瞻协议修复、S2选币与研究诊断、S3目标仓位与分批恢复、保护单仓位身份迁移以及新候选独立登记。SYS-12/13更新为“部分实现待验收”；SYS-11继续“待证据”，SYS-14继续“后续扩展”。见[追加执行记录](section56_acceptance_20260920.md)。

第7节追加交付：优先级/依赖检查及七项完成定义已接入[机器验收](../scripts/verify_roadmap_completion.py)和[证据清单](roadmap_completion_manifest.json)，保留24项历史本地验收和16项开放任务。P0的SYS-05、SYS-08仍阻断相关运行；当前自身依赖已满足的开放队列按优先级为SYS-05、SYS-04、SYS-03。队列仅表示可推进自身验收。见[第7节记录](section7_acceptance_20260920.md)。


本次回顾追加 SYS-19 自动化工作包，当前登记共 **41 项：24 项历史已验收、17 项开放**。原始40项基线和历史验收不改写。账户来源/新增风险门禁、指标事实输入、自动化及文档修正见[本次完成记录](followup_completion_20260920.md)。工程执行人为 **Codex**，开始日期 **2026-09-20**；真实数据/账户操作及人工验收负责人尚待项目方指定。每个开放任务的 `execution_assignment` 记录实际完成边界；未来样本与连续观测不填造日历截止日。

## 2. 批次与交付顺序

| 批次 | 目标 | 工作包 | 本批边界 |
| --- | --- | --- | --- |
| B0 | 建立当前基线 | DOC-01；SYS-01 | 归档、冻结工作区、保存受控差异、三次复现。已知失败逐条挂到FIX，不把准备基线当成正式放行。 |
| B1 | 修正验收与研究门槛 | FIX-01–03 | 先堵住误放行，再校准统计支持、源码完整性与跨平台冻结。 |
| B2 | 数据与因果性 | FIX-04–07、FIX-09；SYS-04 | 锁定数据来源/PIT/已收盘边界，消除前视和重叠验证，验收数据身份。 |
| B3 | 交易、账务与风险 | FIX-10–16；VER-01 | 闭环成本、持仓回调、lot、借币、外部仓位、健康恢复和真实减仓；验证争议意见。 |
| B4 | 恢复与存量兼容 | FIX-17–18 | 原子检查点、并发导出、旧事件读取和幂等恢复。可与B2/B3无冲突部分并行。 |
| B5 | 集成契约与一致结果 | FIX-08、FIX-19–20；SYS-02/03/05–09 | 收敛权威事实、指标、研究入口、账户对账、共享持仓管理与预算，完成跨模块验收。 |
| B6 | 研究裁决、运维与自动化验收 | SYS-10/11/16/19 | 完成现有交付包和隔离回执，重新形成独立研究判断，完成备份恢复及故障演练。 |
| B7 | 连续运行 | SYS-17 | 前置验收通过后冻结版本；至少56连续自然日、两种市场状态及全链路对账。 |
| B8 | 受控真实资金 | SYS-18 | 证据齐全且取得人工批准后才启动小额灰度；每次只扩一个维度。 |
| BX | 独立扩展支线 | SYS-12–15 | 选币/目标权重、vol targeting、合约候选、解释展示。满足各自前置后独立安排，不替代主线缺陷修复。 |

批次是交付组织方式，不是强制所有任务串行。允许前置已满足的独立工作推进；跨批次验收依赖以任务登记表为准。本次工程执行人与开始时间已登记；真实运行负责人和投入尚未给出，外部证据阶段采用依赖与观测边界排期。已有前瞻观察协议的时间边界继续有效。

B0分两层：先建立可追溯的准备基线，记录当前已知失败；FIX-03完善严格冻结工具后，再按新工具完成批次验收。准备基线允许明确登记的已知缺陷，最终放行必须满足相应全部验收条件，避免“必须先修工具才能开始修工具”的循环。

## 3. 任务登记

表中每行链接到详情卡；负责人是职责角色，实际人选在开工时登记。依赖是验收依赖：可提前阅读、设计或制作夹具，但未满足依赖不能宣布任务关闭。

| ID | 任务 | 优先级 | 批次 | 当前状态 | 前置任务 | 负责角色 |
| --- | --- | --- | --- | --- | --- | --- |
| [DOC-01](development_details.md#doc-01) | 三层计划、历史文档与邮件联合归档 | P1 | B0 | 已验收 | 无 | 文档与工程治理 |
| [SYS-01](development_details.md#sys-01) | 当前工作区冻结与三次可复现基线 | P1 | B0 | 已验收 | [DOC-01](development_details.md#doc-01) | 工程验收 |
| [FIX-01](development_details.md#fix-01) | 准入检查必须拒绝不健康、不完整和不连续的证据 | P0 | B1 | 已验收 | [SYS-01](development_details.md#sys-01) | 准入与工程验收 |
| [FIX-02](development_details.md#fix-02) | 统计准入拒绝不足样本并统一 Sharpe 尺度 | P1 | B1 | 已验收 | [SYS-01](development_details.md#sys-01) | 准入与工程验收 |
| [FIX-03](development_details.md#fix-03) | 冻结验收包含未跟踪文件并修复跨平台归档验证 | P1 | B1 | 已验收 | [SYS-01](development_details.md#sys-01) | 准入与工程验收 |
| [FIX-04](development_details.md#fix-04) | 下载来源、分页长度与刷新失败贯穿矩阵入口 | P1 | B2 | 已验收 | [SYS-01](development_details.md#sys-01) | 数据与研究 |
| [FIX-05](development_details.md#fix-05) | 补全持仓量历史并排除未收盘日线 | P1 | B2 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-04](development_details.md#fix-04) | 数据与研究 |
| [FIX-06](development_details.md#fix-06) | 因子只使用当时可确认的数据 | P1 | B2 | 已验收 | [SYS-01](development_details.md#sys-01) | 数据与研究 |
| [FIX-07](development_details.md#fix-07) | 统一币对名称并在成员退出时结束持仓 | P1 | B2 | 已验收 | [SYS-01](development_details.md#sys-01) | 数据与研究 |
| [FIX-09](development_details.md#fix-09) | Walk-forward 按候选预热并禁止收益窗口重叠 | P1 | B2 | 已验收 | [SYS-01](development_details.md#sys-01) | 数据与研究 |
| [SYS-04](development_details.md#sys-04) | 数据身份、PIT与独立来源核验 | P1 | B2 | 部分实现待验收 | [SYS-01](development_details.md#sys-01)、[FIX-04](development_details.md#fix-04)、[FIX-05](development_details.md#fix-05)、[FIX-06](development_details.md#fix-06)、[FIX-07](development_details.md#fix-07) | 领域内核与报告 |
| [FIX-10](development_details.md#fix-10) | 现金、费用、滑点与无价市价单采用同一风险输入 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [FIX-11](development_details.md#fix-11) | 平仓回调按持仓聚合并使用所属标的索引 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [FIX-12](development_details.md#fix-12) | 报告按主 lot 事实保留分片、风险与成本口径 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-11](development_details.md#fix-11) | 交易与风险 |
| [FIX-13](development_details.md#fix-13) | 正式 Broker 在空仓时重置借币计息时钟 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [FIX-14](development_details.md#fix-14) | 外部同步持仓具有明确接管或退出政策 | P0 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [FIX-15](development_details.md#fix-15) | 策略通过试运行后重置亏损基线并同步恢复状态 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-11](development_details.md#fix-11) | 交易与风险 |
| [FIX-16](development_details.md#fix-16) | 风险减仓覆盖缺行情持仓并保留期末真实流动性 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [VER-01](development_details.md#ver-01) | 验证流动性不足后的部分减仓是否真正中断 | P1 | B3 | 已验收 | [SYS-01](development_details.md#sys-01) | 交易与风险 |
| [FIX-17](development_details.md#fix-17) | 熔断检查点原子保存并隔离并发导出临时文件 | P0 | B4 | 已验收 | [SYS-01](development_details.md#sys-01) | 运行与持久化 |
| [FIX-18](development_details.md#fix-18) | 存量事件解码、幂等类型与运维入口兼容 | P1 | B4 | 已验收 | [SYS-01](development_details.md#sys-01) | 运行与持久化 |
| [FIX-08](development_details.md#fix-08) | 统一研究入口、冻结实现与样本外政策 | P1 | B5 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-02](development_details.md#fix-02)、[FIX-03](development_details.md#fix-03)、[FIX-09](development_details.md#fix-09)、[FIX-19](development_details.md#fix-19) | 报告与研究 |
| [FIX-19](development_details.md#fix-19) | 常规报告、空结果与 Metrics 兼容共享稳定契约 | P1 | B5 | 已验收 | [SYS-01](development_details.md#sys-01) | 报告与研究 |
| [FIX-20](development_details.md#fix-20) | 统一分析图表、持有期诊断与入场漏斗口径 | P2 | B5 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-12](development_details.md#fix-12)、[FIX-19](development_details.md#fix-19) | 报告与研究 |
| [SYS-02](development_details.md#sys-02) | 权威交易事实、账本与报告统一 | P1 | B5 | 已验收 | [SYS-01](development_details.md#sys-01)、[FIX-10](development_details.md#fix-10)、[FIX-11](development_details.md#fix-11)、[FIX-12](development_details.md#fix-12)、[FIX-13](development_details.md#fix-13)、[FIX-16](development_details.md#fix-16) | 领域内核与报告 |
| [SYS-03](development_details.md#sys-03) | 指标契约与BM0–BM8标准结果验收 | P1 | B5 | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[FIX-02](development_details.md#fix-02)、[FIX-09](development_details.md#fix-09)、[FIX-19](development_details.md#fix-19)、[FIX-20](development_details.md#fix-20) | 领域内核与报告 |
| [SYS-05](development_details.md#sys-05) | 完整账户事实及周期/日终对账 | P0 | B5 | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[FIX-01](development_details.md#fix-01)、[FIX-14](development_details.md#fix-14)、[FIX-17](development_details.md#fix-17)、[FIX-18](development_details.md#fix-18) | 领域内核与报告 |
| [SYS-06](development_details.md#sys-06) | 模式一致性、共享事实与迁移验收 | P1 | B5 | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[SYS-05](development_details.md#sys-05)、[FIX-18](development_details.md#fix-18) | 领域内核与报告 |
| [SYS-07](development_details.md#sys-07) | 持仓身份与共享管理决策 | P1 | B5 | 部分实现待验收 | [SYS-06](development_details.md#sys-06)、[FIX-11](development_details.md#fix-11)、[FIX-14](development_details.md#fix-14)、[FIX-16](development_details.md#fix-16) | 领域内核与报告 |
| [SYS-08](development_details.md#sys-08) | 组合预算与幂等风险转移整体验收 | P0 | B5 | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[SYS-07](development_details.md#sys-07)、[FIX-10](development_details.md#fix-10)、[FIX-12](development_details.md#fix-12)、[FIX-16](development_details.md#fix-16)、[FIX-17](development_details.md#fix-17)、[VER-01](development_details.md#ver-01) | 领域内核与报告 |
| [SYS-09](development_details.md#sys-09) | 健康裁决证据与隔离影子观察 | P1 | B5 | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[SYS-07](development_details.md#sys-07)、[FIX-15](development_details.md#fix-15) | 领域内核与报告 |
| [SYS-10](development_details.md#sys-10) | 完成现有研究与元层交付回执 | P1 | B6 | 部分实现待验收 | [SYS-03](development_details.md#sys-03)、[SYS-04](development_details.md#sys-04)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[FIX-08](development_details.md#fix-08) | 研究与数据 |
| [SYS-11](development_details.md#sys-11) | 独立策略裁决与重新准入 | P1 | B6 | 待证据 | [SYS-04](development_details.md#sys-04)、[SYS-10](development_details.md#sys-10)、[FIX-01](development_details.md#fix-01)、[FIX-02](development_details.md#fix-02)、[FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09) | 研究与数据 |
| [SYS-16](development_details.md#sys-16) | 可运行运维、备份与故障演练 | P1 | B6 | 部分实现待验收 | [SYS-05](development_details.md#sys-05)、[SYS-06](development_details.md#sys-06)、[SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[SYS-09](development_details.md#sys-09)、[FIX-01](development_details.md#fix-01)、[FIX-17](development_details.md#fix-17)、[FIX-18](development_details.md#fix-18) | 运行与运维 |
| [SYS-17](development_details.md#sys-17) | 连续Shadow/Sandbox/Paper与执行校准 | P1 | B7 | 待证据 | [SYS-11](development_details.md#sys-11)、[SYS-16](development_details.md#sys-16) | 运行与运维 |
| [SYS-18](development_details.md#sys-18) | 小额灰度与单变量扩容 | P1 | B8 | 待证据 | [SYS-17](development_details.md#sys-17) | 运行与运维 |
| [SYS-12](development_details.md#sys-12) | 动态选币到目标权重执行 | P3 | BX | 部分实现待验收 | [SYS-04](development_details.md#sys-04)、[SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[FIX-08](development_details.md#fix-08)、[FIX-09](development_details.md#fix-09) | 研究与数据 |
| [SYS-13](development_details.md#sys-13) | 波动率目标、分批与组合仓位 | P3 | BX | 部分实现待验收 | [SYS-02](development_details.md#sys-02)、[SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[FIX-08](development_details.md#fix-08) | 领域内核与报告 |
| [SYS-14](development_details.md#sys-14) | 合约账户扩展与候选策略 | P3 | BX | 后续扩展 | [SYS-04](development_details.md#sys-04)、[SYS-05](development_details.md#sys-05)、[SYS-07](development_details.md#sys-07)、[SYS-08](development_details.md#sys-08)、[FIX-08](development_details.md#fix-08) | 领域内核与报告 |
| [SYS-15](development_details.md#sys-15) | 报告解释、图表与扩展观察能力 | P3 | BX | 后续扩展 | [SYS-03](development_details.md#sys-03) | 领域内核与报告 |

| [SYS-19](development_details.md#sys-19) | 自动化回测、研究编排与运行留存 | P2 | B6 | 部分实现待验收 | [SYS-01](development_details.md#sys-01)、[FIX-08](development_details.md#fix-08)、[FIX-19](development_details.md#fix-19) | Codex（工程）；项目运行负责人（待指定） |

## 4. 原始首批队列与并行边界

以下六步保留最初实施顺序；已完成的FIX不重新排期。当前后续队列、实际负责人和缺失输入见[后续实施记录第3节](followup_completion_20260920.md#3-剩余验收与执行顺序)，以任务登记的当前状态为准。

1. 先完成SYS-01：保留用户工作区，登记当前源码、配置与数据身份，输出已知失败清单和三次基线差异。
2. 基线固定后优先处理P0的FIX-01、FIX-14、FIX-17；同时可安排FIX-02/FIX-03及独立数据用例。三项P0分别负责准入、外部仓位、检查点，交叉文件仍须串行集成。
3. 数据线按FIX-04→FIX-05推进；FIX-06、FIX-09可独立；FIX-07涉及引擎，须与交易线协调。
4. 交易线先确定FIX-10/FIX-11/FIX-16的共享输入与事件边界，再合并FIX-12/13/15；VER-01先在B0原始快照复现，FIX-16完成后再跑集成反例。
5. B5先形成FIX-19稳定输出契约和SYS-02事实链，再完成其余指标/账户/共享运行任务；FIX-08依赖FIX-02/03/09/19，FIX-20依赖FIX-12/19。
6. B6分别交付研究证据和运行证据；研究失败仍可完成工程任务，但该候选不能进入真实资金放行。

| 共享范围 | 合并顺序/负责人协调 |
| --- | --- |
| 基线验收工具 | SYS-01先登记现状，FIX-03修严格验证，工程验收角色统一复核新身份 |
| 数据下载器 | FIX-04明确分页/来源/错误契约后合入FIX-05，SYS-04集中验收 |
| 回测引擎 | FIX-07、FIX-10、FIX-11、FIX-16、FIX-19使用独立变更集，按接口约定串行合并并复跑受影响用例 |
| live tick与状态恢复 | FIX-14、FIX-15、FIX-17串行集成；SYS-05/08验证恢复后的账户和风险状态 |
| lot、交易报告与指标 | FIX-11→FIX-12；FIX-19/20完成输出，SYS-02/03做事实与指标总验收 |
| 研究验证 | FIX-02/09先统一单位与样本边界，FIX-08统一入口，SYS-10/11完成交付与判断 |

## 5. 单任务开发流程

1. 读取详情卡和来源，记录原始源码身份；确认当前状态是否已被其他变更修复。
2. 对行为修复先做最小失败样本，固定输入、时间、初始账户、配置及期望事实；文档任务用结构和链接校验。
3. 实现最小闭环，补齐持久化、旧事件/报告兼容和失败原因；不顺带改变策略阈值或参数选择政策。
4. 执行与变更风险相称的正反例、故障和集成验证；不以全套测试数量代替目标契约。
5. 在详情卡指定目录保存验收结果、源码/配置/输入摘要和新旧差异。工程、研究、运行分别记录pass/fail/insufficient/pending。
6. 同步更新本计划、任务登记表、相关行为文档及邮件追溯结果。保留2026-09-20原始审计判断，新增复核时间和证据引用。

一个任务发现新问题时，只关闭已证明的边界，其余新增任务并记录来源与阻断关系。24条已修复邮件在相应路径改变时做针对性回归；无关任务不重复全量验证。

## 6. 批次退出与项目放行

- B1–B4：对应FIX正反例通过；兼容/恢复影响有证据；新旧差异可解释；不存在未登记的失败。
- B5：同一冻结输入下现金、仓位、成交、lot、成本、风险和报告一致；无法计算的结果为null并附明确状态；业务失败不能误标passed。
- B6：交付包可打开可追溯，completion各维度明确；统计样本不足或研究失败保持原判断；运维演练可按手册复做。
- B7：代码/配置身份固定，连续性、市场状态、逐笔和日终对账均通过；任何中断、重大修复或身份变化按冻结协议重新评估观察覆盖。
- B8：人工批准、权限、金额、急停和回滚边界齐全。计划文件本身不构成启动真实交易或解除策略锁的授权。

每项证据至少包括task_id、run_id、源码/工作区摘要、配置与数据身份、契约版本、逐项结论、限制与产物索引。详见开发详情的统一交付规范。
