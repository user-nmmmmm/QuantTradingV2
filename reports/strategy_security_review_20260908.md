# 当前策略代码与交易执行审查

审查日期：2026-09-08。源码基准：`1a659b4929435e8dee38962812a858a2ebcd32b5` 的当前工作目录。用户问题：当前策略代码存在什么安全问题，策略交易还存在什么问题。

结论：当前代码尚不满足真实资金放行条件。主要风险集中在保护单、强制风险动作、实盘持仓归属和回测/实盘一致性，而不是已经证明的远程攻击。当前配置的 `TrendBreakout: paused_revalidation` 和官方入口的准入检查确实限制了风险：标准 `--live` 会拒绝当前策略。下述执行缺陷仍影响 sandbox，并会影响未来合法准入后的实盘或直接集成该引擎的调用方。

本报告是静态源码审查，未启动交易程序、未下单、未读取实际凭据、未运行测试或重新回测。完成了独立安全审查、独立架构映射与交易执行专项复核；覆盖并非全仓逐行穷尽。P0/P1/P2 表示本报告的修复优先级，不是漏洞 CVSS 等级。正式安全扫描另外记录了一项条件性、低严重度的凭据日志泄露。

## 1. P0：默认实盘路径无法建立保护止损，拒单被忽略

**确定性代码缺陷，高置信度。**

调用链：

1. `LiveBroker` 默认使用 `ExchangeCapabilities.from_ccxt()` 创建交易边界。
2. 该能力构造只添加 `market` 和 `limit`。
3. 实盘保护逻辑提交 `order_type="stop"`。
4. `OrderValidator` 拒绝不在能力列表中的订单类型；提交服务把异常转换为持久化的 `REJECTED` 返回值。
5. `_apply_protective_intent()` 不检查返回状态；非终态查询也不会把已经拒绝的保护单交回管理器。

结果：有仓位、有止损计划，不代表交易所有止损订单。拒单不抛异常时，代码没有执行注释宣称的保护失败平仓流程。

证据：[能力构造](D:/QuantTradingV1/core/exchange/metadata.py:89)、[类型校验](D:/QuantTradingV1/core/exchange/validation.py:45)、[拒单转换](D:/QuantTradingV1/core/live_broker/submission.py:105)、[忽略结果的保护单提交](D:/QuantTradingV1/live_trading/tick_orchestrator.py:547)。

修复应同时补齐规范触发价格、交易所条件单映射、能力核验，以及提交/撤单结果驱动的状态转换。只把 `stop` 加入白名单仍不充分：当前 CCXT 映射只有普通 `price`，没有触发价字段。保护状态应在订单确认后才进入 ARMED。

## 2. P0：清仓级熔断没有落实为实盘清仓

**确定性代码缺陷，高置信度。**

`RiskManager` 返回 `force_liquidate`、`force_reduce_fraction` 等结构化风险决策，但实盘 tick 主要使用其布尔值。日亏损触发或 `LIQUIDATE/LOCKED` 时，代码设为 `RISK_HALTED` 后直接返回，没有提交对应强制平仓；`REDUCE` 也没有执行回测中的既有仓位减仓动作。

因此，日志显示“熔断”可以仅意味着停止新增风险，不能证明已有资金敞口已经清除。现有止损若正常存在可以提供部分保护，但与组合清仓不是同一承诺；第 1 项又使该后备保护在默认路径上无法成立。

证据：[实盘熔断与早退](D:/QuantTradingV1/live_trading/tick_orchestrator.py:216)、[风险决策定义](D:/QuantTradingV1/core/risk/circuit_breaker.py:49)、[回测实际强制风险动作](D:/QuantTradingV1/backtest/engine.py:417)。

修复：让实盘显式消费风险动作，执行持久化、幂等的撤销开仓单/减仓/清仓流程，并用账户同步确认最终仓位。保持退出与保护恢复通路在禁开仓状态下可运行。

## 3. P0：实盘持仓未建立批次归属，正常退出和健康统计失去依据

**默认入口调用链中的确定性缺陷，高置信度。**

`run_live.py` 创建空 `Portfolio()`。实盘账户同步只覆盖 `cash`、`positions`；实盘成交写订单/成交事件和兼容 `trades` 列表，没有建立用于策略管理的 `portfolio.lot_books`，也没有生成策略消费的 `close_events`。`RecordedExecutionAdapter` 在实盘不会自动应用成交，离线 `replay(apply_fills=True)` 不能补救官方运行链路。

