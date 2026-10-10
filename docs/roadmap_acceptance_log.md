# Roadmap 验收记录(合并)

> 合并自 2026-09-20 的 R 系列、第 5/6 节、第 7 节与后续完成记录,另含 M-01–M-20 能力验收矩阵。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/r_series_acceptance_20260920.md -->
## R 系列执行与验收记录（2026-09-20）

> 本文保留前序 R 系列批次及 freeze04 的验收事实。后续第5、6节的新代码、保护单身份修复、新候选冻结和当前验证见[追加执行记录](roadmap_acceptance_log.md)；本文原源码摘要不代表追加修改后的工作区。

本记录对应 `unified_roadmap.md` 的 R0–R8，按 v3 开发计划执行。已完成缺陷修复、当前工作区冻结与本地集成验证；**整个 R0–R8 尚未全部验收**。系统能力的未闭环部分、独立研究和真实连续运行分别保留状态。原 2026-09-20 审计及旧研究结论保留，策略仍为 `paused_revalidation`。

### 本地工程交付

- FIX-01–FIX-20：逐项修复并保存契约、失败样本/新旧差异、源码摘要和正反例。验收索引见 [机器索引](../reports/roadmap_v3/acceptance_index.json)。
- VER-01：冻结原代码证明“普通 GTC 部分成交本身永久中断”不成立；取消残单和首次减仓拒绝的真实边界成立，已由 FIX-16 固定目标重试覆盖。
- SYS-01 / R0：修改前 ZIP、tracked patch 与清单保留；当前源码含未跟踪文件进入独立冻结，三进程执行无交易、手算、真实引擎合成输入，比较订单/成交/closed trades/权益/健康/配置事实。
- SYS-02：增量成交与订单变更游标、原子 lot / CloseEvent 检查点、策略关闭事件游标和跨标的待处理队列已实现并通过本地验收。首次迁移/启动加载兼容事实，普通更新不反复扫描全部历史。缺归属或费用的旧记录保留原始事实并标为 invalid_input，报告与研究准入均拒绝将其当作正常结果。
- SYS-03：补齐执行率/耗时/执行价偏差、UTC与严格年化尺度、月缺失记录、交易分布/胜率区间、时间加权暴露、换手、回撤Top-N/最长水下，以及三个明确身份的基准。标准报告统一保留 metrics、closed_trades、reconciliation、execution_quality 和 invalid_closed_trades 产物；详细口径见[指标契约](roadmap_acceptance_log.md)。整体未建模边界仍单列。
- SYS-05/06/07/08/09/16：真实 Router/RiskManager/策略经回测与离线 venue adapter 对比，spot/spot_margin 两币同 bar 的 Signal/Intent 全字段一致，事件编解码回放100次。六个订单持久边界分别中断并重启、各重复100次；UNKNOWN保留预算，不增加新请求。entry→棘轮→部分退出→flat→新epoch五阶段管理目标一致。三本SQLite库完成可执行备份、故意损坏、身份校验恢复和事实比较；本地预登记RTO 30秒/RPO零备份前已提交事实丢失达标。这些本地检查不替代整体验收依赖或实际账户运营。
- 历史基线 v2 保留，新 v3 另存。固定夹具成交价、数量和权益未变化；风险分母从成交后重算的 182.017331542 修正为原批准的 150.116507053，分片合计守恒。部分订单ID因模式统一变化，差异已另记。见 [风险差异证据](../reports/roadmap_v3/FIX-12/20260920-r-series/risk_migration_example.json)。
- 当前全套与质量检查结果见下方最终验证；开发过程中失败日志保留，不以最终成功覆盖原始失败记录。

### 最终验证

- 最终全套：**1,547 passed，1 skipped，46 subtests passed**，230.36秒；覆盖率 **89.02%**，超过既定55%门槛。见[完整日志](../reports/roadmap_v3/integration-release.log)及[JUnit结果](../reports/roadmap_v3/integration/20260920-r-series/release.xml)。跳过项为按显式开关启用的真实sandbox连接测试。
- 47条警告已保留，主要是pandas切片、测试SQLite连接释放和JUnit属性格式兼容提示；没有将警告过滤掉或把跳过项计作实测通过。
- 环境、依赖锁、Ruff、关键运行接口类型检查及受保护历史归档校验全部退出0。见[质量回执](../reports/roadmap_v3/integration/20260920-r-series/quality-release.json)。
- 最终源码/配置/固定输入快照在三个独立进程的 facts 和 resolved_config 字节一致，手算现金及lot净PnL对账通过。见[freeze04回执](../reports/roadmap_v3/SYS-01/20260920-r-series-freeze-04/acceptance.json)。
- 最终源码摘要：`7b2e6fbc4d72ee6db4ea5d33d316a787dcb0f9007f7bd1912455f2eedaec50b5`。完整[身份清单](../reports/roadmap_v3/SYS-01/20260920-r-series-freeze-04/source_manifest.json)包含受控未跟踪源码；相对修改前准备快照的103个受控文件变更见[变更清单](../reports/roadmap_v3/integration/20260920-r-series/implementation_manifest.json)。它不把本来就有的未提交修改重新算成此次开发。
- 全套通过后复核当前源码仍与freeze04一致。freeze01–03和开发阶段失败日志均保留；远程CI、干净提交验证及真实交易所运行没有被本地工作区回归冒充。

专项覆盖了源码污染、缓存错源/缺区间、未收盘数据、前视、样本不足、费用与预算、lot 分片、外部仓位、缺 bar、取消/unknown、事务失败、崩溃恢复、并发快照和旧事件类型迁移。告警和订单使用离线测试适配器；这些测试不能当作真实交易所连续运行或真实告警送达证明。

### 各阶段实际状态

