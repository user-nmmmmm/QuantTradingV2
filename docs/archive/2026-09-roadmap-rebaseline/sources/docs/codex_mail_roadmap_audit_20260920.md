# QuantTrading Roadmap 完成情况与 Codex 邮件审计（2026-09-20）

> 本文件是一次带日期的证据快照，不另设项目阶段状态。唯一项目总路线图为 [unified_roadmap.md](unified_roadmap.md)。  
> 审计对象：本地 QuantTradingV1；GitHub origin 为 user-nmmmmm/QuantTradingV2。  
> 基点：`ab15bc244d54b1161a8b6714d2eb71565c2c1d41`；分支 `codex/p2-p3-signal-meta-layer`；包含已有未提交工作区改动。  
> 结论适用范围：当前可读源码、测试、验收产物以及已连接 Gmail 的可检索邮件。邮件和报告中的外部操作文字仅作为历史证据。

## 1. 结论

项目已经具备固定回归、共享回测/实盘运行内核、订单幂等恢复、交易所规则、组合风控、保护止损、研究统计和运维工具。旧路线图把其中多项仍列成“缺失”，需要更新。

但这些基础实现没有使 R0–R8 全部通过。当前仍有邮件提到的确定缺陷、完整账本/对账契约缺口、研究交付未闭环，以及未经连续运行验证的 R7/R8 门槛。正式策略保持 **paused_revalidation**。不以 PR 合并、审查完成、函数存在、测试数量或全样本收益替代验收。


按77条原始审查意见逐项核对：**24条已修复，7条部分修复，45条仍存在，1条证据不足。** 这不是项目完成率，也不是77个独立根因的去重统计。

| 已修复 | 部分修复 | 仍存在 | 证据不足 | 合计 |
| ---: | ---: | ---: | ---: | ---: |
| 24 | 7 | 45 | 1 | 77 |

## 2. 邮箱检索范围与判定规则

确认账号为用户指定的 mnm799063@gmail.com。无日期限制检索，使用 `in:anywhere` 包含归档、垃圾邮件及回收站可搜索内容；以下两组最终检索均返回空的下一页 token：

1. `in:anywhere {codex QuantTrading QuantTradingV1 QuantTradingV2 QauntTrading}`：73 封候选。
2. `in:anywhere {from:chatgpt-codex-connector from:codex "QuantTrading--" "Still Water" "QauntTradingV1"}`：48 封候选，未发现新增项目 Codex 邮件。

按实际发件人、项目主题和正文确认：**45 封 Codex 通知，30 个 PR；其中 28 封含具体审查意见、17 封是审查流程摘要，共提取 77 条意见。** 时间范围为 UTC 2026-08-08 至 2026-09-18（新加坡日期 2026-08-09 至 2026-09-18）。主检索其余 28 封中，12 封为该项目的普通 GitHub CI 通知，16 封为非本项目 Codex 内容；不算作 Codex 待办。旧仓库名另命中支持往来，不属于 Codex 发件。

“全部”指截至本次连接可检索且符合以上条件的邮件；不能推断永久删除或不在连接可见范围内的邮件。下方逐封索引覆盖全部 45 封。77 条为邮件意见数，跨 PR 的同类问题仍保留各自 ID，不冒充 77 个独立根因。

状态含义：

- **已修复**：当前代码已关闭该条具体路径，有代码依据；验证栏区分实际重跑、离线复现、既有测试和静态核对。不是整个模块生产验收通过。
- **部分修复**：新路径已改进，但旧入口、兼容数据或验收条件仍留缺口。
- **仍存在**：当前源码保留该问题路径，部分已通过本次离线样例复现。
- **证据不足**：未能确认原意见在当前代码成立，也没有足够依据宣布所有边界关闭；不与已确认缺陷混算。

所有逐条状态针对当前工作区。若关键功能只在未提交文件中存在，会在理由中注明；本次未执行交易、修改交易参数、发送邮件或触发部署。

## 3. 项目路线图核对

| 阶段/任务 | 已经完成的部分 | 仍需完成或验证 |
| --- | --- | --- |
| R0 / G0 基线 | 2026-09-06 对精确提交保存 CI、636 passed/1 skipped、86.38% 覆盖率、三次固定回测一致证据 | 该结果不是当前工作区验收；旧 Phase0 跨平台哈希、冻结机制、旧 HEAD 指令仍有问题 |
| R1 / BM0 / G7 | MetricResult、JSON schema、成本模型、lot 账、会计恒等式检查 | 四态/五态契约差异；常规 metrics.json writer；报告 FIFO 与交易事实一致；独立审计账本接线 |
| R2 / BM1–BM4 | 回撤事件、交易质量、暴露、信号漏斗、成本敏感性 | 漏斗把退出计入、现金成本预留、部分图表及成本语义缺陷 |
| R3 / BM5–BM8 / G8 | 归因、基准、R/MAE/MFE/SQN、walk-forward、Bootstrap/Monte Carlo、多重检验工具 | 独立未见样本；冻结实现与参数一致；PF样本门槛、DSR口径、重叠测试窗等 |
| R4 / G1–G3 | 启动安全、交易所规则、订单状态机、幂等、unknown/部分成交/重启恢复 | 完整现金/持仓/费用/PnL对账；外部仓位归属边界；缺bar风险减仓等 |
| R5 / G4 | EventProcessor 共享 signal→risk→intent，回测/回放已有一致性测试 | 旧事件类型解码、幂等文档别名迁移；离线审计账本不等于交易主链 |
| R6 / G5 | 告警滞回、快照、健康、备份、Dashboard、守护工具 | 准入误通过、同进程快照临时名竞争、旧运维命令，以及真实连续对账证据 |
| R7 / G6 | 故障注入和 sandbox/长跑证据检查工具 | 未找到可核验的连续2–4周完成证据；Phase6的8–12周要求也未满足 |
| R8 / G10 | 小额灰度入口和门控代码 | 前置验收、批准、真实小额运行与回滚证据未闭环，未放行 |
| G9 工程治理 | 锁文件、lint/type/test、覆盖率门槛及历史 CI 证据 | 当前工作区重新冻结；验收工具拒绝未跟踪文件；完整多平台兼容证据 |

主要依据：[9月6日验收索引](baselines/main_20260906/README.md)、[独立审计账本边界](authoritative_ledger.md)、[共享运行核心](../core/runtime.py)、[订单恢复检查](../live_trading/recovery.py)、[R7要求](r7_sandbox_runbook.md)、[Phase6要求](phase6_operations.md)。

BM1–BM8 是已具备基础能力，并非详细需求全部验收；各项退出门槛仍沿用原路线图。本次不按勾选数计算百分比，因为任务粒度不同，且若干文档状态明显滞后。

## 4. 策略、信号元层及最新研究

| 路线 | 当前判断 | 证据与边界 |
| --- | --- | --- |
| S0 缺陷清理 | 部分完成 | 优化器已使用正式注册策略，生命周期观测已接报告；增量消费、部分退出回调、跨标的bar索引仍有缺口 |
| S1 回测/研究能力 | 部分完成 | 图表、研究指标和walk-forward已实现；原 `--oos` 是事后分段，并不自动保证选择隔离 |
| S2 动态选币 | 部分基础完成 | PIT上市/退市集合已有；完整TopN选择、目标权重到执行及退市退出闭环未完成 |
| S3 仓位/保证金 | 部分完成 | 保证金、融资、强平和组合风险预算已有；不能再笼统写“做空免费”；融资空窗计费、vol targeting、分批目标权重仍待修复/交付 |
| S4 合约策略 | 未形成准入完成证据 | 账户模型存在不等于合约策略已通过研究与实盘准入 |
| SR0–SR3 | 有核心实现和验收记录 | 基线、健康生命周期、保护止损、组合预算已有；本次邮件缺口仍需逐条关闭 |
| SR4 | 部分完成 | SUI上市修复、PIT及公共源核验已有；通用下载旧入口仍有来源混用/截断/失败不停止等缺口 |
| SR5 | 回顾性运行已产出，默认策略未通过 | 结果明确fail；工程/元层完整交付仍pending |
| SR6 | 未放行 | Shadow工具、sandbox门控不能替代真实连续paper与批准 |
| P0/P1信号研究 | 9月18日已合入代码 | 默认关闭、研究用途；不是正式下单策略准入 |
| P2/P3信号研究 | 本地工程实现与三币隔离验收完成 | 源码仍有未提交/未跟踪文件；测试期768条预测全部弃权，80份归因账本全部等权回退；gate没有交易，不证明增益 |
| PM0–PM4仓位专项 | PM0已有契约；PM1–3部分底层存在；PM4未整体验收 | 风险动作和保护功能已有，不等于统一PositionManager及全部重启生命周期交付 |

最新 [9月19日研究验收报告](../reports/strategy_review_20260919/研究验收报告.md) 显示：

- 15/15 基线、572/572 矩阵、39/39 验证，共626次已完成；另有8次跨市场研究。
- 默认策略最后20%历史区间净收益 **-5.436%**、事件组 PF **0.468**；11个滚动窗口仅4个盈利，中位收益 **-0.447%**，研究门槛为fail。
- 原固定历史区间不是新获得的未见样本。后续前瞻协议已登记，但未来观察未发生，不能提前验收。
- 最新测试交付日志记录 **1304 passed、1 skipped、46 subtests passed**；XML包含子测试后为1351例。较早 `completion.json` 发布的是1301例记录，应按产物时间区分。
- [completion.json](../reports/strategy_review_20260919/completion.json) 仍为 `engineering_status=pending`、`meta_official_isolation=pending`、`evidence_pipeline_complete=false`；元层分层报告/交付回执未完成。较晚README称模型计算与隔离已做、导出发生内存问题并恢复，不能单凭该声明把整个批次改为完成。
- 当前准入仍为 **paused_revalidation**。部分非默认配置滚动表现较好，仅覆盖登记窗口，未完成所有独立准入要求。

P2/P3 的具体实验范围见 [p23_signal_meta_layer.md](p23_signal_meta_layer.md)。这里区分“工程隔离成立”“研究流程产出”“策略有效性通过”三件事。

## 5. 根据邮件形成的后续 Roadmap

以下为依赖顺序，不是未经确认的工期承诺。MAIL编号详见第7节及JSON清单。已修复条目作为回归清单保留；同一根因跨PR可在执行时合并，验收时回填每个原始ID。