而 Router 仅从第一笔未平仓 lot 取得开仓策略；没有 lot 就记录 `UNOWNED_POSITION`，不调用该策略的 `process_exit_only()`。最大持仓时长也依赖 lot 入场时间。健康生命周期的实际盈亏观察依赖 `close_events`，相关止损风险预算则依赖 lot 的 `initial_risk`。

后果包括正常 Donchian/状态退出不执行、最长持仓约束失效、实盘平仓未更新健康观察，以及相关止损风险统计漏计。这里不表示所有风险限额失效：基于实际 positions 的名义敞口限制是独立路径。

证据：[账户同步](D:/QuantTradingV1/core/live_broker/account_sync.py:31)、[成交发布](D:/QuantTradingV1/core/live_broker/__init__.py:182)、[Router 归属与退出](D:/QuantTradingV1/router/router.py:94)、[lot 归属查询](D:/QuantTradingV1/router/router.py:162)、[关闭事件消费](D:/QuantTradingV1/strategies/base.py:115)、[相关止损风险计量](D:/QuantTradingV1/core/risk/portfolio_governor.py:134)。

修复：从持久化成交事实重建具有策略、时间、费用、初始止损风险的 lot/close 投影；与交易所余额同步分离现金应用，避免重复记账。未知归属应进入明确恢复流程，不能静默跳过退出。

## 4. P1：紧急平仓与跳空减仓会被参考价校验拒绝

**使用官方 SafeLiveBroker 与 PersistentOrderSafetyGuard 时确定，高置信度。**

保护失败的市价平仓，以及 `GapRiskResize` 市价减仓，都没有传 `price`。持久化安全守卫在判断订单是否属于退出之前，要求所有订单都有正参考价。`SafeLiveBroker` 将这类校验失败转换为结构化拒单。

结果：减仓分支会持续报告失败；保护平仓分支还可能忽略拒单结果。把订单类型设为 market 或标记 reduce-only 并不会自动绕过此检查。

证据：[GapRiskResize](D:/QuantTradingV1/live_trading/tick_orchestrator.py:471)、[紧急平仓](D:/QuantTradingV1/live_trading/tick_orchestrator.py:560)、[参考价要求](D:/QuantTradingV1/core/risk/persistent_guard.py:52)、[守卫失败转换](D:/QuantTradingV1/core/live_broker/safe.py:92)。

修复：显式区分市价订单执行价格与风险参考价；为退出提供可验证参考价和安全减仓规则，检查成交及残余仓位。不要简单关闭所有风险校验。

## 5. P1：保护单更新的身份和状态契约不一致

**源码确认；部分后果在支持止损的边界下才可触发。**

保护单替换先撤旧单，再以默认 `sequence=0` 提交。订单 ID 由交易所、账户、标的、bar、策略、方向和 sequence 确定，不包含价格、数量或订单类型；同一 bar 的不同替换动作可能被当作同一命令重放。旧单撤销后，新的提交可能只返回旧单的 CANCELED 结果。紧急平仓还与止损共用 `ProtectiveStop` 策略身份。

另一个独立适配错误是：真实订单状态为 `partial`、`cancel_pending`，保护管理器却匹配 `partially_filled`、`pending_cancel`。真实部分成交或撤单等待可能被当作没有有效保护。

实际 tick 还会在发现 UNKNOWN 时提前返回，早于保护核验；直接调用保护管理器的 UNKNOWN→FLATTEN 测试不能证明完整引擎能到达该分支。

证据：[替换流程](D:/QuantTradingV1/live_trading/tick_orchestrator.py:543)、[订单身份](D:/QuantTradingV1/core/domain.py:144)、[已有 ID 重放](D:/QuantTradingV1/core/live_broker/submission.py:73)、[规范状态](D:/QuantTradingV1/core/domain.py:20)、[保护状态集合](D:/QuantTradingV1/core/protective_orders.py:69)、[UNKNOWN 早退](D:/QuantTradingV1/live_trading/tick_orchestrator.py:150)。

修复：不同动作分配持久化 generation/sequence；同一动作重试保持 ID；集中使用规范状态枚举；取消确认、替换确认和失败补偿形成完整状态机。

## 6. P1：行情来源没有绑定下单交易所和市场

**确定的配置传递缺口，高置信度。**

`--exchange` 配置了 broker，但初始化和刷新行情没有向 `fetch_ccxt()` 传 `exchange_id`。行情函数按 Binance、OKX、Kraken、Coinbase 顺序尝试，部分后备交易所还会把 USDT 映射为 USD。刷新会将数据按时间拼接并去重，因此跨来源的切换可以进入同一指标序列。

这会让突破、OBV、ATR、定仓参考价来自与执行不同的市场。跨市场行情本身不是必然错误，但这里没有显式的跨来源契约和校验。普通 Yahoo 后备函数要求日期范围；当前实盘调用不提供日期，所以不能声称这条实盘路径已经会使用 Yahoo 数据。