| 阶段 | 本次已验证边界 | 仍需完成 |
| --- | --- | --- |
| R0 | 当前实际源码身份、三进程复现、归档跨平台字节校验、拒绝未登记输入 | 远程 CI / 干净提交验收独立记录，本地快照不冒充主分支提交 |
| R1 | 现金费用可支付、融资空窗、原批准风险、lot 分片、增量投影/游标与恢复、旧缺字段拒绝、严格指标 JSON | 真实账户资本/外部转账桥接依 SYS-05；SYS-03 的完整指标范围仍保留明确未建模项 |
| R2 | 图表窗口、共享分桶、持有期、入场因果漏斗、成本单调性、执行质量、标准报告对账文件及三种基准身份 | 缺分组持仓净值路径的回撤贡献、未采集盘口/机会成本继续 not_modeled；不以关闭交易PnL伪造组合回撤分摊 |
| R3 | train-only 选参、PF 样本与字段完整性、DSR 单位、预热与窗口隔离 | 新代码身份冻结后的独立研究；最终未见样本未成熟 |
| R4 | 外部仓位具名退出、unknown/部分成交/缺 bar 目标持续、批准风险恢复；规范快照漏单/重复/费用/入金/缺价/额外仓位注入拒绝，真实同步缺事实不覆盖旧状态 | SYS-05 的生产开账资本、资金流水、费用和全账户估值独立来源尚未接入，仍需六层逐笔/日终实际证据 |
| R5 | 真实模式同输入字段和Signal因果链、旧ID保留、百次幂等回放、六个持久边界、增量与共享管理五阶段 | SYS-05事实源依赖；SYS-07同向仓位在未观测flat时被外部整仓替换，保护止损epoch迁移尚未闭环 |
| R6 | 严格准入、原子检查点、并发导出、损坏库CLI恢复、身份冲突和替换失败反例、本地RTO/RPO与可执行手册 | 真实告警送达/回执、人工决策与实际部署/交易所恢复演练，以及前置账户/生命周期依赖 |
| R7 | 门控已禁止首尾两条观测冒充连续运行 | 前置 SYS-11/16 通过后，至少 56 连续自然日、两种市场状态与全链路对账 |
| R8 | 门控与研究锁保持有效 | R7 通过，明确账户/限额/权限/急停回滚证据及人工真实资金批准 |

SYS-12–SYS-15 是原计划的独立扩展支线，本次未将其冒充已验收。开发计划和登记表保留逐项状态。

### 既有研究交付与候选身份

重新核实 9 月 19 日的 15 基线、572 矩阵、39 验证共 626 次运行，其 summary 和 identity 齐全；baseline 441 个、revised 459 个冻结源码 hash 均匹配。未重跑或覆盖旧批次。

追加汇总已保存 1,404 行分层证据（P1 624、P2 780）。P1/P2 各有 88,940 条预测，并非没有有效预测；已更正该证据解释。P2摘要、集成P3摘要、正式账户开关前后的 digest/health/allocation 精确隔离回执现已恢复。使用原冻结源码、原预测和固定政策完成 primary 与 quarter_control 两项新增回放，每项三账户全部校验通过；没有模型拟合、参数搜索或重跑旧626次引擎。8个跨市场run的256产物和48输入也已核验。

SYS-10新交付包的工程及交付状态为pass，41项产物hash及分层8项输入hash通过复核；由于依赖和独立研究尚未通过，SYS-10总体仍不作为策略准入。见[新追加交付回执](../reports/roadmap_v3/SYS-10/20260920-delivery-recovery/acceptance.json)与[交付说明](../reports/roadmap_v3/SYS-10/20260920-delivery-recovery/README.md)。原校准CSV缺完整导出校验和、历史关闭cohort融资归属、静态历史币池/借贷资格/容量和独立源异常确认仍保留不足。

现有研究结论仍为 fail。旧协议观察窗为 [2026-10-20, 2027-04-18)，最早成熟日 2027-05-08。本次改变源码身份，已登记 [候选变更影响](../reports/roadmap_v3/SYS-11/20260920-r-series/candidate_impact.json)；原协议不修改、不提前打开，也不将其旧 hash 用作新代码身份。下一候选必须按原协议变更规则重新冻结。

### 兼容与恢复

- `metrics.json` 统一为 `quanttrading.metrics/v1`。`metrics` 保留旧 CamelCase 标量及 legacy 状态；`metric_results` 使用 v2 五态，`insufficient` 映射为 `insufficient_data`。非有限数值为 null，并在 `nonfinite_values` 保留路径和原因。
- 旧 `Metrics` 静态接口直接委托现有公式；旧缺理论价成交使用增量滑点口径，不能再扣一次既有滑点。
- `MetricsFormulaVersion=2.0` 明确UTC、等间隔或显式年化、缺月；动态再平衡基准按实际漂移权重计换手，并使用独立v2身份。模式统一的开仓sequence使部分后续订单ID变化，固定夹具的数量、成交价和权益不变；差异记录见[基线迁移](../reports/roadmap_v3/SYS-01/20260920-r-series-migration/baseline_diff.json)。
- 旧 Phase5 报告保留为已见历史，默认入口拒绝准入；显式 `--retrospective --output 新目录` 读取配置并产出无准入资格的诊断。
- 原工作区未提交修改全部保留。没有提交、推送、部署、真实下单或修改策略放行锁。


---

<!-- 合并自 docs/r_series_metrics_contract_20260920.md -->
## R 系列指标与产物契约（2026-09-20）

适用本次 SYS-03 / FIX-19–20 的当前代码，历史原始报告保持原有身份。本文件补充 [统一路线图](unified_roadmap.md)，不构成策略准入。

### 时间、状态和标准产物