| 顺序 | 工作包/映射 | 主要邮件ID | 完成标准 |
| --- | --- | --- | --- |
| 1 | 修准入判断（R3/R6，阻止假通过） | MAIL-20-1/2/3，MAIL-19-3/5/6 | 不健康监控拒绝准入；实际额外业务字段能被发现；连续运行缺口可检测；PF不足样本拒绝；DSR使用一致尺度；证据入口使用冻结配置 |
| 2 | 修数据及因果性（R1/R3/SR4） | MAIL-13-1/2/3，MAIL-21-1/2/3，MAIL-17-2，MAIL-32-2，MAIL-35-2 | 前缀数据不改变历史信号；来源/周期明确；分页完整；下载失败停止；统一symbol；未闭合bar拒绝；PIT退出有可追溯订单 |
| 3 | 修风险、生命周期与成本（R1/R2/R4/SR1–3） | MAIL-10-1，MAIL-12-2，MAIL-16-1/3/4，MAIL-17-3，MAIL-19-4，MAIL-31-1/3/4，MAIL-32-1，MAIL-33-1/2，MAIL-36-1/2，MAIL-37-2，MAIL-38-1/2 | 缺bar/部分成交减仓达目标；健康试运行不复用已评判亏损；lot/部分退出与报告对账；现金和成本口径一致；终点不重复消耗流动性；空数据返回稳定 |
| 4 | 修存量兼容与基线（R0/R5/G9） | MAIL-15-1/2/3，MAIL-23-1/2/3，MAIL-29-2，MAIL-34-1，MAIL-4-5 | 老事件可读且重试幂等；旧命令更新；兼容接口明示；验收拒绝无关未跟踪输入；Git新检出哈希一致；每次导出独立临时文件 |
| 5 | 补报告契约和当前研究交付（R1–3/SR5） | MAIL-11-1/2，MAIL-9-2/3，MAIL-18-3，MAIL-19-1/2，MAIL-21-4；9月19日completion pending | 常规机器可读输出完整；图表与执行口径一致；旧研究入口失效化或修正；完成元层导出、隔离及身份核验；独立研究判定可追溯 |
| 6 | 连续运行与受控灰度（R7→R8/SR6） | 原路线图退出条件 | 修复前置缺口后冻结代码/参数，按要求收集连续真实sandbox/paper及每日对账，满足批准、急停和回滚后才评估小额灰度 |

MAIL-37-1 的原始“部分减仓永远不重试”判断需要区别于 MAIL-36-1 的“缺bar标的根本未建退出单”；本次正常 GTC 剩余订单机制可完成部分减仓，不能把两者当成同一已确认缺陷。详见逐条记录。

## 6. 本次验证及限制

本次采用分工静态审阅、既有证据核验、少量针对性测试和内存/模拟样例。没有重跑全套研究，也没有连接交易所或执行sandbox/live。

- 依赖锁文件校验：本次通过。
- 止损两次撮合、健康生命周期、保护止损、breaker生命周期和默认恢复共5个测试文件：**52通过、1失败**；失败项为SQLite临时目录访问权限，不是业务断言失败。把临时目录移入工作区后仍受同类权限影响，未将其宣称通过。
- diagnostics/close_events：**25通过、3失败**；三个失败均受临时目录权限影响。
- end_of_backtest、round_trip、revalidation_execution、breaker_lifecycle、phase4：**40通过**。这些测试与前述部分重叠，不将不同运行数字直接相加。
- 近期邮件核对另有4个无需临时目录的针对性测试通过。其余临时目录相关测试不能宣称验证通过。
- 本次已复现的代表问题：监控不健康但准入通过、56天仅两条观测仍通过、PF样本不足仍通过、旧档案滑点重复扣除、部分入场lot信息丢失、short空窗利息、摆动因子前视、零成交量POC、旧事件解码失败、inf/nan漏解析等。
- 9月6日/9月18日/9月19日的全套测试是历史产物证据，不能与当前工作区的全套重新验收混称。
- 部分旧输出目录访问受限；“未找到可核验证据”不等于证明从未运行。对未确认问题明确保留证据不足。

本次证据快照与复现结果保存在 `tmp/roadmap_audit_20260920/`；可读主交付为本文及 [结构化逐条清单](codex_mail_findings_20260920.json)。没有覆盖已有研究证据或生产代码。


## 7. 全部77条邮件意见与当前完成情况

每条保留原始意见标题与邮件来源；状态是本次判断，不直接采用邮件的历史措辞。P1/P2为原邮件严重性标签，与信号元层P1/P2命名无关。

### PR #4

**MAIL-4-1 · P1 · 已修复** — [Isolate alert-delivery failures from the trading loop](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929)

- 当前核对：_alert 已独立 try/except 告警 sink.notify 异常并记录 alert_delivery_failed，避免投递失败从健康故障处理链逃出。
- 代码/既有测试：[live_trading/engine.py:215](../live_trading/engine.py)。
- 验证方式：静态确认异常边界；未连接Webhook或交易所。
**MAIL-4-2 · P2 · 已修复** — [Recompute recorded slippage after clamping limit fills](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929)

- 当前核对：交易记录 slip 现在取 abs(fill_price-price)，限价夹紧后以实际价差记录，原TotalSlippage错误路径已修正。注意CostBreakdown分量仍按理论滑点另算，属于额外一致性问题，不扩大此条修复结论。
- 代码/既有测试：[core/broker/fill_service.py:80](../core/broker/fill_service.py)；[core/broker/fill_service.py:266](../core/broker/fill_service.py)。
- 验证方式：静态核对限价夹紧及成交记录字段。
**MAIL-4-3 · P2 · 已修复** — [Classify broker synchronization exceptions as account failures](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929)

- 当前核对：行情更新与账户同步现在有独立异常分支，broker.sync 抛异常标记 ACCOUNT_SYNC_FAILED。
- 代码/既有测试：[live_trading/tick_orchestrator.py:103](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:115](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:119](../live_trading/tick_orchestrator.py)。
- 验证方式：静态核对_tick_once异常处理。
**MAIL-4-4 · P2 · 已修复** — [Preserve subjects when deduplicating health alerts](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929)

- 当前核对：risk_halt context 现在保留(code,subject)；HysteresisAlertSink根据完整稳定context生成key，因此不同标的同原因可分辨。
- 代码/既有测试：[live_trading/engine.py:246](../live_trading/engine.py)；[core/alerting.py:200](../core/alerting.py)。
- 验证方式：静态追踪健康告警context和去重key。
**MAIL-4-5 · P2 · 部分修复** — [Give concurrent state exports unique temporary files](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929)

- 当前核对：临时名已从固定.tmp变成带PID，可区分进程；同进程多个实例或线程写同一目标仍共享同一个PID临时名，未达到每次导出唯一。
- 代码/既有测试：[live_trading/state_export.py:97](../live_trading/state_export.py)；[live_trading/state_export.py:145](../live_trading/state_export.py)；[live_trading/state_export.py:150](../live_trading/state_export.py)。
- 验证方式：静态核对命名和os.replace路径；未触碰实盘状态文件。

### PR #9