证据：[实盘初始化行情](D:/QuantTradingV1/live_trading/engine.py:346)、[刷新行情调用](D:/QuantTradingV1/core/market_data.py:122)、[来源选择](D:/QuantTradingV1/core/data_fetcher.py:205)、[报价币转换](D:/QuantTradingV1/core/data_fetcher.py:120)。

修复：绑定 exchange/market/symbol/quote，并持久化来源；实盘数据异常时进入明确降级策略，禁止未经验证的来源切换。

## 7. P1：实际 Portfolio 账户模型没有使用已验证的配置

**确定的构造缺口；具体账户偏差依赖交易所余额语义。**

配置选择 `spot_margin`，入口也验证 CLI market type 与配置一致，但随后仍创建默认 `Portfolio()`，其 account_mode 为 SPOT。`get_equity()` 在 SPOT 下计算现金加持仓全部市值，而保证金分支使用现金加未实现盈亏。账户同步又把 margin 与 futures/swap 放在同一个 fetch_positions 路径，没有独立表达现货保证金的资产、负债、利息。

因此“配置校验通过”没有证明实际权益与保证金计量正确。在衍生品配置下，若现金表示保证金余额，默认 SPOT 公式会错误加入全部仓位名义价值；默认 margin 模式仍需核验其余额/负债字段映射。不能只传 account_mode 后就宣称账户语义已修复。

证据：[实际 Portfolio 构造](D:/QuantTradingV1/run_live.py:110)、[默认 SPOT](D:/QuantTradingV1/core/portfolio.py:29)、[不同权益公式](D:/QuantTradingV1/core/portfolio.py:189)、[账户同步分支](D:/QuantTradingV1/core/live_broker/account_sync.py:47)。

修复：按明确的 spot/spot-margin/perpetual 契约构造账户，并用对应交易所事实核对资产、负债、权益、保证金与融资费用。

## 8. P1：UNKNOWN 订单的自动过期可能把“不知道”当作“确定未下单”

**源码确认的恢复风险，触发依赖适配器返回语义。**

`_fetch_exchange_order()` 通过精确查询或列表寻找订单；没有匹配时返回 None，没有提供“列表已完整覆盖”的证明。上层把 None 标记成 `order_not_found_by_client_id`，经过 TTL 后允许自动 `EXPIRED_UNSUBMITTED`。订单列表可能只覆盖近期窗口；不支持查询的适配器与权威不存在也不应使用同一结果。

若此前创建订单响应丢失、交易所实际已接受，下游风险阻断可能在缺乏充分证据时被释放，允许新的风险。实际 CCXT 适配器抛出 NotSupported 时会走保留 UNKNOWN 的异常路径，这构成一部分反证，但不能修复成功返回不完整列表的路径。

证据：[查单](D:/QuantTradingV1/core/live_broker/reconciler.py:326)、[None→确认不存在标签](D:/QuantTradingV1/core/live_broker/reconciler.py:68)、[自动过期](D:/QuantTradingV1/core/live_broker/reconciler.py:127)。

修复：将精确确认不存在、不支持查询、不完整列表、暂时查不到分为不同事实；仅充分确认不存在或独立人工核验才释放 UNKNOWN。

## 9. P2：实盘逐币种分配，回测同时间批量排名

**确定的执行差异，多标的/资金受限时影响明显。**

回测 `EventProcessor.process()` 收集同时间所有候选后一次分配；实盘逐个遍历 `self.symbols`，调用 `process_symbol()`，每次只传一个候选进入 allocator。虽然候选已经有实际 score，实盘仍无法在这一批候选之间比较后再下单。后到的高分候选可能输给先到的低分候选。

证据：[实盘循环](D:/QuantTradingV1/live_trading/tick_orchestrator.py:234)、[单候选分配](D:/QuantTradingV1/core/runtime.py:274)、[批量分配](D:/QuantTradingV1/core/runtime.py:214)、[排序](D:/QuantTradingV1/core/allocation.py:82)。

官方 R8 当前要求单标的，限制了正式灰度阶段的影响；sandbox 和未来多标的阶段仍需修复。改为在统一时间事件中完成全部候选收集、排序与组合预算，再有序提交。

## 10. P2：日线信号收盘价同时承担实时风险估值

实盘 snapshot 使用最近已收盘 K 线的 close，默认 timeframe 是 1d。因此一分钟轮询不等于一分钟更新权益估值。即使每轮成功同步余额，盘中价格变化仍可能无法及时进入本地日亏损/回撤判定。还应区分交易所自有保证金保护与本系统组合风险保护，二者不是相同机制。