`MetricsFormulaVersion=2.0`。权益输入必须为唯一、递增、无 NaT 的时间索引及有限数值；不再通过排序、去重或删除坏值修饰输入。naive 历史时间明确解释为 UTC，aware 时间转换为 UTC。只有严格等间隔才能自动推断年化尺度；缺 bar / 混频返回不可计算的 Sharpe，并说明需要显式 `periods_per_year`。调用者可在指标入口及 `ReportGenerator.generate()` 显式提供该尺度。

`MonthlyReturns` 保留每个自然月、观测数、完整覆盖标记及不可计算原因。缺月不会把相隔数月的端点伪装成一个月收益。已有月收益序列仍保留相邻有效月份的收益；首月没有上月端点时不产生虚构收益。

所有落盘报告 profile 均保留 `metrics.json`、`closed_trades.csv`、`reconciliation.json`、`execution_quality.json`。前者的 `metrics` 为兼容字段，`metric_results` 为五态适配；JSON 不允许 NaN/Infinity。`reconciliation.json` 验证 closed lot 分片 → position 往返 → 报告净 PnL；这不是全账户资本/外部转账桥接，后者缺输入时显式 `not_modeled`。

### 交易与执行统计

- 最大回撤事件表按回撤深度选 Top-5，并独立报告最长水下期、未恢复数。最长持续与最深回撤不是同一选取条件。
- 交易分布包括平均/极值/分位数/标准差、盈亏持有期、最长连续盈利/亏损及该段 PnL、Wilson 胜率区间；连续段使用提供的关闭事实顺序，同长取首段。百分比期望以原入场名义金额为分母，缺分母则 `not_modeled`。
- R 倍数仅使用原批准风险的分片份额；没有原批准风险时 `not_modeled`，不依据后来的权益补造。
- 执行质量按 `(account_id, client_order_id)` 聚合，成交按 `(account_id, fill_id)` 去重。冲突重复、超量成交、先成交后下单、订单累计成交大于成交事实总量均为 `invalid_input`，不会给出有效总体比率。
- 完全成交率 = 完全成交订单数 / 已观察订单数；部分成交率 = 期末成交量大于零但不足原请求量的订单数 / 已观察订单数。曾经部分成交但最终全成单独保留 `had_partial_fill`。拒绝、取消、过期按最后订单状态计数；unknown 单独可见，不视为拒绝或取消。
- 首次成交及完成耗时用 UTC 事件时间相对原 intent，单位秒。未完成订单不混入完成耗时；缺 intent 时间则明确无法建模。
- 执行价偏差（bps）= `sum(side_sign * (fill_price - reference_price) * qty) / sum(reference_price * qty) * 10000`；buy/cover 的 sign 为 +1，sell/short 为 -1。该字段仅覆盖已执行数量的价格差，不重复扣佣金；未成交机会成本使用下面独立的可选指标，缺参考价时为 `not_modeled`。费用及借币另按权威成交/融资事实报告。

### 本轮追加的可选事实输入

[ReportGenerator.generate()](../backtest/reporting/__init__.py) 已接入以下可选输入，并将结果写入标准报告；计算实现及正反例分别见 [attribution.py](../core/metrics/attribution.py)、[execution.py](../core/metrics/execution.py) 和 [test_metrics_fact_completion.py](../tests/test_metrics_fact_completion.py)。这些是事后诊断接口，不改变交易决策。

- `group_equity`、`group_cashflows`、`external_cashflows`：同一时钟、同一报告币种的完整分组估值及显式资金流。分组包括分配的现金、持仓和负债，必须覆盖全账户并与账户权益、外部资金流对账。当前只支持 `cashflow_timing="end_of_interval"`；各组贡献在同一个账户资金流中性回撤峰谷间链接，可加总为账户回撤。没有同步路径时不按已关闭 PnL 伪造贡献。
- `terminal_valuations` 与 `execution_as_of`：以 `(account_id, client_order_id)` 关联真实已终结订单的独立期末估值。估值须带价格、币对、报价币、实际时间、可得时间及来源记录，且在订单终态之后、报告截止前已可得。以剩余未成交数量计算相对原意图价格的有符号机会成本；订单未终结或覆盖不足不能产生完整总体值。
- `independent_quotes`：以 `(account_id, fill_id)` 关联独立 bid/ask 及来源、实际时间、可得时间。报价在成交时必须已可得，默认最大陈旧时间 `max_quote_age_seconds=1.0`。分别计算相对中间价和可执行侧报价的成交偏差；不含手续费，也不把观察到的价差解释为因果市场冲击。

上述来源字段及 `independent=True` 只是输入契约，不构成对调用方真实性的独立认证。未提供事实保留 `null + not_modeled`，覆盖不足或非法输入按对应状态拒绝有效总体值。真实分组路径、完整机会成本估值和独立报价采集尚未验收；实现和合成手算通过不代表这些真实事实已经取得。

### 基准身份与迁移

| benchmark_id | 政策 |
| --- | --- |
| `equal_weight_initial_close_buy_hold/v1` | 起点可见资产按首个 close 等权买入，不加入后来上市资产，不再平衡 |
| `equal_weight_event_rebalanced/v2` | 每事件按有实际 bar 的资产等权调整；先计算价格变动造成的实际权重漂移，再以含现金的单边换手计费 |
| `btc_eth_50_50_first_open_buy_hold_gross/2026-09-14` | BTC/ETH 各固定 50% 资金，在每个评价窗的首个可见日线 open 买入，不再平衡，上市前该袖套保留现金；无手续费的未融资参考 |

v2 动态基准修正了旧算法按“上次目标权重”计算换手而漏掉价格漂移的问题。手算：初始 1000、两资产 100/100、次日 110/90、费率 10bps；首次费用 1，次日漂移权重 55%/45%、单边换手 5%、费用 0.04995，期末 998.95005。冻结的 9/14 BTC/ETH 基准没有更换政策。