**MAIL-9-1 · P2 · 部分修复** — [Exclude pending shorts from reserved cash](https://github.com/user-nmmmmm/QuantTradingV2/pull/9#pullrequestreview-5000638067)

- 当前核对：现金公式仍减去所有方向预留，pending_notional仍未区分buy/short；现在SPOT禁止开空、margin走保证金，正常账户模式显著缩小触发范围，但原预留现金算法并未按方向修正。
- 代码/既有测试：[core/risk/position_sizing.py:159](../core/risk/position_sizing.py)；[core/risk/position_sizing.py:164](../core/risk/position_sizing.py)；[core/risk/reservation.py:124](../core/risk/reservation.py)。
- 验证方式：静态核对账户模式分支和预留汇总。
**MAIL-9-2 · P2 · 仍存在** — [Compute drawdown peaks within each trailing window](https://github.com/user-nmmmmm/QuantTradingV2/pull/9#pullrequestreview-5000638067)

- 当前核对：滚动回撤仍先全历史cummax，再对全历史水下值rolling(window).min，旧峰值离开窗口后仍影响图。
- 代码/既有测试：[backtest/reporting/render/charts.py:224](../backtest/reporting/render/charts.py)；[backtest/reporting/render/charts.py:226](../backtest/reporting/render/charts.py)。
- 验证方式：静态公式核对，未生成图表。
**MAIL-9-3 · P2 · 仍存在** — [Use common bin edges for the PnL histogram](https://github.com/user-nmmmmm/QuantTradingV2/pull/9#pullrequestreview-5000638067)

- 当前核对：盈利/亏损直方图仍分别传同一整数bins，并未以全部PnL生成共享边界。
- 代码/既有测试：[backtest/reporting/render/charts.py:276](../backtest/reporting/render/charts.py)；[backtest/reporting/render/charts.py:278](../backtest/reporting/render/charts.py)。
- 验证方式：静态核对两次hist调用。

### PR #10

**MAIL-10-1 · P1 · 仍存在** — [Reserve execution costs when clamping to available cash](https://github.com/user-nmmmmm/QuantTradingV2/pull/10#pullrequestreview-5000967232)

- 当前核对：现金上限仍直接使用 cash-reserved_exposure，只扣1e-9安全边际；执行端仍要求成交金额加手续费不超现金，没有执行时可支付数量夹紧。其他风险预算可能缩量，但没有修复该现金契约。
- 代码/既有测试：[core/risk/position_sizing.py:164](../core/risk/position_sizing.py)；[core/risk/position_sizing.py:224](../core/risk/position_sizing.py)；[core/risk/__init__.py:42](../core/risk/__init__.py)；[core/broker/fill_service.py:158](../core/broker/fill_service.py)。
- 验证方式：静态追踪现金上限、夹紧和成交拒单条件。
**MAIL-10-2 · P2 · 已修复** — [Supply lifecycle observations to the diagnostic suite](https://github.com/user-nmmmmm/QuantTradingV2/pull/10#pullrequestreview-5000967232)

- 当前核对：引擎返回每策略 observed_close_events；main 将其传入报告，报告传给 build_diagnostics，生命周期覆盖率链路已经接通。
- 代码/既有测试：[backtest/engine.py:823](../backtest/engine.py)；[main.py:636](../main.py)；[backtest/reporting/__init__.py:261](../backtest/reporting/__init__.py)；[tests/test_diagnostics.py:342](../tests/test_diagnostics.py)；[tests/test_diagnostics.py:360](../tests/test_diagnostics.py)。
- 验证方式：定向合跑 diagnostics/close_events 共25 passed、3因系统临时目录PermissionError失败；引擎close_event计数测试通过，报告写文件测试被环境权限阻断，静态接线已确认。

### PR #11

**MAIL-11-1 · P1 · 仍存在** — [Keep OOS results out of parameter ranking](https://github.com/user-nmmmmm/QuantTradingV2/pull/11#pullrequestreview-5001918007)

- 当前核对：路线图仍写“参数优化默认按 OOS 排序”，与同页 train-only 冲突。优化器已有 full-sample 排序的 caveat 并提供单独walk-forward方向，但未修正原验收标准。
- 代码/既有测试：[docs/strategy_development_roadmap.md:169](../docs/strategy_development_roadmap.md)；[analysis/optimize.py:246](../analysis/optimize.py)。
- 验证方式：静态核对文档和优化器输出说明。
**MAIL-11-2 · P1 · 部分修复** — [Leave metrics.json serialization on the roadmap](https://github.com/user-nmmmmm/QuantTradingV2/pull/11#pullrequestreview-5001918007)

- 当前核对：专项回测脚本已经写 metrics.json 且磁盘有产物；常规 ReportGenerator.generate/main.py 仍未统一写该文件，文档“backtest/reporting输出metrics.json”仍过度宣称。
- 代码/既有测试：[docs/strategy_development_roadmap.md:25](../docs/strategy_development_roadmap.md)；[backtest/reporting/__init__.py:278](../backtest/reporting/__init__.py)；[scripts/run_revalidation60.py:153](../scripts/run_revalidation60.py)；[scripts/run_p0_recovery_backtest.py:108](../scripts/run_p0_recovery_backtest.py)。
- 验证方式：搜索全部 core/backtest/main/scripts 写入点；常规生成器只返回 metrics，专项脚本保存。
**MAIL-11-3 · P1 · 已修复** — [Optimize the strategies behind the routing names](https://github.com/user-nmmmmm/QuantTradingV2/pull/11#pullrequestreview-5001918007)

- 当前核对：优化器现在直接构造 TrendBreakoutStrategy/TrendBreakdownStrategy，并以 entry_window/exit_window 网格驱动，不再只是旧类改字典名。
- 代码/既有测试：[analysis/optimize.py:23](../analysis/optimize.py)；[analysis/optimize.py:37](../analysis/optimize.py)；[analysis/optimize.py:108](../analysis/optimize.py)。
- 验证方式：静态检查导入、构造工厂和单组任务入口。

### PR #12

**MAIL-12-1 · P1 · 已修复** — [Consume circuit-breaker fills outside Router.route](https://github.com/user-nmmmmm/QuantTradingV2/pull/12#pullrequestreview-5001994647)

- 当前核对：当前强制减仓/熔断成交在引擎里立即形成 forced_trades，并显式遍历策略消费 CloseEvent；不再依赖恢复 Router.route 才消费熔断平仓。
- 代码/既有测试：[backtest/engine.py:491](../backtest/engine.py)；[backtest/engine.py:498](../backtest/engine.py)；[strategies/base.py:129](../strategies/base.py)；[tests/test_close_events.py:62](../tests/test_close_events.py)。
- 验证方式：静态核对实际引擎消费路径；定向 tests/test_close_events.py 所有6个参数化/测试案例通过（与diagnostics合跑）。
**MAIL-12-2 · P1 · 仍存在** — [Pass the closing symbol's bar index to its callback](https://github.com/user-nmmmmm/QuantTradingV2/pull/12#pullrequestreview-5001994647)

- 当前核对：策略遍历全局所有 CloseEvent，却把调用方当前 bar_index 传给所有 event.symbol；没有按平仓标的取得对应历史索引。强制成交消费还使用组合全局 bar_index。
- 代码/既有测试：[strategies/base.py:143](../strategies/base.py)；[strategies/base.py:166](../strategies/base.py)；[router/router.py:83](../router/router.py)；[backtest/engine.py:501](../backtest/engine.py)。
- 验证方式：静态追踪全局事件循环与回调参数；现有CloseEvent测试只覆盖归因/去重，未覆盖错位时间轴。

### PR #13

**MAIL-13-1 · P1 · 仍存在** — [Delay swing markers until their confirmation bar](https://github.com/user-nmmmmm/QuantTradingV2/pull/13#pullrequestreview-5005367713)

- 当前核对：SWING_HIGH/LOW 仍用 center=True 并直接返回未延后标记；现有测试只核对长度。
- 代码/既有测试：[core/factors/support_resistance.py:52](../core/factors/support_resistance.py)；[core/factors/support_resistance.py:62](../core/factors/support_resistance.py)；[tests/test_factors.py:105](../tests/test_factors.py)。
- 验证方式：2026-09-20 内存重现：high=[1,2,8,3,2],order=1，完整数据在索引2给8，而截断至索引2时为NaN，确认未来信息泄漏。
**MAIL-13-2 · P1 · 仍存在** — [Paginate open-interest history through the requested end](https://github.com/user-nmmmmm/QuantTradingV2/pull/13#pullrequestreview-5005367713)

- 当前核对：OI 历史仍只有一次 fetchOpenInterestHistory；end_date 仅过滤返回结果，未继续分页。
- 代码/既有测试：[core/data_fetcher.py:455](../core/data_fetcher.py)；[core/data_fetcher.py:456](../core/data_fetcher.py)；[core/data_fetcher.py:475](../core/data_fetcher.py)。
- 验证方式：静态检查完整函数，无交易所调用。
**MAIL-13-3 · P2 · 仍存在** — [Leave POC empty for zero-volume windows](https://github.com/user-nmmmmm/QuantTradingV2/pull/13#pullrequestreview-5005367713)

- 当前核对：POC 仍在 total_volume<=0 检查前赋值，零量窗口输出伪支撑价。
- 代码/既有测试：[core/factors/volume.py:112](../core/factors/volume.py)；[core/factors/volume.py:117](../core/factors/volume.py)。
- 验证方式：2026-09-20 内存重现3根零成交量窗口：POC=1.5、VAH/VAL=NaN。

### PR #15

**MAIL-15-1 · P1 · 仍存在** — [Regenerate hashes after Git line-ending normalization](https://github.com/user-nmmmmm/QuantTradingV2/pull/15#pullrequestreview-5019831836)

- 当前核对：归档清单仍记录 CRLF 字节。实际工作树可通过，但 Git 已提交 blob 是 LF；新检出/非 Windows 环境不能按清单验证。
- 代码/既有测试：[docs/baseline/phase0/reports_manifest.json:8](../docs/baseline/phase0/reports_manifest.json)；[.gitattributes:1](../.gitattributes)。
- 验证方式：2026-09-20 只读重算：首个 benchmark.csv 工作树21727字节/fdf23e1a…；git show HEAD:该文件21005字节/ff37867c…，与邮件完全一致。
**MAIL-15-2 · P2 · 仍存在** — [Replace the HEAD check with a check of the recorded commit](https://github.com/user-nmmmmm/QuantTradingV2/pull/15#pullrequestreview-5019831836)

- 当前核对：文档仍要求 git rev-parse HEAD 输出旧基线 ff14fb8；当前 HEAD 为 ab15bc2，未改为检查被记录对象。
- 代码/既有测试：[docs/baseline/phase0/baseline_lock.md:16](../docs/baseline/phase0/baseline_lock.md)。
- 验证方式：静态核对文档及当前 Git HEAD。
**MAIL-15-3 · P2 · 仍存在** — [Enforce read-only archives rather than relying on chmod 444](https://github.com/user-nmmmmm/QuantTradingV2/pull/15#pullrequestreview-5019831836)

- 当前核对：仍声称 chmod 444 只读冻结；Git 无法保留此权限位，CI 未见归档目录变更/哈希守卫。
- 代码/既有测试：[docs/baseline/phase0/README.md:8](../docs/baseline/phase0/README.md)；[docs/baseline/phase0/freeze_notice.md:24](../docs/baseline/phase0/freeze_notice.md)；[.github/workflows/tests.yml:23](../.github/workflows/tests.yml)。
- 验证方式：静态核对冻结说明、Git 工作流及属性规则；未更改归档。

### PR #16

**MAIL-16-1 · P1 · 仍存在** — [Aggregate close callbacks until the position is flat](https://github.com/user-nmmmmm/QuantTradingV2/pull/16#pullrequestreview-5027130456)

- 当前核对：基类仍对每个CloseEvent立即on_trade_closed，is_position_fully_closed仅用于清context；TrendBreakout的cohort聚合减轻其健康误计，但Range依然把部分退出当独立失败，未实现按position聚合一次闭合回调。
- 代码/既有测试：[strategies/base.py:142](../strategies/base.py)；[strategies/base.py:166](../strategies/base.py)；[strategies/mean_reversion.py:180](../strategies/mean_reversion.py)；[strategies/trend_breakout.py:194](../strategies/trend_breakout.py)。
- 验证方式：2026-09-20 注入同一position三条尚未完全平仓的亏损CloseEvent，Range在bar100已设置cooldown_until124。详见 mid_verification.json MAIL-16-1。
**MAIL-16-2 · P1 · 已修复** — [Cancel outstanding entries before tail liquidation](https://github.com/user-nmmmmm/QuantTradingV2/pull/16#pullrequestreview-5027130456)

- 当前核对：期末尾仓处理在读取待平数量、提交EndOfBacktest订单和处理合成bar之前取消保护单及所有pending/active订单，剩余开仓不会借合成bar的大成交量补成交。
- 代码/既有测试：[backtest/engine.py:1053](../backtest/engine.py)；[backtest/engine.py:1056](../backtest/engine.py)；[backtest/engine.py:1063](../backtest/engine.py)；[tests/test_end_of_backtest.py:81](../tests/test_end_of_backtest.py)。
- 验证方式：静态确认取消顺序；2026-09-20 定向回归40 passed含期末估值/强制退出测试。
**MAIL-16-3 · P2 · 仍存在** — [Preserve lot details across partial entry fills](https://github.com/user-nmmmmm/QuantTradingV2/pull/16#pullrequestreview-5027130456)

- 当前核对：报告仍逐入场fill压栈、逐配对位置消费lot_closes，而真实lot合并同一订单多次入场；一笔退出覆盖多个入场碎片时只有首碎片有lot/position/risk，后续缺失。新增round-trip汇总因缺position_id也无法修正该情形。
- 代码/既有测试：[backtest/reporting/trades.py:121](../backtest/reporting/trades.py)；[backtest/reporting/trades.py:207](../backtest/reporting/trades.py)；[backtest/reporting/trades.py:345](../backtest/reporting/trades.py)；[core/lots.py:240](../core/lots.py)。
- 验证方式：2026-09-20 真实本地Broker撮合25单位开仓分10/10/5、一次卖25：报告3条legs的lot_id=[LOT-000000001,null,null]，被汇总成3个roundtrips。详见 mid_verification.json MAIL-16-3。
**MAIL-16-4 · P2 · 部分修复** — [Scale initial risk to the quantity being closed](https://github.com/user-nmmmmm/QuantTradingV2/pull/16#pullrequestreview-5027130456)

- 当前核对：CloseEvent风险已按qty_closed/qty_original比例分摊；主报告改按position汇总并按lot去重总初始风险，完整往返的R不再重复分母。但原LotClose.initial_risk和重建legs仍重复完整风险，直接消费legs的旧研究入口仍未统一；且部分入场匹配缺失仍影响此链。
- 代码/既有测试：[core/lots.py:203](../core/lots.py)；[core/broker/fill_service.py:235](../core/broker/fill_service.py)；[core/broker/fill_service.py:250](../core/broker/fill_service.py)；[backtest/reporting/trades.py:367](../backtest/reporting/trades.py)；[backtest/reporting/__init__.py:216](../backtest/reporting/__init__.py)；[tests/test_round_trip_aggregation.py:143](../tests/test_round_trip_aggregation.py)；[tests/test_round_trip_aggregation.py:151](../tests/test_round_trip_aggregation.py)。
- 验证方式：2026-09-20 定向回归40 passed含完整往返risk只计一次；现有test_legs_each_repeat_the_lots_full_risk明确仍断言每条leg完整250，而roundtrip250。

### PR #17

**MAIL-17-1 · P1 · 已修复** — [Continue routing exits after blocking new entries](https://github.com/user-nmmmmm/QuantTradingV2/pull/17#pullrequestreview-5028474123)

- 当前核对：共享运行时已分离持仓管理与新仓候选，BLOCK_NEW仍调用退出管理，只有entry collection/allocation被禁用。
- 代码/既有测试：[core/runtime.py:214](../core/runtime.py)；[core/runtime.py:219](../core/runtime.py)；[core/runtime.py:354](../core/runtime.py)；[tests/test_breaker_lifecycle.py:38](../tests/test_breaker_lifecycle.py)。
- 验证方式：2026-09-20 定向回归40 passed包含test_block_new_runs_position_management_but_never_collects_entry。
**MAIL-17-2 · P1 · 仍存在** — [Close positions when point-in-time membership ends](https://github.com/user-nmmmmm/QuantTradingV2/pull/17#pullrequestreview-5028474123)

- 当前核对：--universe-file路径仍只是截断delisted_at之后bars，没有在最后可交易bar生成退出标记；引擎新增scheduled_exit/AnnouncedMarginDelisting能力，但普通PointInTimeUniverse.apply未接入，旧路径仍可能持仓带到总体期末。
- 代码/既有测试：[core/universe.py:66](../core/universe.py)；[core/universe.py:79](../core/universe.py)；[main.py:433](../main.py)；[backtest/engine.py:443](../backtest/engine.py)；[backtest/engine.py:1061](../backtest/engine.py)。
- 验证方式：静态追踪CSV universe输入→apply→引擎；已有scheduled_exit分支不能证明通用PIT路径已关闭缺口。
**MAIL-17-3 · P2 · 仍存在** — [Reset borrow accrual when a short is flat](https://github.com/user-nmmmmm/QuantTradingV2/pull/17#pullrequestreview-5028474123)

- 当前核对：正式Broker在flat期间仍跳过symbol且不清_last_borrow_time；仅Ghost/P3影子路径自行pop，不修复正式路径再开空仓的跨空窗利息。
- 代码/既有测试：[core/broker/financing.py:35](../core/broker/financing.py)；[core/broker/financing.py:77](../core/broker/financing.py)；[backtest/signal_ghost.py:74](../backtest/signal_ghost.py)；[backtest/signal_meta_replay.py:214](../backtest/signal_meta_replay.py)。
- 验证方式：2026-09-20 内存复现：日费0.1，1月2日平仓后2月2日重开首次收3.1，包含31天空窗。详见 mid_verification.json MAIL-17-3。

### PR #18

**MAIL-18-1 · P1 · 部分修复** — [Preserve exit control for synchronized live positions](https://github.com/user-nmmmmm/QuantTradingV2/pull/18#pullrequestreview-5031311051)

- 当前核对：账户同步已从持久成交账本重建lots/owner，解决本系统有成交历史的重启仓位；不匹配仓位标unowned并禁止新风险，缺保护会尝试具名平仓。但无历史归属的外部持仓仍没有策略owner/entry_time，策略与max-holding退出不会执行，未见完整外部仓位接管验收。
- 代码/既有测试：[core/live_broker/account_sync.py:37](../core/live_broker/account_sync.py)；[core/live_broker/__init__.py:184](../core/live_broker/__init__.py)；[core/live_broker/submission.py:295](../core/live_broker/submission.py)；[router/router.py:93](../router/router.py)；[router/router.py:167](../router/router.py)；[live_trading/tick_orchestrator.py:685](../live_trading/tick_orchestrator.py)；[tests/test_revalidation_execution.py:174](../tests/test_revalidation_execution.py)；[tests/test_revalidation_execution.py:227](../tests/test_revalidation_execution.py)。
- 验证方式：2026-09-20 定向回归40 passed包含test_revalidation_execution；读代码确认对有持久成交事实的恢复与无法归属的边界。
**MAIL-18-2 · P1 · 已修复** — [Batch live candidates before allocating portfolio risk](https://github.com/user-nmmmmm/QuantTradingV2/pull/18#pullrequestreview-5031311051)

- 当前核对：实盘先收集候选并按close_time分批，再调用组合allocator一次分配，已不再按每个symbol立即消耗风险预算。该文件当前有其他未提交改动，结论针对当前工作区。
- 代码/既有测试：[live_trading/tick_orchestrator.py:294](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:298](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:315](../live_trading/tick_orchestrator.py)；[tests/test_phase4_routing_allocation.py:42](../tests/test_phase4_routing_allocation.py)；[tests/test_sr3_portfolio_risk.py:147](../tests/test_sr3_portfolio_risk.py)。
- 验证方式：静态核对真实tick接线；2026-09-20 定向回归40 passed含Phase4批量排名测试。
**MAIL-18-3 · P2 · 仍存在** — [Use the configured holding limit in diagnostics](https://github.com/user-nmmmmm/QuantTradingV2/pull/18#pullrequestreview-5031311051)

- 当前核对：运行路由读取router.max_holding_days（已从phase4迁移），但build_diagnostics和Phase4分析脚本仍固定365天；非365配置的超时诊断仍与执行政策不一致。
- 代码/既有测试：[core/diagnostics.py:506](../core/diagnostics.py)；[scripts/run_phase4_analysis.py:137](../scripts/run_phase4_analysis.py)；[composition/factory.py:179](../composition/factory.py)；[config/params.yaml:102](../config/params.yaml)。
- 验证方式：静态对照配置传播及两个报告调用点。

### PR #19

**MAIL-19-1 · P1 · 部分修复** — [Reserve a genuinely unseen holdout](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：新的9/19研究明确已见历史不是新holdout，并冻结2026-10-20至2027-04-18前瞻窗口、最早2027-05-08验收；但旧Phase5脚本及报告仍将Phase0既见档案切片标作一次性final holdout，未替换/失效化。前瞻证据尚未成熟。
- 代码/既有测试：[scripts/run_phase5_analysis.py:35](../scripts/run_phase5_analysis.py)；[scripts/run_phase5_analysis.py:75](../scripts/run_phase5_analysis.py)；[scripts/run_phase5_analysis.py:191](../scripts/run_phase5_analysis.py)；[reports/strategy_review_20260919/研究验收报告.md:24](../reports/strategy_review_20260919/研究验收报告.md)；[reports/strategy_review_20260919/README.md:78](../reports/strategy_review_20260919/README.md)。
- 验证方式：静态核对旧runner及最新协议/报告；未打开未来保留期、未执行研究重跑。
**MAIL-19-2 · P1 · 部分修复** — [Evaluate the frozen Phase 4 implementation](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：最新9/19批次已有修复前/后冻结源码、配置、输入身份和真实当前引擎运行；但原Phase5入口依然硬编码Phase0档案PRIMARY，并从旧净值/交易计算准入，不反映当前实现。新批次未提交且总验收pending。
- 代码/既有测试：[scripts/run_phase5_analysis.py:34](../scripts/run_phase5_analysis.py)；[scripts/run_phase5_analysis.py:181](../scripts/run_phase5_analysis.py)；[reports/strategy_review_20260919/README.md:17](../reports/strategy_review_20260919/README.md)；[reports/strategy_review_20260919/README.md:19](../reports/strategy_review_20260919/README.md)；[reports/strategy_review_20260919/engineering_comparison.json:2](../reports/strategy_review_20260919/engineering_comparison.json)。
- 验证方式：静态核对旧runner与最新冻结证据；不把新路径的存在当成旧入口已修复。
**MAIL-19-3 · P1 · 仍存在** — [Reject insufficient profit-factor samples](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：G13 仍仅检查PF值与置信下界，没有要求pf.status=='ok'，不足30笔的有利小样本仍能通过该门槛。
- 代码/既有测试：[analysis/research_validation.py:360](../analysis/research_validation.py)；[analysis/research_validation.py:387](../analysis/research_validation.py)；[core/metrics/trade_quality.py:25](../core/metrics/trade_quality.py)。
- 验证方式：2026-09-20 内存复现19笔（18笔+10，1笔-1）：profit_factor.status=insufficient，但G13_pf_significance=true。详见 mid_verification.json MAIL-19-3。
**MAIL-19-4 · P2 · 仍存在** — [Avoid double-counting legacy slippage](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：缺 theoretical_price 时仍回退fill price，并照样生成gross_pnl_theoretical；成本敏感度再扣slippage，旧档案仍重复计入滑点。
- 代码/既有测试：[backtest/reporting/trades.py:94](../backtest/reporting/trades.py)；[backtest/reporting/trades.py:211](../backtest/reporting/trades.py)；[core/metrics/attribution.py:141](../core/metrics/attribution.py)；[core/metrics/attribution.py:149](../core/metrics/attribution.py)；[scripts/run_phase5_analysis.py:65](../scripts/run_phase5_analysis.py)。
- 验证方式：2026-09-20 只读重建PRIMARY完整历史：closed net=-1581.579048，1x成本净值=-2070.377486，差额488.798438等于全部slippage。详见 mid_verification.json MAIL-19-4。
**MAIL-19-5 · P2 · 仍存在** — [Scale the expected maximum Sharpe consistently](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：observed Sharpe乘sqrt(periods_per_year)，expected_max仍为未缩放标准正态顺序统计量；分子和方差口径不一致。
- 代码/既有测试：[analysis/research_validation.py:285](../analysis/research_validation.py)；[analysis/research_validation.py:293](../analysis/research_validation.py)；[analysis/research_validation.py:298](../analysis/research_validation.py)；[scripts/run_phase5_analysis.py:145](../scripts/run_phase5_analysis.py)。
- 验证方式：2026-09-20 相同80条收益及12次试验，periods_per_year由1改365.25，expected_max仍1.664811，概率由0.000080025升至1.0。详见 mid_verification.json MAIL-19-5。
**MAIL-19-6 · P2 · 仍存在** — [Honor the Phase 5 configuration](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890)

- 当前核对：phase5 YAML仍声明分区、窗口和压力参数；run_phase5_analysis无配置读取，继续硬编码60/20/20、730/180/180和默认AdmissionThresholds，修改YAML不会同步该证据生成入口。
- 代码/既有测试：[config/params.yaml:243](../config/params.yaml)；[scripts/run_phase5_analysis.py:77](../scripts/run_phase5_analysis.py)；[scripts/run_phase5_analysis.py:96](../scripts/run_phase5_analysis.py)；[scripts/run_phase5_analysis.py:181](../scripts/run_phase5_analysis.py)；[analysis/research_validation.py:343](../analysis/research_validation.py)。
- 验证方式：静态检查配置、完整runner和调用参数；未改配置运行。

### PR #20

**MAIL-20-1 · P1 · 仍存在** — [Fail admission on unhealthy monitoring snapshots](https://github.com/user-nmmmmm/QuantTradingV2/pull/20#pullrequestreview-5032477893)

- 当前核对：监控维度 ok=false 仍只追加 generated_alerts，不追加 issues；只要告警送达已验证，就会 passed=true 并被准入检查采用。
- 代码/既有测试：[core/admission_gates.py:380](../core/admission_gates.py)；[core/admission_gates.py:397](../core/admission_gates.py)；[core/admission_gates.py:446](../core/admission_gates.py)。
- 验证方式：2026-09-20 内存复现：data_quality.ok=false，其他维度健康，evaluate_monitoring 返回 passed=true。详见 mid_verification.json MAIL-20-1。
**MAIL-20-2 · P1 · 仍存在** — [Compare unexpected fields in actual reconciliation records](https://github.com/user-nmmmmm/QuantTradingV2/pull/20#pullrequestreview-5032477893)

- 当前核对：逐字段对账仍只遍历 expected_record.items()，实际记录额外业务字段未纳入比较。
- 代码/既有测试：[core/admission_gates.py:138](../core/admission_gates.py)；[core/admission_gates.py:154](../core/admission_gates.py)。
- 验证方式：2026-09-20 内存复现：六层 actual 均多出 unexpected_business_status=bad，对账仍 coverage=1、passed=true。详见 mid_verification.json MAIL-20-2。
**MAIL-20-3 · P1 · 仍存在** — [Require continuous evidence across the paper-run interval](https://github.com/user-nmmmmm/QuantTradingV2/pull/20#pullrequestreview-5032477893)

- 当前核对：paper 时长仍仅由最早/最晚日期差得出，没有逐日覆盖或中间缺口校验。
- 代码/既有测试：[core/admission_gates.py:248](../core/admission_gates.py)；[core/admission_gates.py:263](../core/admission_gates.py)。
- 验证方式：2026-09-20 内存复现：2026-01-01 与 2026-02-25 两条、两个regime，56天paper gate passed=true。详见 mid_verification.json MAIL-20-3。

### PR #21

**MAIL-21-1 · P1 · 仍存在** — [Reject Yahoo fallback data from the Binance cache](https://github.com/user-nmmmmm/QuantTradingV2/pull/21#pullrequestreview-5052647523)

- 当前核对：通用下载路径仍可回退Yahoo且缓存脚本未验证实际来源/周期；新研究专用采集器不关闭此旧路径。
- 代码/既有测试：[scripts/fetch_binance_data.py:115](../scripts/fetch_binance_data.py)；[core/data_fetcher.py:239](../core/data_fetcher.py)；[core/data_fetcher.py:251](../core/data_fetcher.py)。
- 验证方式：静态核对
**MAIL-21-2 · P1 · 仍存在** — [Split intraday chunks below the fetcher's safety cap](https://github.com/user-nmmmmm/QuantTradingV2/pull/21#pullrequestreview-5052647523)

- 当前核对：依旧按自然年分块；15m/5m全年超过单次10000根上限。
- 代码/既有测试：[scripts/fetch_binance_data.py:92](../scripts/fetch_binance_data.py)；[scripts/fetch_binance_data.py:113](../scripts/fetch_binance_data.py)；[core/data_fetcher.py:324](../core/data_fetcher.py)。
- 验证方式：静态核对
**MAIL-21-3 · P1 · 仍存在** — [Stop the matrix when a cache refresh fails](https://github.com/user-nmmmmm/QuantTradingV2/pull/21#pullrequestreview-5052647523)

- 当前核对：下载子进程check=False且不检查returncode，失败后仍进入回测。
- 代码/既有测试：[scripts/run_backtest_matrix.py:96](../scripts/run_backtest_matrix.py)；[scripts/run_backtest_matrix.py:177](../scripts/run_backtest_matrix.py)。
- 验证方式：本次模拟returncode=1确认未传播异常
**MAIL-21-4 · P2 · 仍存在** — [Parse non-finite metrics emitted by reports](https://github.com/user-nmmmmm/QuantTradingV2/pull/21#pullrequestreview-5052647523)

- 当前核对：数值表达式仍不支持inf/nan，会漏掉报告指标。
- 代码/既有测试：[scripts/run_backtest_matrix.py:112](../scripts/run_backtest_matrix.py)。
- 验证方式：本次离线构造报告，解析结果为空字典

### PR #22

**MAIL-22-1 · P1 · 已修复** — [Remove the pre-listing SUI price series](https://github.com/user-nmmmmm/QuantTradingV2/pull/22#pullrequestreview-5061172508)

- 当前核对：当前SUI数据从2023-05-03开始，PIT universe同步上市日；不等于所有其他缓存来源均通过审计。
- 代码/既有测试：[data/binance/1d/SUI_USDT.csv:2](../data/binance/1d/SUI_USDT.csv)；[config/universe_binance_spot_1d.csv:26](../config/universe_binance_spot_1d.csv)。
- 验证方式：本次读取CSV首行与universe记录

### PR #23

**MAIL-23-1 · P2 · 仍存在** — [Retain a decoding shim for archived ledger events](https://github.com/user-nmmmmm/QuantTradingV2/pull/23#pullrequestreview-5061253521)

- 当前核对：注册表未接纳core.ledger:CashEvent/MarkPriceEvent，老审计库类型无法解码。
- 代码/既有测试：[core/events/codec.py:27](../core/events/codec.py)；[core/events/codec.py:38](../core/events/codec.py)。
- 验证方式：本次调用legacy解析复现ValueError
**MAIL-23-2 · P1 · 仍存在** — [Preserve legacy wire names for moved event classes](https://github.com/user-nmmmmm/QuantTradingV2/pull/23#pullrequestreview-5061253521)

- 当前核对：新wire名core.events.types:*与旧core.events:*文档仍不同；幂等比较只忽略observed_at，不归一化类型别名。
- 代码/既有测试：[core/events/codec.py:22](../core/events/codec.py)；[core/events/store.py:198](../core/events/store.py)。
- 验证方式：本次同义新旧事件文档比较为false
**MAIL-23-3 · P2 · 仍存在** — [Update operational reconciliation commands after the move](https://github.com/user-nmmmmm/QuantTradingV2/pull/23#pullrequestreview-5061253521)

- 当前核对：两本运行手册仍引用已删除core.reconciliation_job；实际模块已迁往research.audit。
- 代码/既有测试：[docs/r6_operations.md:14](../docs/r6_operations.md)；[docs/r7_sandbox_runbook.md:98](../docs/r7_sandbox_runbook.md)；[research/audit/reconciliation_job.py:1](../research/audit/reconciliation_job.py)。
- 验证方式：静态核对

### PR #25

**MAIL-25-1 · P2 · 已修复** — [Preserve the broker logger name after splitting](https://github.com/user-nmmmmm/QuantTradingV2/pull/25#pullrequestreview-5064031605)

- 当前核对：包化后logger为core.broker.matching/fill_service，均属于core.broker子层级，恢复父logger handler传播。
- 代码/既有测试：[core/broker/matching.py:26](../core/broker/matching.py)；[core/broker/fill_service.py:22](../core/broker/fill_service.py)。
- 验证方式：本次读取实际logger名称确认

### PR #26

**MAIL-26-1 · P1 · 已修复** — [Preserve restored daily breakers in risk decisions](https://github.com/user-nmmmmm/QuantTradingV2/pull/26#pullrequestreview-5065127391)

- 当前核对：同日恢复同步设置daily_loss_triggered；断点与交易日原子性另见MAIL-33-1。
- 代码/既有测试：[live_trading/engine.py:300](../live_trading/engine.py)；[live_trading/engine.py:320](../live_trading/engine.py)；[core/risk/circuit_breaker.py:328](../core/risk/circuit_breaker.py)。
- 验证方式：静态核对
**MAIL-26-2 · P1 · 已修复** — [Finish liquidating every position before terminating](https://github.com/user-nmmmmm/QuantTradingV2/pull/26#pullrequestreview-5065127391)

- 当前核对：终止前检查是否仍有仓位；未平时保留risk-only执行并记录unresolved。HEAD已有。
- 代码/既有测试：[backtest/engine.py:581](../backtest/engine.py)；[backtest/engine.py:584](../backtest/engine.py)；[tests/test_breaker_lifecycle.py:182](../tests/test_breaker_lifecycle.py)。
- 验证方式：本次breaker lifecycle既有测试通过；缺bar专项未另加测试

### PR #27

**MAIL-27-1 · P1 · 已修复** — [Ignore pre-probation losses when evaluating probation](https://github.com/user-nmmmmm/QuantTradingV2/pull/27#pullrequestreview-5074006738)

- 当前核对：试运行判定只取新cohort，样本数足够后才评估；与通过后清空亏损基线缺口MAIL-32-1分别跟踪。
- 代码/既有测试：[core/strategy_health.py:460](../core/strategy_health.py)；[core/strategy_health.py:618](../core/strategy_health.py)；[tests/test_sr1_strategy_health.py:172](../tests/test_sr1_strategy_health.py)。
- 验证方式：本次相关逻辑测试通过；同文件1项SQLite临时目录权限受阻
**MAIL-27-2 · P1 · 已修复** — [Fail closed when protective-order submission is rejected](https://github.com/user-nmmmmm/QuantTradingV2/pull/27#pullrequestreview-5074006738)

- 当前核对：已检查submit结果，未确认保护时告警且尝试风险平仓；HEAD已含accepted检查。
- 代码/既有测试：[live_trading/tick_orchestrator.py:667](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:676](../live_trading/tick_orchestrator.py)。
- 验证方式：静态核对；真实交易所拒绝场景未重跑
**MAIL-27-3 · P2 · 已修复** — [Validate the account mode selected by the live CLI](https://github.com/user-nmmmmm/QuantTradingV2/pull/27#pullrequestreview-5074006738)

- 当前核对：运行入口将CLI market_type送入成本/账户模式验证，别名映射已存在。
- 代码/既有测试：[run_live.py:109](../run_live.py)；[tests/test_sr3_portfolio_risk.py:480](../tests/test_sr3_portfolio_risk.py)。
- 验证方式：静态核对及已有测试

### PR #28

**MAIL-28-1 · P1 · 已修复** — [Exclude resident stops from the opening-order pass](https://github.com/user-nmmmmm/QuantTradingV2/pull/28#pullrequestreview-5074542286)

- 当前核对：开盘撮合明确排除保护止损，止损由独立pass处理。
- 代码/既有测试：[backtest/execution_adapter.py:13](../backtest/execution_adapter.py)；[backtest/execution_adapter.py:37](../backtest/execution_adapter.py)；[tests/test_backtest_stop_pass_and_liquidity.py:100](../tests/test_backtest_stop_pass_and_liquidity.py)。
- 验证方式：本次该测试文件7项通过
**MAIL-28-2 · P1 · 已修复** — [Carry the consumed volume budget into the stop pass](https://github.com/user-nmmmmm/QuantTradingV2/pull/28#pullrequestreview-5074542286)

- 当前核对：按bar时间戳复用成交量余额，不为第二次撮合重建额度。
- 代码/既有测试：[core/broker/matching.py:421](../core/broker/matching.py)；[tests/test_backtest_stop_pass_and_liquidity.py:179](../tests/test_backtest_stop_pass_and_liquidity.py)。
- 验证方式：本次相关测试通过
**MAIL-28-3 · P1 · 已修复** — [Ignore closed historical entries during live risk rechecks](https://github.com/user-nmmmmm/QuantTradingV2/pull/28#pullrequestreview-5074542286)

- 当前核对：按client_order_id匹配仍未平仓lot；已关闭历史开仓不会减后来仓位。HEAD已含此修复。
- 代码/既有测试：[live_trading/tick_orchestrator.py:483](../live_trading/tick_orchestrator.py)；[tests/test_entry_risk_contract.py:241](../tests/test_entry_risk_contract.py)。
- 验证方式：静态核对及已有针对性测试，本次未执行该测试

### PR #29

**MAIL-29-1 · P1 · 已修复** — [Update the checksum to match the modified lockfile](https://github.com/user-nmmmmm/QuantTradingV2/pull/29#pullrequestreview-5078459446)

- 当前核对：当前锁文件校验已通过；历史修复提交44f9178。
- 代码/既有测试：[requirements.lock.sha256:1](../requirements.lock.sha256)；[scripts/verify_lock.py:8](../scripts/verify_lock.py)。
- 验证方式：本次实际执行verify_lock通过
**MAIL-29-2 · P2 · 仍存在** — [Restore the promised Metrics compatibility export](https://github.com/user-nmmmmm/QuantTradingV2/pull/29#pullrequestreview-5078459446)

- 当前核对：仍承诺旧接口兼容，但Metrics类未导出；直接导入失败。
- 代码/既有测试：[core/metrics/__init__.py:4](../core/metrics/__init__.py)；[core/metrics/__init__.py:48](../core/metrics/__init__.py)。
- 验证方式：本次离线导入复现ImportError

### PR #31

**MAIL-31-1 · P1 · 仍存在** — [Warm up test runs for each candidate's full lookback](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#pullrequestreview-5087145892)

- 当前核对：候选含100-bar entry window，但测试运行前缀仍固定全局 warmup_period=30，没有按 candidate 最大指标lookback扩大。60-bar测试加30-bar前缀总90不足100；现有warmup测试只保证固定前缀可用，不覆盖候选lookback。
- 代码/既有测试：[analysis/optimize.py:33](../analysis/optimize.py)；[analysis/walk_forward.py:86](../analysis/walk_forward.py)；[analysis/walk_forward.py:170](../analysis/walk_forward.py)；[analysis/walk_forward.py:281](../analysis/walk_forward.py)；[strategies/trend_breakout.py:483](../strategies/trend_breakout.py)；[tests/test_walk_forward.py:188](../tests/test_walk_forward.py)。
- 验证方式：静态校验候选grid、切片与策略入口条件；未运行完整优化矩阵。 Git边界：缺口已在HEAD；trend_breakout.py本地调整保留该warmup条件。
**MAIL-31-2 · P1 · 已修复** — [Restart the TTL clock after partial-fill progress stops](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#pullrequestreview-5087145892)

- 当前核对：改为按总 matchable-bar age 硬TTL，部分成交不再重置寿命；虽不是邮件建议的 idle-progress 实现，但消除了无限保留预留/锁的原始问题，且明确记录了语义。
- 代码/既有测试：[core/broker/matching.py:398](../core/broker/matching.py)；[core/broker/matching.py:406](../core/broker/matching.py)；[core/broker/matching.py:412](../core/broker/matching.py)；[tests/test_order_ttl_and_funnel.py:126](../tests/test_order_ttl_and_funnel.py)；[tests/test_order_ttl_and_funnel.py:142](../tests/test_order_ttl_and_funnel.py)。
- 验证方式：本次离线 pytest TestPartialFillsDoNotResetTheClock 两个测试通过，覆盖部分成交仍到期与同bar多pass只计龄一次。 Git边界：修复和测试均已提交，无本地-only 修复。
**MAIL-31-3 · P2 · 仍存在** — [Restrict funnel groups to entry signal chains](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#pullrequestreview-5087145892)

- 当前核对：仍为任意correlation创建group并计退出链；新增说明承认order_created可超过risk_evaluated，但没有仅筛入场或拆开退出。测试甚至认可退出导致后段更大，未实现邮件要求。
- 代码/既有测试：[core/metrics/attribution.py:68](../core/metrics/attribution.py)；[core/metrics/attribution.py:83](../core/metrics/attribution.py)；[core/metrics/attribution.py:95](../core/metrics/attribution.py)；[tests/test_order_ttl_and_funnel.py:285](../tests/test_order_ttl_and_funnel.py)。
- 验证方式：离线实际 calculate_signal_funnel：1条开仓风险评估+1条退出order_intent，order_created=2、risk_approved=1，转化率200%。 Git边界：当前实现与接受该行为的测试均已提交。
**MAIL-31-4 · P2 · 仍存在** — [Reject overlapping test windows before stitching returns](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#pullrequestreview-5087145892)

- 当前核对：WalkForwardConfig未拒绝step<test_size；split仅要求step>=1，procedure仍concat全量收益，重叠时间无去重。测试仅覆盖默认非重叠几何。
- 代码/既有测试：[analysis/walk_forward.py:94](../analysis/walk_forward.py)；[analysis/walk_forward.py:313](../analysis/walk_forward.py)；[analysis/research_validation.py:134](../analysis/research_validation.py)；[tests/test_walk_forward.py:177](../tests/test_walk_forward.py)。
- 验证方式：离线实际配置/分窗：test_size=10、step=5被接受，首两测试窗[15,25)、[20,30)重叠。 Git边界：缺口位于已提交实现，无本地修复。

### PR #32

**MAIL-32-1 · P1 · 仍存在** — [Reset the loss streak after probation passes](https://github.com/user-nmmmmm/QuantTradingV2/pull/32#pullrequestreview-5090380872)

- 当前核对：probation 通过转 ACTIVE 后未将通过样本加入 streak baseline；ACTIVE 分支只清空时间字段。已有通过测试采用连续盈利样本，遗漏正总R但尾部连续亏损场景。
- 代码/既有测试：[core/strategy_health.py:487](../core/strategy_health.py)；[core/strategy_health.py:491](../core/strategy_health.py)；[core/strategy_health.py:560](../core/strategy_health.py)；[core/strategy_health.py:602](../core/strategy_health.py)；[tests/test_sr1_strategy_health.py:152](../tests/test_sr1_strategy_health.py)；[tests/test_sr1_strategy_health.py:172](../tests/test_sr1_strategy_health.py)。
- 验证方式：已用实际 StrategyHealthMachine 离线复现：阈值2、probation样本3个(+10R,-1R,-1R)，通过评估为ACTIVE，随后无新样本再次evaluate即COOLDOWN。 Git边界：缺口已在HEAD；当前未提交 cohort迁移改动未修复该通过分支。
**MAIL-32-2 · P1 · 仍存在** — [Normalize symbols before applying the generated universe](https://github.com/user-nmmmmm/QuantTradingV2/pull/32#pullrequestreview-5090380872)

- 当前核对：生成 universe 仍改为连字符符号；apply 仍按原字符串 exact lookup，CLI没有统一 key。现有 PIT 测试未覆盖 slash/hyphen 同一币对混用。
- 代码/既有测试：[scripts/repair_binance_point_in_time_data.py:67](../scripts/repair_binance_point_in_time_data.py)；[core/universe.py:82](../core/universe.py)；[main.py:435](../main.py)；[tests/test_phase2_reproducibility_audit.py:172](../tests/test_phase2_reproducibility_audit.py)。
- 验证方式：已离线复现：membership BTC-USDT，data_map BTC/USDT，apply 返回空映射。 Git边界：缺口已提交；main.py 本地其它修改未解决。

### PR #33

**MAIL-33-1 · P1 · 仍存在** — [Persist the breaker day atomically with its checkpoint](https://github.com/user-nmmmmm/QuantTradingV2/pull/33#pullrequestreview-5124762654)

- 当前核对：checkpoint 和 circuit_breaker_day 仍为独立 StateStore.set 事务；重启恢复仍根据独立 day 字段重置 daily breaker。checkpoint 内没有将交易日纳入原子记录。现有 restart 测试覆盖完成 checkpoint 后的重启，不覆盖两次写入间崩溃。
- 代码/既有测试：[live_trading/tick_orchestrator.py:220](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:247](../live_trading/tick_orchestrator.py)；[live_trading/engine.py:304](../live_trading/engine.py)；[live_trading/engine.py:308](../live_trading/engine.py)；[core/state_store_v2.py:43](../core/state_store_v2.py)；[tests/test_drawdown_recovery.py:94](../tests/test_drawdown_recovery.py)。
- 验证方式：静态检查写入/恢复/事务边界；未做进程崩溃注入测试。 Git边界：HEAD 与工作区均存在此边界；tick_orchestrator.py 有其它本地改动。
**MAIL-33-2 · P2 · 仍存在** — [Clear RISK_HALTED when cooldown recovery succeeds](https://github.com/user-nmmmmm/QuantTradingV2/pull/33#pullrequestreview-5124762654)

- 当前核对：健康状态先于 breaker 自动恢复计算；后续仅 breaker truthy 分支赋值 RISK_HALTED，没有恢复成功后同 tick刷新 HEALTHY 的分支。本地新增 risk_actions 仅写 DEGRADED，也未修复此状态显示问题。
- 代码/既有测试：[live_trading/tick_orchestrator.py:179](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:217](../live_trading/tick_orchestrator.py)；[live_trading/tick_orchestrator.py:249](../live_trading/tick_orchestrator.py)；[live_trading/engine.py:206](../live_trading/engine.py)；[tests/test_drawdown_recovery.py:39](../tests/test_drawdown_recovery.py)。
- 验证方式：静态检查全部 _operational_state 赋值；现有冷却恢复测试断言 risk action/entry permission，未断言 live 导出状态。未运行完整 live tick。 Git边界：缺口 HEAD 与工作区均存在；本地 risk lifecycle 工作未处理此项。

### PR #34

**MAIL-34-1 · P1 · 仍存在** — [Reject untracked files before accepting a commit](https://github.com/user-nmmmmm/QuantTradingV2/pull/34#pullrequestreview-5125020002)

- 当前核对：验收仍仅使用 git diff --name-only HEAD 判断干净树，未检查 untracked；后续直接运行 pytest，可能加载未跟踪 conftest/module。现有测试只覆盖交付ZIP，不覆盖源码净化。
- 代码/既有测试：[scripts/main_acceptance.py:37](../scripts/main_acceptance.py)；[scripts/main_acceptance.py:38](../scripts/main_acceptance.py)；[scripts/main_acceptance.py:44](../scripts/main_acceptance.py)；[tests/test_main_acceptance_archive.py:25](../tests/test_main_acceptance_archive.py)。
- 验证方式：静态确认 git diff 检查与未跟踪文件缺失检查；未运行会生成验收产物的完整 acceptance。 Git边界：缺口位于已提交脚本，未见本地修复。

### PR #35

**MAIL-35-1 · P1 · 已修复** — [Require the complete 60-symbol universe](https://github.com/user-nmmmmm/QuantTradingV2/pull/35#pullrequestreview-5125276758)

- 当前核对：原下载/研究脚本已要求 len(expanded)==60，否则在运行比较前失败；新增 revalidation runner 也有缺币 fail-closed 测试。
- 代码/既有测试：[scripts/run_expanded_universe_backtest.py:192](../scripts/run_expanded_universe_backtest.py)；[scripts/run_expanded_universe_backtest.py:193](../scripts/run_expanded_universe_backtest.py)；[tests/test_expanded_universe_download.py:42](../tests/test_expanded_universe_download.py)；[tests/test_revalidation_data_protocol.py:33](../tests/test_revalidation_data_protocol.py)。
- 验证方式：静态确认严格60/60 gate；本次 test_predeclared_unique_sixty_symbols 通过。缺币 runner pytest 因临时目录权限无法完成，未声称运行通过。 Git边界：严格 gate 与相关测试已提交，无本地-only 修复。
**MAIL-35-2 · P2 · 仍存在** — [Reject incomplete daily candles](https://github.com/user-nmmmmm/QuantTradingV2/pull/35#pullrequestreview-5125276758)

- 当前核对：仍只保存 kline 前6列并按 opening timestamp 筛选，忽略 close time，也不限制 --end 必须在当前 UTC session 之前。现有下载测试只验证请求区间，不验证是否收盘。
- 代码/既有测试：[scripts/run_expanded_universe_backtest.py:72](../scripts/run_expanded_universe_backtest.py)；[scripts/run_expanded_universe_backtest.py:78](../scripts/run_expanded_universe_backtest.py)；[tests/test_expanded_universe_download.py:28](../tests/test_expanded_universe_download.py)。
- 验证方式：已全 mock 离线调用实际 fetch_symbol，提供尚未收盘的2030年31根日线，函数接受31行并保留2030-01-31；网络与CSV写出均已 mock。 Git边界：缺口位于已提交脚本。

### PR #36

**MAIL-36-1 · P1 · 仍存在** — [Retry BLOCK_NEW reductions for missing-bar positions](https://github.com/user-nmmmmm/QuantTradingV2/pull/36#pullrequestreview-5125488681)

- 当前核对：force_liquidate 遇无 bar 的持仓直接 continue，不创建残余退出订单；engine 随即将 action 记为已应用，后续非零剩余比例动作不会为该 symbol 重试。已有 block_new 测试验证可管理持仓，但未覆盖 union 缺 bar 后恢复。
- 代码/既有测试：[backtest/engine.py:488](../backtest/engine.py)；[backtest/engine.py:495](../backtest/engine.py)；[core/broker/liquidation.py:55](../core/broker/liquidation.py)；[core/broker/liquidation.py:57](../core/broker/liquidation.py)；[tests/test_breaker_lifecycle.py:38](../tests/test_breaker_lifecycle.py)。
- 验证方式：静态追踪 engine→force_liquidate→后续 action 判定已确认缺口；未运行完整多 symbol 缺 bar 集成复现。 Git边界：HEAD 已存在该缺口；backtest/engine.py 其它未提交修改未解决。
**MAIL-36-2 · P2 · 仍存在** — [Include entry observations in empty backtest results](https://github.com/user-nmmmmm/QuantTradingV2/pull/36#pullrequestreview-5125488681)

- 当前核对：empty_result 仍无 entry_observations，而 run_arm 审计路径直接索引该字段；正常结果才有该字段。未找到空数据结果包含该键的专项测试。
- 代码/既有测试：[backtest/engine.py:265](../backtest/engine.py)；[backtest/engine.py:279](../backtest/engine.py)；[backtest/engine.py:831](../backtest/engine.py)；[scripts/run_expanded_universe_backtest.py:114](../scripts/run_expanded_universe_backtest.py)。
- 验证方式：已离线调用 BacktestEngine().run({}, routing_log_enabled=False)，entry_observations in result 为 False。 Git边界：缺口 HEAD 与当前工作区均存在。

### PR #37

**MAIL-37-1 · P1 · 证据不足** — [Retry partial reductions until the target is reached](https://github.com/user-nmmmmm/QuantTradingV2/pull/37#pullrequestreview-5149579832)

- 当前核对：引擎确实仍提前记录 applied action，未按目标数量追踪完成；但真实 Broker 将未完成的减仓 GTC 单保留在 active_orders，普通后续撮合会继续成交。因此邮件所述“流动性不足后永久不再减仓”在当前常规路径未复现，不宜直接当作仍存在的已确认缺陷，也不能仅凭代码形状标成修复。所列 partial-risk 测试属于 live 路径，不能代替 backtest 集成覆盖。缺 bar 的明确缺口另列 MAIL-36-1。
- 代码/既有测试：[backtest/engine.py:489](../backtest/engine.py)；[backtest/engine.py:495](../backtest/engine.py)；[core/broker/liquidation.py:59](../core/broker/liquidation.py)；[core/broker/matching.py:65](../core/broker/matching.py)；[core/broker/matching.py:375](../core/broker/matching.py)；[tests/test_revalidation_execution.py:193](../tests/test_revalidation_execution.py)。
- 验证方式：已做离线真实 Broker 复现：原仓10，目标5，每 bar 容量1；首次降至9、保留剩余4的 GTC 单，后续4 bar自动降至5。未运行完整策略/保护单干扰场景。 Git边界：GTC 残单保留与引擎 once-only 条件均已在 HEAD；未发现可指认的专门修复提交。
**MAIL-37-2 · P1 · 仍存在** — [Preserve the final bar's remaining volume budget](https://github.com/user-nmmmmm/QuantTradingV2/pull/37#pullrequestreview-5149579832)

- 当前核对：期末强平仍复制最后完整成交量并改为新 synthetic timestamp，触发按 timestamp 重置预算。已有测试覆盖同 timestamp 多 pass 和期末费用，未覆盖这两条路径组合。
- 代码/既有测试：[backtest/engine.py:1090](../backtest/engine.py)；[backtest/engine.py:1091](../backtest/engine.py)；[core/broker/matching.py:435](../core/broker/matching.py)；[core/broker/matching.py:439](../core/broker/matching.py)；[tests/test_backtest_stop_pass_and_liquidity.py:179](../tests/test_backtest_stop_pass_and_liquidity.py)；[tests/test_end_of_backtest.py:94](../tests/test_end_of_backtest.py)。
- 验证方式：已做离线内存复现：真实 bar 剩余预算0，复制后 timestamp +1微秒，相同 bar 的可成交预算重置为1。 Git边界：HEAD 已存在该缺口；backtest/engine.py 的其它本地修改未修复。
**MAIL-37-3 · P1 · 已修复** — [Restore reservations from the persisted reference price](https://github.com/user-nmmmmm/QuantTradingV2/pull/37#pullrequestreview-5149579832)

- 当前核对：恢复预留时已优先使用持久化 intent.reference_price，回归测试也覆盖 reference_price 持久化及重建后的正 pending notional。
- 代码/既有测试：[core/live_broker/__init__.py:170](../core/live_broker/__init__.py)；[tests/test_entry_risk_contract.py:194](../tests/test_entry_risk_contract.py)；[tests/test_entry_risk_contract.py:202](../tests/test_entry_risk_contract.py)；[tests/test_entry_risk_contract.py:207](../tests/test_entry_risk_contract.py)。
- 验证方式：已独立离线内存调用实际 LiveBroker._restore_reservations：price=None、reference_price=100、qty=1，生成1个 reservation 事件，pending_notional=100。现有 pytest 用例因临时目录权限错误未完成，不能声称其通过。 Git边界：修复代码与回归测试均已提交，相关文件没有工作区修改。

### PR #38

**MAIL-38-1 · P2 · 仍存在** — [Use the resolved broker slippage in the budget](https://github.com/user-nmmmmm/QuantTradingV2/pull/38#pullrequestreview-5246104970)

- 当前核对：bind() 没有从绑定 Broker 同步已解析滑点，成本仍读取原始 execution.slippage_bps。现有测试只验证 engine 的默认滑点，没有覆盖 budget 与覆盖值一致性。
- 代码/既有测试：[core/risk/drawdown_budget.py:64](../core/risk/drawdown_budget.py)；[core/risk/drawdown_budget.py:87](../core/risk/drawdown_budget.py)；[backtest/engine.py:141](../backtest/engine.py)；[backtest/engine.py:198](../backtest/engine.py)；[composition/factory.py:144](../composition/factory.py)；[tests/test_backtest_engine.py:55](../tests/test_backtest_engine.py)。
- 验证方式：已做离线内存复现：broker.slippage=0.05、预算配置=1bp、数量1价格100时预算成本仅0.01（应按已解析滑点计5，不含其它成本）。 Git边界：HEAD 已存在该缺口；相关配置/引擎文件有其它未提交修改，未解决此项。
**MAIL-38-2 · P2 · 仍存在** — [Preserve price-less market-order support in the risk guard](https://github.com/user-nmmmmm/QuantTradingV2/pull/38#pullrequestreview-5246104970)

- 当前核对：check_intent 只使用 intent.reference_price/price，未回退当前 symbol mark；Broker 仍声明市场单 price 可以为 None。未找到针对有 mark 的无价市场单预算验收测试。
- 代码/既有测试：[core/risk/drawdown_budget.py:178](../core/risk/drawdown_budget.py)；[core/broker/matching.py:58](../core/broker/matching.py)；[core/broker/matching.py:74](../core/broker/matching.py)。
- 验证方式：已做离线内存复现：guard current mark=100，market intent 未带 price/reference_price，返回 missing_sizing_reference。 Git边界：相关实现已提交；缺口未修复。

## 8. 全部45封Codex邮件索引

日期按UTC列示；每个“邮件”链接打开对应Gmail会话。“摘要”仅表示审查流程通知，不新增缺陷。PR #24的已收到摘要仍显示Running，PR #30摘要显示Completed；两者都不代表项目验收结论。

| UTC日期 | PR | 类型 | 对应具体意见数 | 邮件与原文 |
| --- | ---: | --- | ---: | --- |
| 2026-08-08 19:02 | 4 | 具体审查意见 | 5 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/19fe2c1bced78d32) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/4#pullrequestreview-4889477929) |
| 2026-08-22 17:06 | 9 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a02a70299f1110e) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/9#pullrequestreview-5000638067) |
| 2026-08-22 20:14 | 10 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a02b1be8ad56f7d) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/10#pullrequestreview-5000967232) |
| 2026-08-23 07:45 | 11 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a02d94cc4bbcb96) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/11#pullrequestreview-5001918007) |
| 2026-08-23 08:32 | 12 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a02dbffa40b1065) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/12#pullrequestreview-5001994647) |
| 2026-08-24 07:18 | 13 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a032a2aa81b7066) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/13#pullrequestreview-5005367713) |
| 2026-08-25 14:03 | 15 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a0393b4b7581a5c) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/15#pullrequestreview-5019831836) |
| 2026-08-26 05:45 | 16 | 具体审查意见 | 4 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a03c9a15252cd08) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/16#pullrequestreview-5027130456) |
| 2026-08-26 08:52 | 17 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a03d457a7ef033c) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/17#pullrequestreview-5028474123) |
| 2026-08-26 14:00 | 18 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a03e5f0dec59b1e) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/18#pullrequestreview-5031311051) |
| 2026-08-26 15:04 | 19 | 具体审查意见 | 6 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a03e9990378d4e3) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/19#pullrequestreview-5032066890) |
| 2026-08-26 15:54 | 20 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a03ec7aa5b8fd35) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/20#pullrequestreview-5032477893) |
| 2026-08-28 15:41 | 21 | 具体审查意见 | 4 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a049087b39acea3) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/21#pullrequestreview-5052647523) |
| 2026-08-30 15:12 | 22 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a0533a93285643a) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/22#issuecomment-5469492090) |
| 2026-08-30 15:17 | 22 | 具体审查意见 | 1 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a0533a93285643a) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/22#pullrequestreview-5061172508) |
| 2026-08-30 15:54 | 23 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05360bc455bee6) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/23#issuecomment-5469707798) |
| 2026-08-30 15:57 | 23 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05360bc455bee6) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/23#pullrequestreview-5061253521) |
| 2026-08-31 06:19 | 24 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05678bc4e86b9e) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/24#issuecomment-5474525924) |
| 2026-08-31 07:19 | 25 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a056b03f46edc22) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/25#issuecomment-5475084244) |
| 2026-08-31 07:24 | 25 | 具体审查意见 | 1 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a056b03f46edc22) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/25#pullrequestreview-5064031605) |
| 2026-08-31 09:40 | 26 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a057317a5d64997) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/26#issuecomment-5476548528) |
| 2026-08-31 09:45 | 26 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a057317a5d64997) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/26#pullrequestreview-5065127391) |
| 2026-09-01 04:23 | 27 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05b34da3e57df6) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/27#issuecomment-5488852225) |
| 2026-09-01 04:26 | 27 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05b34da3e57df6) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/27#pullrequestreview-5074006738) |
| 2026-09-01 06:06 | 28 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05b938ac2335df) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/28#issuecomment-5489646520) |
| 2026-09-01 06:09 | 28 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05b938ac2335df) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/28#pullrequestreview-5074542286) |
| 2026-09-01 13:11 | 29 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05d183fbccf8d3) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/29#issuecomment-5494456051) |
| 2026-09-01 13:14 | 29 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05d183fbccf8d3) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/29#pullrequestreview-5078459446) |
| 2026-09-01 14:35 | 30 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a05d656bd3d0655) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/30#issuecomment-5495596418) |
| 2026-09-02 07:59 | 31 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a06121ba26be8a2) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#issuecomment-5506411860) |
| 2026-09-02 08:06 | 31 | 具体审查意见 | 4 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a06121ba26be8a2) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/31#pullrequestreview-5087145892) |
| 2026-09-02 13:37 | 32 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a06256a5f1c91e2) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/32#issuecomment-5510416199) |
| 2026-09-02 13:41 | 32 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a06256a5f1c91e2) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/32#pullrequestreview-5090380872) |
| 2026-09-06 08:37 | 33 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a075dd685d2850b) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/33#pullrequestreview-5124762654) |
| 2026-09-06 08:37 | 33 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a075dd685d2850b) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/33#issuecomment-5558074781) |
| 2026-09-06 10:09 | 34 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a07631c34ed8774) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/34#issuecomment-5558526706) |
| 2026-09-06 10:12 | 34 | 具体审查意见 | 1 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a07631c34ed8774) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/34#pullrequestreview-5125020002) |
| 2026-09-06 12:16 | 35 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a076a5a3d38db27) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/35#issuecomment-5559145714) |
| 2026-09-06 12:19 | 35 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a076a5a3d38db27) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/35#pullrequestreview-5125276758) |
| 2026-09-06 13:40 | 36 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a076f2f7fc308cb) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/36#issuecomment-5559599421) |
| 2026-09-06 13:45 | 36 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a076f2f7fc308cb) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/36#pullrequestreview-5125488681) |
| 2026-09-09 03:50 | 37 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a08449bfeb8d915) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/37#issuecomment-5595496984) |
| 2026-09-09 04:00 | 37 | 具体审查意见 | 3 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a08449bfeb8d915) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/37#pullrequestreview-5149579832) |
| 2026-09-18 08:52 | 38 | 审查摘要 | 0 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a0b3b74235a6a3e) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/38#issuecomment-5727605674) |
| 2026-09-18 09:06 | 38 | 具体审查意见 | 2 | [邮件](https://mail.google.com/mail/?authuser=mnm799063%40gmail.com#all/1a0b3b74235a6a3e) · [GitHub原文](https://github.com/user-nmmmmm/QuantTradingV2/pull/38#pullrequestreview-5246104970) |

## 9. 文档回写边界

本次更新统一Roadmap第3节当前状态与第8节下一顺序，新增本文和JSON逐条清单。领域计划旧勾选没有批量改为完成，原验收标准没有降低。历史邮件/旧计划与现在代码不一致的地方，以上述实际证据和边界解释为准。