证据：[已收盘价格提取](D:/QuantTradingV1/live_trading/tick_orchestrator.py:160)、[snapshot 使用这些价格](D:/QuantTradingV1/live_trading/tick_orchestrator.py:174)、[估值公式](D:/QuantTradingV1/core/valuation.py:8)。

修复：策略继续使用已收盘 bar 防前视，风险估值另用带来源、时间和有效期的实时价格或交易所权益事实。

## 11. 安全漏洞：认证代理地址写入日志

**CWE-532，低严重度，高置信度；需要使用带认证信息的代理。**

`QUANT_PROXY_URL` 或构造参数中的完整 URL 被 INFO 日志原样输出。日志过滤器只覆盖部分交易所凭据和 key/value 形式，不剥离 URL 的 userinfo。获得日志而无权读取运行环境的人，可能取得代理用户名和密码；此项没有证明交易所 API key 泄露。

证据：[日志写入](D:/QuantTradingV1/core/data_fetcher.py:61)、[过滤器](D:/QuantTradingV1/core/logger.py:14)。

修复：只记录代理 scheme/hostname/port，去掉用户名、密码、查询参数和 fragment，并用虚构凭据验证日志脱敏。未检查实际代理配置或复制任何真实凭据。

## 12. 策略研究与运维仍有未闭合事项

- 当前只有上涨趋势启用 TrendBreakout；其余主要市场状态路由到 Cash。不能把注册了四个策略理解成已经具备四种已验证收益来源。[routing](D:/QuantTradingV1/config/params.yaml:85)
- ATR 初始止损与 Chandelier 追踪止损当前均为 false；不能把实现了这些功能理解成当前生产配置已经采用。[stops](D:/QuantTradingV1/config/params.yaml:134)
- 当前研究协议明确没有当前版本独立 holdout 证据；20/10、OBV、健康阈值、冷静期与风险参数仍登记为研究候选。不能根据旧版正收益报告推断这一版策略已经有效，也不应把旧版“永久死亡开关”重复报告成已经修复后代码的当前根因。[研究协议](D:/QuantTradingV1/docs/research/current_strategy_protocol.json:6)、[实验登记](D:/QuantTradingV1/docs/research/current_strategy_experiment_registry.jsonl:12)
- 默认 sandbox/live 共享 reports 下的订单、风险、状态数据库，账户 ID 默认只是 market_type；切换账户/环境复用目录存在状态污染风险。R8 rollback admission 只验证文件存在，并不验证可恢复性或与当前代码/账户的匹配。这些是运维隔离和恢复能力缺口，未建立独立攻击者模型。
- 事件 JSON 解码可以动态导入 class tag 指定模块，异常 traceback 也未走完整脱敏；未建立可利用的外部账本导入链或确定含密异常，所以不把它们写成已证实 RCE/第二项泄露漏洞。

## 修复顺序与验证要求

先修保护单可接受与结果确认、强制风险动作、实盘 lot/close 投影，再修紧急退出参考价、保护单身份状态、账户/行情契约和 UNKNOWN 恢复。保持当前准入暂停状态，随后进行真正经过 SafeLiveBroker、ExchangeBoundary、OrderStore 的离线假交易所联调和 sandbox 故障注入；单独测纯状态机不足以覆盖这些问题。

关键验收应验证结果而不只是调用次数：入场成交后有已确认止损；止损拒单/未知状态时不会继续新增风险；撤换后始终可说明残余保护；日亏损/清仓动作后仓位符合决策；重启能重建策略归属与关闭事件；同一行情/成交事实在回测和实盘投影中得到一致权益、退出和分配。

研究验证应在上述执行与账户契约稳定后重跑，并绑定代码/配置/数据版本。当前证据支持“还不能证明可放行”，不支持对未来收益作确定判断。

## 扫描产物与计量

Codex Security 扫描 `d3a83a86-9760-4c6c-9305-4285ec536f42` 已成功校验、封存和索引，正式安全发现 1 项（low），安全源码覆盖标记为 partial。交易执行缺陷在本文单独说明。插件提示扫描期间工作目录变化；本次新增了本文，结束时 `git diff --stat` 为空，未修改受跟踪的策略或其他源代码。原有未跟踪的 `cua_probe.txt` 保留。

工具返回的处理计量：输入 6,448,354 tokens，其中缓存输入 6,085,760；输出 34,586；总计 6,482,940，涉及 4 个代理任务。该数据是工具的累计处理计量，不是费用估算。

已检查本文 47 个源码链接，目标文件及引用行号均存在。此检查不等同于执行测试。