基准 metadata 写入曲线 attrs 并进入比较结果；无身份的外部/旧曲线明确 `identity_status=not_modeled`，不会自动认作某个冻结政策。报告时不得用新的通用基准重算并覆盖已封存研究结论。

### 验收边界

历史专项证据位于 `reports/roadmap_v3/SYS-03/20260920-r-series/`，后续事实输入与报告接线见[本轮完成记录](roadmap_acceptance_log.md)；旧回执的源码身份和当时未建模边界不改写。纯工程通过不代表 BM8 的独立研究成立。当前全账户真实资本桥接、缺原始输入的历史迁移、真实分组估值、独立报价/期末估值采集及未见研究仍分别待证据；因果市场冲击模型不由上述报价偏差指标替代。SYS-03 整体状态以任务登记表和逐项矩阵为准。


---

<!-- 合并自 docs/section56_acceptance_20260920.md -->
## Roadmap 第 5、6 节执行记录（2026-09-20）

本轮将旧计划冲突落实为可执行检查，并补齐选币与目标仓位的隔离工程能力。第 5 节使用九条统一规则逐条验收；第 6 节分别记录工程交付、研究证据和运行准入。**第 6 节整体尚未验收，正式策略仍为 `paused_revalidation`**。已知真实数据、账户及独立研究缺口不能用合成测试关闭。

### 第 5 节：统一口径

[规则契约](roadmap_policy_contract.md)登记 POL-01–POL-09 的原文档路径、旧编号、当前代码和具体回归节点。[验证入口](../scripts/verify_roadmap_policy.py)检查引用、复合追溯键和源文件摘要，并执行行为回归；只检查引用时明确记录 `references_verified_tests_not_run`。

本次发现并修复的行为缺口：

- 连续运行参数曾能把 56 天、两种市场状态下限调低。现在下限不可降低，更长冻结要求继续生效；直接提交的 paper 报告也必须有完整连续性和覆盖事实，不能仅凭 `passed=true` 放行。
- 前瞻样本开启曾直接信任 JSON 内的成熟日期。现在先校验原登记哈希、身份和时间边界；标签完成必须是布尔 `true`，字符串 `"false"` 不算有效证据。
- 活动领域文档中按最终 OOS 表现排序、较短 paper 观察等冲突文字已改为统一规则；受保护历史快照保持原样。

九条规则的机器回执见 [policy_report.json](../reports/roadmap_v3/POLICY/20260920-section56-release/policy_report.json)。工程检查不构成真实连续运行证据。

### 第 6 节：支线实际交付

| 支线 | 本轮可核验交付 | 仍未完成的边界 |
| --- | --- | --- |
| S0/S1、SR0–SR5 | 复核既有生命周期、增量事实、真实成本、train/validation 隔离和报告回归；原失败结论保留 | SYS-04 的真实历史成员/借贷资格/独立源异常确认；SYS-11 未见样本裁决 |
| S2 / SYS-12 | PIT 生效时间与可得时间、来源内容哈希、rank/zscore、TopN 缓冲、显式 symbols、目标权重、现金/费用/换手/参与率/待成交约束、持续退市退出提案；真实 Broker 部分成交对账 | 全历史退市数据真实性、因子 Top/Bottom 独立样本分离、正式风险路由接线和前置任务整体验收 |
| S3 / SYS-13、PM1–PM4 | 默认关闭的波动率定仓、集中度与相关簇约束、原批准风险限制、冻结目标、分批决策与检查点恢复；保护单绑定权威仓位身份，覆盖未观测 flat 的同向重开、取消后重启、UNKNOWN 与遗留单迁移 | 完整协方差风险贡献研究、目标仓位正式接线、真实账户生命周期和前置系统整体验收 |
| 信号 P0–P3 / SYS-10、11 | 原交付回执与正式账户隔离证据保留；新候选独立冻结，原协议哈希和研究失败不改写 | 样本支持与动态模型有效性；全弃权、无交易、等权回退或固定 25% 仓位均不能证明有效 |
| S4 / SYS-14 | 既有 spot/spot_margin/perpetual 账户、资金费率、保证金、强平和容量基础回归；逐项记录候选评估前置状态 | SYS-04/05/07/08 尚未整体通过，不启动具体合约候选评估；真实盘口/资金费率/借贷资格及实际运行证据不足 |

S2/S3 的具体接口、单位和迁移规则见[选币契约](s2_selection_contract.md)与[仓位能力契约](s3_position_capabilities.md)。它们默认不接正式路由，不改 `config/params.yaml`。SYS-12/13 从“后续扩展”更新为“部分实现待验收”，没有把独立纯决策模块标成完整生产能力。

PM1 的共享保护逻辑已接入现有回测和 live 调用链：从 open lot 核对实际数量并获取 position ID；新旧身份无交集时不继承旧棘轮；取消确认后重新核对仓位；UNKNOWN 不生成替代单。取消已确认、替换尚未提交时发生重启，从同仓位的持久订单事实恢复已确认止损，避免放松；待退出订单占用全部库存不再被当作实际 flat。缺失归属的旧保护单不能静默降低止损。这些是本地故障验收，未替代实际交易所运行。

### 可复现的隔离例子

从仓库根目录运行：

```powershell
.venv\Scripts\python.exe scripts/run_strategy_branch_example.py --output reports/roadmap_v3/section56/example-new.json
```

该入口构造明确标记为 synthetic 的 PIT 成员与因子，经过选择、波动率缩量、费用/现金/参与率约束，再交给已有真实回测 Broker。两根后续 bar 各成交 1 单位，尚未成交的目标保留；输出订单、成交、费用、现金、仓位和对账差异。输出文件拒绝覆盖。[本轮实例](../reports/roadmap_v3/section56/20260920-implementation/branch_example.json)只证明工程语义，不是收益或容量研究。

