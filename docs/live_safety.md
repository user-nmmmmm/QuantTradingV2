# 实盘安全启动与订单生命周期(合并)

> 合并自 G1 安全启动与凭据治理、G2 订单提交幂等与成交事实闭环。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/g1_live_safety.md -->
## G1 安全启动与凭据治理

状态：已实现并自动化验证。本文只说明安全启动边界，不构成真实资金上线批准。

### 已实施的安全边界

- `run_live.py` 不传模式参数时固定进入 sandbox/testnet。
- 真实端点只能通过显式 `--live` 启用；它与 `--sandbox` 互斥。
- CLI 不提供 API key、secret 或 password 参数，避免凭据进入 shell 历史和进程列表。
- 凭据仅从进程环境读取：`EXCHANGE_API_KEY`、`EXCHANGE_SECRET`，以及交易所需要时的 `EXCHANGE_PASSWORD`。外部密钥管理器应在启动前注入环境变量。
- 启动时校验交易所、账户类型、symbol 白名单和 base currency；任何不匹配均停止启动。
- 每张订单在最终提交边界再次检查 kill switch、symbol、正数参考价格、单笔名义金额和每日新增风险。
- 每日新增风险计数持久化在 `reports/live_safety_state.db`；同一风险日内重启不会重置额度。
- 实盘启动必须从交易所取得 API 权限事实，明确确认所需交易权限开启、提现权限关闭；不可验证即停止。
- 日志过滤环境凭据，以及 Authorization、API key、secret、signature 和常见认证头字段。

### 配置

白名单使用英文逗号分隔：

- `QUANT_ALLOWED_EXCHANGES`，安全默认值：`binance`
- `QUANT_ALLOWED_ACCOUNT_TYPES`，安全默认值：`spot`
- `QUANT_ALLOWED_SYMBOLS`，安全默认值：`BTC/USDT,ETH/USDT`
- `QUANT_SANDBOX_MAX_ORDER_NOTIONAL`，sandbox 默认值：`1000`
- `QUANT_SANDBOX_MAX_DAILY_NEW_RISK`，sandbox 默认值：`5000`
- `QUANT_LIVE_MAX_ORDER_NOTIONAL`，实盘必须显式配置为正数
- `QUANT_LIVE_MAX_DAILY_NEW_RISK`，实盘必须显式配置为正数
- `QUANT_KILL_SWITCH`，设为 `1`、`true`、`yes`、`on` 或 `active` 时阻止后续订单

`buy` 和 `short` 计入每日新增风险；降低风险的 `sell` 和 `cover` 不消耗该额度。市价单也必须提供正数参考价格，以便执行名义金额检查。

### Sandbox 启动

PowerShell 示例，凭据必须属于测试环境：

```powershell
$env:EXCHANGE_API_KEY = "<sandbox key>"
$env:EXCHANGE_SECRET = "<sandbox secret>"
$env:QUANT_ALLOWED_EXCHANGES = "binance"
$env:QUANT_ALLOWED_ACCOUNT_TYPES = "spot"
$env:QUANT_ALLOWED_SYMBOLS = "BTC/USDT"
python run_live.py --symbols BTC/USDT
```

不传 `--sandbox` 仍然是 sandbox；可以显式添加它以增强操作可读性。

### 实盘额外门槛

真实端点除了显式 `--live` 和上述检查外，还必须满足：

1. 明确配置两项 `QUANT_LIVE_*` 限额；不存在实盘宽松默认值。
2. 交易所适配器能读取当前 API 权限；无法验证时 fail closed。
3. 所需交易权限明确开启。
4. 提现权限明确关闭；字段缺失或含糊同样拒绝启动。

即使程序检查通过，路线图 G2-G9 和人工放行尚未完成时，也不得使用真实资金。

### 人工启动检查表

- [ ] 本次运行已获得独立的真实资金灰度批准；G2-G9 门槛与未关闭 P0/P1 已复核。
- [ ] 使用专用最小权限凭据；提现关闭，来源 IP 已限制。
- [ ] 交易所、账户类型、base currency 与预批准范围一致。
- [ ] `QUANT_ALLOWED_SYMBOLS` 只包含本次批准的 symbol。
- [ ] 单笔名义金额和每日新增风险是本次明确批准值，不是复制的旧值。
- [ ] `QUANT_KILL_SWITCH` 已现场验证，操作者可立即启用。
- [ ] 日志中没有 key、secret、签名、Authorization、完整请求头或敏感账户载荷。
- [ ] `reports/live_safety_state.db` 可写、已纳入备份，并与当日人工风险台账一致。
- [ ] 账户同步、订单查询、人工对账与停止流程均已准备。
- [ ] 值守人、复核人、停止条件和升级联系人已经记录。

### 自动化验收

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

覆盖默认 sandbox、模式互斥、CLI 无密钥参数、缺少实盘限额、错误白名单/base currency、权限不可验证、提现权限开启、单笔/每日超限、重启保持每日限额、kill switch 到交易所边界不下单，以及日志脱敏。

### 已知限制

- 权限查询依赖交易所适配器提供标准权限接口或 Binance API restriction 接口；无法确认时有意 fail closed。
- 每日风险持久状态目前按本机 SQLite 记录，不支持多个主机共享额度；进入分布式部署前必须迁移到单一权威账本。
- 本阶段不解决未知订单、幂等提交、部分成交、重启恢复和权威账户对账；这些属于 G2/G3，因此项目仍保持 sandbox-only。


