# G1 安全启动与凭据治理

状态：已实现并自动化验证。本文只说明安全启动边界，不构成真实资金上线批准。

## 已实施的安全边界

- `run_live.py` 不传模式参数时固定进入 sandbox/testnet。
- 真实端点只能通过显式 `--live` 启用；它与 `--sandbox` 互斥。
- CLI 不提供 API key、secret 或 password 参数，避免凭据进入 shell 历史和进程列表。
- 凭据仅从进程环境读取：`EXCHANGE_API_KEY`、`EXCHANGE_SECRET`，以及交易所需要时的 `EXCHANGE_PASSWORD`。外部密钥管理器应在启动前注入环境变量。
- 启动时校验交易所、账户类型、symbol 白名单和 base currency；任何不匹配均停止启动。
- 每张订单在最终提交边界再次检查 kill switch、symbol、正数参考价格、单笔名义金额和每日新增风险。
- 每日新增风险计数持久化在 `reports/live_safety_state.db`；同一风险日内重启不会重置额度。
- 实盘启动必须从交易所取得 API 权限事实，明确确认所需交易权限开启、提现权限关闭；不可验证即停止。
- 日志过滤环境凭据，以及 Authorization、API key、secret、signature 和常见认证头字段。

## 配置

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

## Sandbox 启动

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

## 实盘额外门槛

真实端点除了显式 `--live` 和上述检查外，还必须满足：

1. 明确配置两项 `QUANT_LIVE_*` 限额；不存在实盘宽松默认值。
2. 交易所适配器能读取当前 API 权限；无法验证时 fail closed。
3. 所需交易权限明确开启。
4. 提现权限明确关闭；字段缺失或含糊同样拒绝启动。

即使程序检查通过，路线图 G2-G9 和人工放行尚未完成时，也不得使用真实资金。

## 人工启动检查表

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

## 自动化验收

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

覆盖默认 sandbox、模式互斥、CLI 无密钥参数、缺少实盘限额、错误白名单/base currency、权限不可验证、提现权限开启、单笔/每日超限、重启保持每日限额、kill switch 到交易所边界不下单，以及日志脱敏。

## 已知限制

- 权限查询依赖交易所适配器提供标准权限接口或 Binance API restriction 接口；无法确认时有意 fail closed。
- 每日风险持久状态目前按本机 SQLite 记录，不支持多个主机共享额度；进入分布式部署前必须迁移到单一权威账本。
- 本阶段不解决未知订单、幂等提交、部分成交、重启恢复和权威账户对账；这些属于 G2/G3，因此项目仍保持 sandbox-only。