### 新候选与观察安排

原协议保持 **[2026-10-20, 2027-04-18)**、最早成熟日 **2027-05-08**，未开启也未修改。新修复身份单独保存源码快照、逐文件哈希、原配置与原实验登记引用；按同一 30 天隔离、180 天观察、20 天标签成熟规则从实际新登记时间重新安排，不继承旧候选已经经过的天数。

2026-09-20 登记的新身份观察窗为 **[2026-10-21, 2027-04-19)**，最早成熟日 **2027-05-09**。见[新登记回执](../reports/roadmap_v3/SYS-11/20260920-section56-successor-02/acceptance.json)和[源码清单](../reports/roadmap_v3/SYS-11/20260920-section56-successor-02/source_manifest.json)。登记仅建立可复现身份和未来观察边界；不会自动开启研究、解除策略锁或批准资金。

### 验证与证据

最终全套为 **1,717 passed、1 skipped、46 subtests passed**，47 条警告保留，覆盖率 **89.26%**，耗时 272.31 秒。跳过项是需显式开关和真实连接的 sandbox 测试。九规则专项 **70 passed**；环境、依赖锁、受保护历史归档、关键运行接口类型检查和 Ruff 均通过。源码身份为 `1174212be1fe952929bf83138c50285394a8213ca72b2c11b70f033da3774824`。

三进程的固定事实和解析配置逐字节一致；相对前序 freeze04，固定样本成交、现金/权益、lot、健康状态和配置相同，仅两条保护单意图增加仓位归属。首轮全套出现一个旧测试替身缺少历史订单接口的失败，补齐模拟订单事实后再跑全套通过；首轮冻结和失败日志保留。详见[基线差异](../reports/roadmap_v3/section56/20260920-implementation/baseline_migration.json)及[最终日志](../reports/roadmap_v3/section56/20260920-implementation/full-tests-final.log)。

本轮[总回执](../reports/roadmap_v3/section56/20260920-implementation/acceptance.json)记录源文件、配置、固定输入、专项与集成测试摘要、限制及产物索引；[全套回归](../reports/roadmap_v3/section56/20260920-implementation/full-tests-final.xml)、[质量检查](../reports/roadmap_v3/section56/20260920-implementation/quality.json)、[逐项验收索引](../reports/roadmap_v3/acceptance_index.json)共同用于复核。此前失败日志保留，不覆盖旧研究或旧源码冻结。

合约候选的具体阻断项见 [S4 readiness](../reports/roadmap_v3/SYS-14/20260920-section56/readiness.json)。市场数据真实性、56 天以上连续运行、远程 CI 与真实交易所/告警送达均不在本地合成验收中宣称通过。

交付校验另行核对40项任务在计划/登记/索引中的状态、所有索引回执哈希、本文档链的本地链接、374个源码文件与冻结副本、九规则证据摘要，以及新旧前瞻协议均未开启。见[交付校验](../reports/roadmap_v3/section56/20260920-implementation/artifact_validation.json)。


---

<!-- 合并自 docs/section7_acceptance_20260920.md -->
## Roadmap 第 7 节验收记录（2026-09-20）

本轮实现优先级与完成定义的机器校验。验收范围是任务治理及历史本地证据的显式适配；不改变策略、风险参数或交易运行代码。40项任务继续保留24项已验收、16项开放，正式策略仍为 `paused_revalidation`。

### 交付内容

- [优先级与依赖检查](../scripts/roadmap_priority.py)：拒绝未知编号、重复/未知依赖、依赖环和未满足前置的关闭状态；按依赖与优先级输出工作队列、开放任务及正式冻结/长跑阻断项。
- [完成定义检查](../scripts/verify_roadmap_completion.py)：核对计划、详情当前目录及任务卡、登记与验收索引；强制七项完成要求，验证证据路径、冻结源码成员及SHA-256，拒绝缺失、篡改、错误归属和内部失败却宣称通过的JSON/JUnit。
- [完成定义契约](roadmap_completion_contract.md)与[版本化清单](roadmap_completion_manifest.json)：为24项历史验收逐项绑定原始回执、契约、兼容规则、固定源码/输入、正反例与差异/集成证据、限制及开发流程。旧回执保持原样。
- CI增加结构检查。完整证据校验在保留历史报告的工作区运行；CI结构检查明确表示尚未检查历史产物，不伪装完整验收。

发现并同步了开发详情当前目录中SYS-12/13仍写为“后续扩展”的旧状态，使其与计划、登记及当前复核的“部分实现待验收”一致；原始审计卡片和历史快照继续保留。

实现复核进一步关闭了源码/输入角色替换、源文件变化、回执顶层通过覆盖内部失败、详情任务卡当前状态漂移等误通过路径。源码身份相关的4项最小失败样本保存在[source-binding-before.xml](../reports/roadmap_v3/section7/20260920-completion/source-binding-before.xml)及[原始日志](../reports/roadmap_v3/section7/20260920-completion/source-binding-before.log)，后续修复与集成结果另存，不覆盖失败证据。

### 当前阻断项

| 优先级 | 尚未关闭 |
| --- | --- |
| P0 | SYS-05、SYS-08 |
| P1 | SYS-03、SYS-04、SYS-06、SYS-07、SYS-09、SYS-10、SYS-11、SYS-16、SYS-17、SYS-18 |
| P2 | 无 |
| P3 | SYS-12、SYS-13、SYS-14、SYS-15 |