---

<!-- 合并自 docs/g2_order_lifecycle.md -->
## G2 订单提交、幂等与成交事实闭环

状态：✅ 已完成并通过自动化故障注入测试。本文描述当前实盘订单主链路，不代表允许真实资金运行。

### 结果契约

`LiveBroker.submit_order()` 始终返回 `OrderSubmissionResult`，不再用隐式 `None` 表示成功或失败。结果包含：

- `client_order_id` 与交易所订单 ID；
- `accepted/open/partial/filled/canceled/rejected/unknown` 对应的统一状态；
- requested、filled、remaining 数量与平均成交价；
- 网络、超时、限流、认证、交易规则、余额不足、交易所不可用、安全策略和未知错误分类；
- 订单事实是否已经安全持久化，以及当前 bar 是否允许标记完成。

open 和 partial 响应不会写成完整成交。`self.trades` 只保留兼容投影，不是权威账本；权威事实来自 `OrderStore` 的 orders 与 fills 表。

### 确定性 client order ID

每个 `OrderIntent` 的 ID 绑定以下身份字段：

- exchange；
- account；
- symbol；
- timeframe；
- bar close time；
- strategy ID；
- action；
- sequence。

这些字段以规范 JSON 编码后计算 SHA-256 摘要，生成 `qt_<24 hex>`。同一信号重复消费会得到同一 ID；订单数量和价格不是身份字段，因此重放时配置漂移不会悄悄创建第二个逻辑订单。

### 提交流程

1. 构造 `OrderIntent` 并执行本地规则和安全检查。
2. 在任何交易所网络调用前，将 intent 以 `submitting` 写入 `OrderStore`。
3. 在调用 `create_order` 前持久记录 `submission_attempted=true`。
4. 原样向 CCXT 参数传入 `clientOrderId`。
5. 将交易所响应规范化并持久化 requested、filled、remaining、average price 和状态。
6. 将响应中的每个 fill 独立写入 fills 表，使用交易所 fill ID 去重；没有逐笔明细时只记录累计数量的新增差额。
7. 只有订单事实安全持久化且状态不是 unknown，bar 才能标记为 processed。

相同 client ID 再次提交时：

- 如果此前尚未开始网络提交，则安全继续首次提交；
- 如果已经尝试过提交，则查询交易所事实；
- 绝不以盲目重试替代查询；
- terminal 状态直接返回持久化结果。

### 状态转换

主状态为：`created → submitting → accepted/partial/filled/canceled/rejected/unknown`。

- accepted 可以进入 partial、filled、cancel_pending、canceled、rejected 或 unknown；
- partial 可以继续 partial、filled、cancel_pending、canceled 或 unknown；
- cancel_pending 的取消拒绝会查询交易所，可回到 accepted/partial，或进入 filled/canceled/unknown；
- canceled 后允许处理迟到的 partial/filled，以覆盖 cancel 与 fill 竞态；
- filled 与 rejected 不允许静默回到活动状态。

衍生品 intent 显式保存 `reduce_only`、`position_side` 和 `position_mode`（`one_way`/`hedge`）；交易所特有参数只在 adapter payload 中出现。

### unknown 与恢复

网络、超时、限流、交易所不可用及无法分类的提交异常都视为可能已经接单，进入 `unknown`。处理规则：

- 优先使用 exchange order ID 查询；
- 没有 exchange ID 时使用 client order ID 查询；
- 查询不可用或仍找不到事实时保持 unknown；
- unresolved unknown 阻止相同 symbol、相同方向的新增风险；
- 引擎进入 `HALTED`，状态输出标记 unknown，并释放当前 bar claim；
- 后续 tick 和进程启动都会恢复全部非终态订单；恢复期间标记 `RECONCILING`；
- 只有查询得到 accepted、partial、filled、canceled 或 rejected 事实后才能继续。

`processing` bar 使用租约。租约未到期时其他进程不能取得；租约过期后允许安全重领。已经 processed 的 bar 永不重领。

### 故障注入验收

自动化测试覆盖：

- 相同信号重复消费 100 次，交易所最多创建一张订单；
- 提交前 intent 已写入但进程崩溃，重启后安全继续；
- 交易所已接单但客户端超时，随后按 client ID 找回且不再次 create；
- 交易所响应后、本地最终持久化前等价故障，通过 unknown 查询恢复；
- partial 后恢复为 filled，requested/filled/remaining 正确；
- 多个 fill 独立去重并汇总到请求数量；
- cancel 拒绝与 fill 乱序到达，以交易所最终事实为准；
- unknown 阻止同方向新增风险；
- stale processing bar 只有租约过期后才能重领；
- unknown 出现在策略路由后，bar claim 被释放而不是标为 processed。

运行命令：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

### 当前边界

- 本阶段提供订单与 fill 的权威本地账本，但完整账户 cash/locked/open orders/positions/equity 对账属于 G3。
- 不支持多个主机同时共享 SQLite 订单账本；横向扩展前必须迁移到单一事务性存储。
- 交易所若既不支持 exchange ID 查询也不支持 client ID 查询，订单会有意保持 unknown 并停机。
- 订单恢复闭环完成不等于项目允许真实资金运行；G3-G9 和独立人工放行仍未完成。
