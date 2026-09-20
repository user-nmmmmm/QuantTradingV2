# S3 波动率目标与分批仓位：本地工程能力边界

生效日期：2026-09-20。对应 [统一 Roadmap 第 6 节](unified_roadmap.md#6-策略与能力支线)、[SYS-13](development_details.md#sys-13)，并依赖 SYS-02 / SYS-07 / SYS-08 / FIX-08。此记录描述新增的纯决策能力及专项验收，不替代现有风险批准、预算预留、交易账本、健康裁决或实际执行。

实现入口为 [core/target_position.py](../core/target_position.py)，专项验证为 [tests/test_s3_target_position.py](../tests/test_s3_target_position.py)。现有策略和引擎没有自动启用本模块；波动率策略 `VolatilityPolicy.enabled` 默认为 `False`。

## 输入、输出和公式

| 接口 | 输入 | 输出与不变量 |
| --- | --- | --- |
| `volatility_target_weight` | 原始目标权重、带时区且已完成的简单收益、决策时间、冻结波动率参数 | 严格使用早于决策时间的最近窗口；样本标准差乘年化周期数平方根；倍率为 `min(maximum_multiplier, target_annual_volatility / estimated_volatility)` |
| `VolatilityPolicy.rebalance_due` | 当前时间、上次再平衡时间 | 按预声明秒数判断，未来的上次时间为错误；由调用方执行调度 |
| `constrain_target_weights` | S2 的非负总敞口目标权重、组合/单标的上限、现有 `CorrelationClusterPolicy` | 先约束单标的，再按簇及总敞口同比例缩量；未知资产沿用既有默认簇，不给独立风险额度 |
| `freeze_target` | account + symbol + side + position_id、当前数量、目标权重、权益、批准参考价/原止损/金额风险、剩余新增名义额度、单标的名义上限、每批上限、交易所规则 | 固定目标数量和生命周期；剩余额度必须已扣除现仓及在途预留，由现有风险管理器提供 |
| `next_tranche` | 冻结目标、当前已对账仓位/完整订单事实、当期剩余预算、有效止损、风险门控和交易所规则 | 输出一批 buy / short / sell / cover，或带原因的 HOLD；不发送订单，不预留或释放资金 |

简单收益必须来自预声明频率。默认观察间隔为 86,400 秒，年长度 `annualization_days` 默认为 365；年化周期数自动按 `annualization_days × 86400 / observation_interval_seconds` 推导。小时频率对应 8,760，日频对应 365；显式传入矛盾的 `periods_per_year` 会报错。参数须提前冻结，不从收益表现选择。窗口内相邻时间戳须严格满足 `observation_interval_seconds`，缺期或不规则间隔返回 `blocked / irregular_history`。样本不足、重复时间戳、过期、无效收益、零/过大/非有限波动率都返回零目标权重，波动率为 `null`，同时保留状态和原因。最终同一时点和未来收益不进入估计。

默认关闭时原始权重原样返回，不依赖波动率样本。启用时输出的零权重是保守目标建议；减仓仍必须经过现有事实、订单、保护单和执行流程，不能把建议当作已经成交。

单标的目标先由 `equity × target_weight / reference_price` 生成，受最大持仓名义金额约束。新增部分额外受剩余名义预算及不可变原批准金额风险约束，最后按交易所数量步长向下取整。零新增额度不会因为既有仓位不在数量网格上而意外触发减仓。`remaining_entry_notional` 必须取现金、组合杠杆、相关簇及换手剩余额度的最小值，不能由候选策略自行忽略其中任一项。

当前版本支持数量为基础资产单位的现货/保证金候选，明确拒绝衍生品和非单位 `contract_size`。保证金账户的可借额度与方向合法性仍由原账户/交易所检查控制；本模块不改变融资、杠杆和强平合同。

## 分批、保护和恢复

一个目标只沿原始方向推进：开仓目标不反向减仓，减仓目标不因权益/健康恢复而重新加仓。每一批在新鲜剩余预算和交易所单笔数量/名义限制下再次缩量，缩量后的批准风险为 `quantity × abs(original_reference_price - original_stop)`，不能修改原订单的批准金额。

新增风险要求严格布尔门控 `allows_new_risk=True`、事实已对账、有效保护止损存在且未比原止损放松。当前价格已经穿越有效止损，或每单位当前止损风险大于原批准参考风险时，保守阻止新增风险，等待另行批准的新计划。委托后才发生的跳空继续由现有 `evaluate_fill_risk` 和保护单机制处理，不能保证按理想止损成交。

`TrancheFact` 必须包含该目标全部历史委托的连续序号、原请求数量、累计成交数量及权威状态。UNKNOWN、撤单待确认、部分成交和其他非终态都会等待；取消/拒绝后仅在权威终态且已对账时生成下一批。累计成交始终消耗目标原容量，即使后来止损或其他动作减仓，也不重新使用旧目标已消耗的入场批准风险。

每批 `decision_id` 由冻结目标摘要和序号确定。相同输入或重启重放输出同一 ID；调用方必须在发送前把该 ID 与原订单唯一身份持久登记，并继续使用现有订单流水的幂等机制。决策模块不能独立保证两个并发执行器不会重复下单。所有未知外部/保护委托应先通过 `has_unresolved_orders` 阻塞，再交给原执行协调器取消、查询或重保护。

冻结目标通过 `checkpoint()` 输出版本 1 JSON 及内容摘要；使用原 `StateStore` 持久化，再通过 `FrozenTarget.restore()` 校验并恢复。同一目标身份不得在恢复时换成新权益计算的目标。该摘要用于内容一致性，不是审批签名。恢复缺失任何权威订单或持仓事实时，调用方必须设 `facts_reconciled=False`；不得把缺失流水解释为从未下单。

旧仓位已平、仓位 ID/账户/方向发生变化时旧目标不执行。首次从空仓建立目标需要由外层生命周期流程建立并持久映射待开仓身份与实际首个成交的 position ID；本次真实 Broker 集成使用已确认的现仓 position ID 验证分批加仓，不声称已解决所有 flat→new/人工替换身份迁移。

## 专项证据

2026-09-20，命令：

```text
.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_s3_target_position.py -q
```

结果：45 passed。覆盖内容：

- 固定权重默认不变；因果窗口；波动率翻倍对应目标仓位减半；预声明再平衡间隔；小时/日频年化手算与矛盾参数拒绝。
- 样本不足、缺期、重复、过期、NaN/inf、非法简单收益、零与发散波动率拒绝新增。
- 相关簇/单标的/组合权重上限；相同市场不同符号写法不能重复申请单标的额度；批准风险/当期预算/数量步长/最小交易额和最大单笔限制。
- 原批准预算在权益增加、恢复和数量精度处理后不扩大；已有止损不得放松，价格穿越及已知风险超限不新增。
- UNKNOWN/部分成交/撤单待确认阻塞；终态取消后剩余目标继续；已成交额度不因后续减仓而循环使用；生命周期/账户/方向变化隔离。
- SQLite 保存、关闭、重新打开、校验恢复后相同分批决策；修改目标内容而保留旧摘要会失败。
- 使用项目真实 `Broker` 和 `Portfolio` 完成 1 单位既有持仓至 9 单位目标的分批加仓，包含部分成交、重复提交和批准风险进入 lot 的对账；新增成交 8、新增批准风险 80、含原仓风险 90、现金 9,100，单一 position ID 保持。

测试不包含真实市场数据、真实交易所或连续运行。45 项通过只证明上述本地行为，不证明策略收益、实现波动率在市场中稳定收敛或实盘准入。

## 仍待闭环

SYS-13 应记录为“部分实现待验收”。须继续取得 SYS-02 / SYS-07 / SYS-08 / FIX-08 的整体证据，并完成以下事项后再评估正式启用：

1. 将 S2 完整 PIT 目标、现仓、在途预算和换手限制接入统一调度；在全部回测/回放/sandbox 路径验证默认关闭时行为不变。
2. 统一首次开仓身份分配与目标到订单 ID 的持久映射；本模块不会推断缺失身份或自动迁移旧目标。保护单同向整仓替换、epoch 清理与重启棘轮的本地修复见下节；仍需在整体运行链路完成外部仓位对账和真实迁移验收。
3. 多资产协方差/相关性估计、组合风险贡献、有效独立头寸及共同跳空/相关性跃迁/容量压力测试。当前沿用保守簇约束，没有把缺失协方差当作零风险，没有声称实现动态协方差模型。
4. 基于预注册真实数据与成本的独立研究，检验目标波动率区间、再平衡频率、换手和成本；不能用本地手算/合成样本测试替代研究准入。
5. 运行批准、连续观察、逐笔对账和受控灰度仍按主路线图门槛。当前 paused_revalidation、人工锁、既有健康阈值及前瞻协议边界均不变。

## PM1 追加修复：保护止损绑定真实仓位生命周期

本轮同时关闭一个已确认的共享管理缺口：保护单管理器原先只按 symbol 保存棘轮；若两次观察之间旧仓已全部退出并同向重新建立，且未观察到中间 flat，新仓会继承旧 stop。现有 lot 账本已能区分这两个生命周期，本次将该权威身份接入共享保护决策。

修改文件为 [共享保护管理器](../core/protective_orders.py)、[回测保护适配器](../backtest/protective_stops.py)、[live 保护调用](../live_trading/tick_orchestrator.py)。新专项为 [test_protective_position_epoch.py](../tests/test_protective_position_epoch.py)。未改变健康阈值、融资模式、策略入场或人工锁。

- 回测与 live 从 `Portfolio.open_lots` 读取 `position_id` 集合，并先验证有符号 lot 数量与权威净仓一致；缺失/不一致事实不按数量或方向猜身份。live 进入 DEGRADED 并报警，回测明确失败。
- 旧新身份完全不相交或方向变化时清除旧棘轮；仍有交集的部分退出/加仓保持单调保护。全部数量被在途退出预留不等于已确认 flat，保留原身份和棘轮。
- 新保护单在既有不可变 `OrderIntent.causation_id` 中保存 `protective-position-v1` 引用；没有新增资金账本或独立持仓事实。
- 带归属的旧保护单必须先取消，确认取消并同步后重新检查当前 epoch，再向新仓挂新止损；取消未知或同步失败时不补单。风险退出同样绑定原位置身份。
- 重启从原订单账本读取属于当前 epoch、当前方向的已确认保护价格；即使崩溃发生在取消已确认但替换尚未提交之间，也不放松原止损。PLACE 的持久动作身份包含原保护委托谱系，避免同一 bar 恢复时复用已取消的旧订单 ID。
- 旧直接调用未提供身份时保留兼容 API；实际回测/live 调用必须提供经过核对的身份。遗留无归属的 live 保护单不能仅因当前策略建议更低的止损就放松：会记录 `legacy_protective_position_unverified` 并走现有受保护退出流程。只有保持或收紧原保护的取消确认迁移才会重建带身份保护。

2026-09-20 验证：

```text
.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_protective_position_epoch.py tests/test_sr2_protective_orders.py tests/test_sr2_protective_stops.py tests/test_sys_local_closure.py tests/test_live_risk_action_lifecycle.py tests/test_revalidation_execution.py tests/test_s3_target_position.py -q
```

结果为 **197 passed**。此外对新增/修改源码及专项测试执行 Ruff，结果通过。证据含真实回测 Broker 的未观察 flat 同向换仓、真实本地 LiveBroker/OrderStore 的身份持久化、独立重开订单数据库、取消后重启保持原止损、UNKNOWN/取消竞态和缺失 lot 事实反例。交易所为离线故障夹具，不能替代真实连续运行。SYS-07 / PM1 的这个已知缺口已有本地工程证据；整个 SYS-07 的准入、跨模式全量差异和实际外部账户迁移仍服从主验收索引。