SYS-05、SYS-04、SYS-03的任务依赖已满足，可以继续完成各自验收；其自身证据缺口仍需关闭。SYS-08的未闭环链为SYS-05→SYS-06→SYS-07→SYS-08。其余开放任务的传递依赖见机器报告。

### 验证与产物

最终专项及相关规则回归 **248 passed，0 failed，0 skipped**，耗时5.58秒。其中本节优先级、完成定义及源码绑定测试216项，既有规则回归32项。Ruff、环境依赖、锁文件及受保护历史归档检查全部通过。保留本次380个受控源码/配置/夹具文件的快照，执行前后无漂移，逐文件快照摘要无差异。

本次源码身份为 `3f04768dfcfeb62728e65b36bb0510e21f8b3df4b4372247d7b62d80f94acebb`。这仅是本节验证运行的可复现身份，不将旧策略候选改称新身份，也不宣称重跑全部历史交易回归或远程CI。

验收产物：

- [完整完成定义报告](../reports/roadmap_v3/section7/20260920-completion/completion_report.json)：40项任务状态及依赖、24项历史验收证据、源码成员检查和仍然开放的任务。
- [验收总回执](../reports/roadmap_v3/section7/20260920-completion/acceptance.json)：契约、配置/输入、结果、差异、迁移、限制和产物摘要。
- [本次测试报告](../reports/roadmap_v3/section7/20260920-completion/tests.xml)与[日志](../reports/roadmap_v3/section7/20260920-completion/tests.log)：248项实际执行结果。
- [源码清单](../reports/roadmap_v3/section7/20260920-completion/source_manifest.json)与[执行前后身份校验](../reports/roadmap_v3/section7/20260920-completion/test_execution.json)：380个文件固定副本和零漂移。
- [质量与结构检查](../reports/roadmap_v3/section7/20260920-completion/quality.json)、[环境及归档检查](../reports/roadmap_v3/section7/20260920-completion/environment_checks.json)。

### 兼容与限制

原有任务数、优先级、依赖、验收状态及研究/运行结论不变。新清单为历史格式提供明确角色和引用，不改写原始源码冻结、原回执或旧研究fail结论。SYS-01/SYS-02的工程验收与运行pending可以同时成立；纯文档任务无需无关交易测试。

FIX-14/18/19/20的原始中间源码清单无法全部从现有冻结目录恢复，清单中明确改用后续freeze-04同版本源码、夹具和完整集成测试证明最终工程身份，并保留原始专项证据和这一限制。没有生成或猜测缺失的历史字节。

完整校验的pass只证明本节治理检查和被引用历史证据有效。`current_source_revalidated=false`、`live_admission=false`、研究和运行 `not_revalidated` 分别保留。没有取得新的未来独立样本、真实账户完整事实、56日连续运行或人工资金批准；本次治理交付不能消除这些阻断项。


---

<!-- 合并自 docs/followup_completion_20260920.md -->
## 2026-09-20 文档回顾后续实施记录

本记录承接“回顾今天新更新的文档，还有什么没有完成”及“完成以上任务”，覆盖实际可实施的账户/指标缺口、自动化方案和当前文档。原有40项工作包保留历史判断，新增自动化工作包 **SYS-19**；当前为 **41项、24项历史已验收、17项开放**。工程执行人是 **Codex**，开始日期为 **2026-09-20**。真实账户、独立数据提供和人工验收负责人尚未指定。

当前策略仍为 **paused_revalidation**。本记录不改变费率、风险门槛、冻结研究的 fail 结论或人工锁定。工程验证、策略有效性和实际运行分别留痕。

证据适用范围：下文测试、自动化和远端 CI 是 2026-09-20 对明确源码/配置身份取得的历史结论，其中 PR #39 的提交及合并身份列于第 2 节。2026-09-27 核对时，本检出分支 HEAD `ab15bc2` 早于该合并提交，工作区又有未提交变更，不能直接继承这些通过结果。`reports/roadmap_v3/…` 和 `outputs/automation/…` 链接指向本机保留且被 Git 忽略的原始回执；干净检出可阅读本页的证据摘要与公开 PR，但若要复核具体 JSON/JUnit，需取得当时封存的本地文件。

### 1. 本次工程范围

| 工作包 | 本次补齐 | 完整任务保留的边界 |
| --- | --- | --- |
| SYS-03 | 分组同步权益与资金流的回撤贡献；未成交机会成本；独立中间价与可执行报价偏差；标准报告/MetricResult 接线 | 真实分组估值/现金流和独立行情采集、来源核验、BM8未见样本仍需真实证据 |
| SYS-05 | 固定文件摘要的只读账户来源；开账资本/资金流/手续费/融资/全账户估值对账；周期报告、运行时新增风险门禁与CLI接线 | 真实账户原始导出、受信任来源校验、实际逐笔和完整日终证据仍缺；默认无可信校验时禁止新增风险 |
| SYS-06/07/08/16 | 账户差异与余额获取失败分别处理，保留保护性管理；新风险逐次检查完整账户来源及新鲜度 | 整体仍依赖SYS-05和真实恢复、告警及人工接管验收，不以局部测试关闭任务 |
| SYS-18 | 灰度入口重算原始Phase6证据，绑定外部文件摘要、实际源码/配置/账户/唯一策略/额度/有效期；逐次新增风险复核，拒绝旧汇总pass和14日证据 | 新增修复专项88项通过；真实批准、SYS-17前置及实际小额运行仍未取得 |
| SYS-19 | 自动化总入口、独占矩阵目录、指标/摘要裁决、完整重放、训练/验证研究与稳健性/容量任务、索引留存、本地失败告警；六项Windows调度已注册；真实双币四时间窗矩阵与重放、精确提交的两项远端CI均已通过 | 当前仅1个真实UTC刷新日；连续14自然日、独立PIT数据及外部告警回执仍需证据 |
| DOC-01后续同步 | README、回测假设、Router与策略模块文档；自动化草案改为实际接口说明；账户事实操作说明 | 工程负责人已填；真实数据与账户操作负责人的实际姓名待指定 |

指标的缺失事实仍产生 `null + not_modeled/insufficient_data`。分组回撤需要同步完整估值，现金流按区间末发生；组贡献在同一账户峰谷之间可加总。独立报价偏差仅是观察值，不代表因果市场冲击。账户导出只接受显式空仓开账锚点；第三币手续费的库存迁移和未支持的初始库存会明确拒绝。

### 2. 验证与证据

本次证据目录：`reports/roadmap_v3/followup/20260920-implementation/`，[最终验收汇总](../reports/roadmap_v3/followup/20260920-implementation/acceptance-final.json)索引当前源码身份、测试、质量检查、固定输入复现、调度和工作包回执。干净隔离提交的全量结果为 **2237 passed、46 subtests passed、1 skipped、0失败**，覆盖率 **89.40%**；唯一跳过项需要真实sandbox凭据，未当作通过。环境、依赖锁、Ruff、历史档案保护、任务结构、mypy及合成full报告/完整重放全部通过，见[质量回执](../reports/roadmap_v3/followup/20260920-implementation/final-quality.json)与[JUnit](../reports/roadmap_v3/followup/20260920-implementation/final-tests.xml)。

最终源码三进程复现通过。v4指标基线迁移保留旧v3原文件，17个叶路径变化均为新增指标、版本与缺事实原因文本；固定输入下交易、权益、基准和原六项指标一致，20条业务事件相同，见[迁移差异](../reports/roadmap_v3/followup/20260920-implementation/baseline-v4-diff.json)。

用户明确授权后，公开发布副本已作为干净提交 `a2581d1cc1fd799a36008a2deacbbd9101dda205` 推送并通过[PR #39](https://github.com/user-nmmmmm/QuantTradingV2/pull/39)合并到main，合并提交为 `aee11221cfa80e559858a2cea39d6966135a7d69`。395个源码与已测提交完全相同，另有27份净化文档；私人邮件、账户原始资料和本地证据未纳入。PR精确提交和main合并提交的 `quality` 与 `synthetic-smoke-and-regression` 两项远端CI均通过，见[远端CI回执](../reports/roadmap_v3/followup/20260920-implementation/remote-ci.json)及[main合并回执](../reports/roadmap_v3/followup/20260920-implementation/merge-main.json)。

历史验收契约的字节身份继续保留。SYS-01原验收引用的开发详情已逐字节归档到 [修改前开发详情](archive/2026-09-followup/development_details.before-followup.md)，其原摘要不变。当前详情允许加入新任务，历史回执仍绑定原版本。本次新增源码重新冻结，不把旧源码回执描述为当前代码验证。

最终候选登记为 [followup-successor-03](../reports/roadmap_v3/SYS-11/20260920-followup-successor-03/acceptance.json)，源码摘要为 `1ed2cf91117b5de1817fe88e5b46aa19cf2acb754a2ba75412c7c2aa63085211`；前两次候选及其失败/成功回执仍保留，不混用身份。参数文件原始摘要仍为 `49665830ead75ec74f6ef736f3f8b1ee7fdb3122ba9f334cda9d944b26decb4e`。灰度修复的新版证据格式与迁移方式见[Phase6操作说明](phase6_operations.md)。

[最终真实自动化批次](../reports/roadmap_v3/followup/20260920-implementation/final-automation-summary.json)已完成，395个受控文件的逐文件身份在命令前后、运行和研究快照中均与最终冻结版本一致：

| 验证 | 结果与实际边界 |
| --- | --- |
| 真实BTC/ETH日线刷新 | 通过；已收盘日线2020-01-01至2026-09-19，各2454根 |
| 四窗口矩阵及完整重放 | 四项工程裁决和四次重放全部通过 |
| 月度优化 / 季度稳健性与容量 | 工程执行通过；研究均为insufficient，每候选6个有效交易群组，低于30个要求 |
| 月度universe核对 | 缺独立上市/退市观测，明确insufficient_data并退出1；本地失败告警已生成 |
| 连续真实刷新 | 1/14个UTC自然日；不同源码/配置身份不合并计数 |

研究运行没有修改正式参数，也没有打开最终未见样本。六项Windows任务已注册且回读为Ready；实际外部告警送达和人工确认仍待取得。

旧9月基线的配对 `main_acceptance` 回放存在交易/权益/报告摘要不匹配，失败记录保存在 `outputs/followup_release/isolated_acceptance_summary.json`，未改写旧参考文件或将其标为通过。当前源码的三进程复现、v3→v4指标迁移和全量回归分别使用新的明确身份验收；它们不冒充该历史配对检查通过。

### 3. 剩余验收与执行顺序

| 顺序/任务 | 下一步的真实输入与完成边界 | 执行责任 |
| --- | --- | --- |
| 1 · SYS-05（P0） | 提供明确账户身份、空仓开账时点、完整资金/成交/费用/融资导出与带时间的估值；可信来源校验后完成现货和当前保证金账户逐笔/日终比较 | 项目账户负责人提供事实；Codex完成工程核验 |
| 1 · SYS-04（P1） | 补齐PIT历史成员、退市/借币可用量及费用的独立来源，核验来源与时间覆盖；旧无验证清单缓存不能升级为可信输入 | 项目数据负责人提供来源；Codex核验 |
| 2 · SYS-03（P1） | 提供真实分组权益/现金流与独立行情事实，补齐BM8独立样本及账户桥接 | 数据/账户负责人；Codex核验 |
| 2 · SYS-06/07/08/09（含P0 SYS-08） | 在已验证账户来源上完成共享持仓、风险动作、健康恢复与重启的端到端实况验证；关闭前置依赖 | 运行负责人；Codex复核事实与恢复 |
| 3 · SYS-10/11 | 保留历史研究fail；按最新独立候选身份积累未见样本并在成熟后单次裁决，不复用旧候选天数 | 研究负责人/人工裁决人；Codex执行冻结协议 |
| 3 · SYS-16 | 实际告警送达、人工接管、真实场地备份恢复、RTO/RPO与账户隔离证明 | 项目运行负责人 |
| 并行 · SYS-19 | 六项计划已部署、真实行情/周矩阵及精确提交远端CI已通过；继续积累至少14个同源码/配置真实连续自然日，补独立PIT数据与外部告警回执；合成冒烟不计天数 | Codex工程；项目主机/仓库负责人 |
| 4 · SYS-17 | SYS-11和SYS-16完成后，以冻结身份至少56个连续自然日、两种市场状态、逐笔100%与完整日终对账验收 | 项目运行负责人 |
| 5 · SYS-18 | SYS-17通过，取得针对具体版本/账户/金额/范围的人工批准，再执行小额灰度 | 账户授权人与运行负责人 |
| 支线 · SYS-12/13 | 除真实PIT、账户和独立研究外，仍有正式调度接线、目标到订单的持久映射、多资产协方差/风险贡献及联合压力模型的工程工作；前置整体任务尚未验收，扩展保持默认关闭 | 研究/领域负责人；Codex承担工程 |
| 支线 · SYS-14/15 | S4合约账户/候选策略，以及部分报告解释、HTF→LTF、session和确认bar过滤属于尚未实施的BX后续扩展；遵守依赖，不列作本轮已交付 | 研究/领域负责人 |

主线剩余验收依赖真实来源、人工身份和自然时间；BX还包含上述未实施工程，不把全部开放任务统称为“只等外部数据”。这些阶段不采用虚构起止日。最终followup-successor-03候选观察边界为 **[2026-10-21, 2027-04-19)**、最早成熟日 **2027-05-09**，不继承旧身份观察天数。自动化的14日观测与R7的56日准入是两项不同验收，不能互相替代。

### 4. 操作入口

- [自动化执行与留存方案](archive/2026-10-doc-consolidation/automated_backtest_plan.md)
- [账户事实与运行门禁](account_fact_operations.md)
- [开发计划](development_plan.md)、[开发详情](development_details.md)、[结构化任务登记](development_task_registry.json)
- [第7节完成定义](roadmap_completion_contract.md)、[历史验收证据清单](roadmap_completion_manifest.json)

禁止把文件内自填的 verified/passed 字段当作独立来源证明；禁止把离线合成报告、未来预排任务或代码中存在某个函数算作实际连续运行。真实资金运行仍服从既有批准和门禁。


---

<!-- 合并自 docs/missing_capabilities_acceptance.md -->
## M-01～M-20 能力验收矩阵

本矩阵以可运行代码和测试为准，替代 2026-08-12 清单中的历史“缺失”状态。

| ID | 当前实现与验收证据 |
| --- | --- |
| M-01 | `tests/test_backtest_regression.py` 固定 fixtures、结构化事实包及连续运行一致性。 |
| M-02 | `core/metric_result.py` 提供 `MetricResult`、明确状态和 JSON Schema，区分零值与不可计算。 |
| M-03 | `research/audit/ledger.py` 提供离线研究与对账的可重建 fill、费用、现金、仓位和已实现 PnL 投影；它不接管实盘或回测的账户持仓事实。 |
| M-04 | `core/broker/cost_model.py` 统一成本语义；缺失的资金/借券成本显式标为 `not_modeled`。 |
| M-05 | `PortfolioProjection.reconcile` 和现金充足性检查覆盖组合级对账。 |
| M-06 | `core/metrics/` 提供回撤事件、交易质量、暴露、信号漏斗和成本敏感性。 |
| M-07 | `core/metrics/` 提供归因、基准、R-Multiple、MAE/MFE 和 SQN。 |
| M-08 | `analysis/validation.py` 组合 OOS/walk-forward/Bootstrap/Monte Carlo/多重测试；`optimize.py --oos` 输出证据。 |
| M-09 | `core/exchange/__init__.py` 统一 markets、精度、步长、最小数量/名义金额。 |
| M-10 | `research/audit/reconciliation_job.py` 原子输出日终对账报告。 |
| M-11 | `core/events/` 定义共享事件、因果 ID、幂等消费和回放。 |
| M-12 | 原子状态、遥测、滞回告警、启动检查、心跳、对账、备份回滚和 Dashboard schema 均有模块与测试。 |
| M-13 | R7 故障注入、sandbox 凭据门控测试和连续运行证据审计已实现。 |
| M-14 | `core/gray_release.py` 与 `run_live.py --live` 强制 R7、单标的、小额上限、最小权限、人工批准和回滚。 |
| M-15 | TrendBreakout/TrendBreakdown 健康度通过 `bind_state_store` 持久化恢复。 |
| M-16 | 自动测试验证所有非 Cash 路由都有注册策略。 |
| M-17 | `Strategy.hard_stop_exit` 在策略特定退出前统一检查硬止损。 |
| M-18 | `strategies/volatility.py` 提供独立 VOLATILE 策略并接入路由。 |
| M-19 | `strategies/statistical_arbitrage.py` 提供跨标的配对信号。 |
| M-20 | `core/supervisor.py` 提供有限重启、指数退避及稳定期复位。 |

Supervisor 示例：`python -m core.supervisor --max-restarts 10 -- python run_live.py --sandbox --symbols BTC/USDT`。它只负责进程存活，不放宽任何交易权限或风险门禁。
