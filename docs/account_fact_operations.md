# 完整账户事实接入与对账操作

更新日期：2026-09-20。对应 `SYS-05` 及其账户、持仓、预算和运行依赖。工程入口见
[account_source.py](../core/account_source.py)、[account_sync.py](../core/live_broker/account_sync.py)、
[recovery.py](../live_trading/recovery.py)和[run_live.py](../run_live.py)。

当前实现能够读取外部固定账户导出，验证结构、覆盖、时间与哈希，重建本地订单/成交投影，
比较现金、持仓、估值和资本桥接，并据结果禁止新增风险。真实完整导出、独立采集证明及持续
运行回执仍必须来自实际账户；本文不提供伪造的真实导出，也不改变策略准入锁。

## 1. 来源完整性与独立性

`PinnedAccountExport` 只读取已有文件，不调用交易所、不推断开账资本、不回写外部账本。
接入时从文件外指定预期 SHA-256、`source_id` 和账户身份；导出内部的相同字段必须匹配。
哈希证明本次读取的字节没有变化，不能证明文件来自独立采集，也不能证明其涵盖全部账户。

可信边界是部署端注入的 `provenance_verifier(digest, identity)`：只有外部可信校验明确返回
`True`，且证据类型为 `independent_export`，才可能设置
`production_account_source_verified=true`。文件自写 `verified=true`、仅把哈希抄入同一
导出、或把内部日志重新包装为“独立导出”，都不会授予这种信任。`synthetic_fixture` 永远
不能变成真实账户验证通过。

当前 `run_live.py` 的文件参数只创建没有外部 attestor 的只读适配器。因此，即使算术比较
`ok=true`，默认仍为 `allows_new_risk=false`。命令行没有“相信此文件”开关；可信采集与
证明适配器须由部署边界单独接入 `broker.configure_account_source(...)`，不能通过修改
导出 JSON 绕过。账户来源通过也不替代策略治理、R7 或 R8 准入检查。

## 2. 导出结构和覆盖约束

顶层 schema 为数字 `schema_version=1`，不是字符串。所有时间带明确时区，推荐 UTC。
默认最大快照年龄为 90 秒；快照与持仓价格不得来自未来或超过各自时效。历史成交的费用换算时间须在声明覆盖区间内，且与对应成交时间相差不超过 90 秒。各记录列表中的
`record_id` 必须存在且唯一，重复 JSON 对象键也会拒绝。

| 部分 | 必需内容与含义 |
|---|---|
| `source_id`、`evidence_kind` | 与外部钉住的来源一致；类型为 `independent_export` 或 `synthetic_fixture` |
| `identity` | `exchange`、`environment`、`account`、`market_type`、`base_currency`，且 `snapshot.identity` 完全相同 |
| `captured_at` | 与 `snapshot.captured_at` 完全一致，并在当前检查时间允许的年龄范围内 |
| `coverage` | `from`、`through`、`complete=true`、`scope="entire_account"`；从开账锚点一直覆盖到本次采集 |
| `opening_capital` | `at`、非负 `amount`、账户币种 `currency`、`source_id`、显式 `positions=[]` |
| `financing` | 融资成本列表，包含 `record_id`、`at`、非负 `amount`、账户币种 `currency`、`source_id` |
| `snapshot.cash` | `free`、`locked`、`total`，要求 `free + locked = total` |
| `snapshot.positions` | 完整账户非零资产/负债净持仓及其估值；字段见下表 |
| `snapshot.orders` / `fills` | 覆盖开账至采集期间的订单和逐笔成交，数量、身份、费用证据互相对得上 |
| `snapshot.cashflows` | `deposit` 为正金额、`withdrawal` 为负金额，含 `record_id`、`at`、`currency`、`source_id` |
| `snapshot.equity` / `capital_bridge` | 权益和开账资本、净外部现金流、已实现/未实现毛盈亏、全部成本及融资成本桥接 |

实际支持的规范账户模式为 `spot`、`spot_margin`。`run_live.py --market-type margin`
在导出身份中对应 `spot_margin`；合约模式即使其他运行组件具备接口，也不能通过此版本的
全账户来源验收。

`run_live.py --account-id` 是非秘密的稳定账户标签。运行目录和 broker 使用
`RuntimeIdentity(exchange, environment, account_id, CLI_market_type).key` 生成的 24 位身份键。
因此导出 `identity.account` 应匹配该 broker 账户键，不应把 CLI 原始标签直接填入导出。
`environment` 区分 `sandbox` 与 `live`，`base_currency` 必须与账户核算币种一致。

| 记录 | 当前比较字段 |
|---|---|
| 订单 | `record_id`（本地 `client_order_id`）、`exchange_order_id`、`symbol`、`side`、`status`、`requested_qty`、`filled_qty`、`remaining_qty` |
| 成交 | `record_id`（权威成交 ID）、`order_id`、`symbol`、`side`、正 `qty` / `price`、`at`、`fee_amount`、`fee_currency`、`fee_base_amount`、`fee_conversion_rate`、`fee_conversion_source`、`fee_conversion_at` |
| 持仓 | `record_id`（如 `BTC/USDT`）、`qty`、正 `mark_price`、`price_source`、`price_at`、账户币种 `price_currency`、`max_price_age_seconds` |
| 资本桥 | `initial_capital`、`net_cashflows`、`realized_gross_pnl`、`unrealized_gross_pnl`、`costs`、`financing_costs` |

现金流、成交、融资和费用换算时间均在 `(opening_capital.at, captured_at]` 内。订单列表
必须足以解释所有成交，订单的请求量等于已成交量与剩余量之和。业务字段缺少、多出或不一致
会产生差异；只有明确声明的 `captured_at`、`snapshot_id`、`transport_latency_ms` 属于
比较时忽略的元信息。

完整账户范围包含策略之外的订单、资产、负债、费用和转账，不能只导出机器人认识的标的。
当前不支持带开账库存的迁移：必须从明确空仓锚点开始；缺锚点不能由当前权益倒推出初始资本。
非现金入金、换汇入金、既有库存和第三币种手续费库存须另有迁移设计与证据，不能用现金流
字段强行抹平。

费用当前支持账户核算币种，或成交交易对的基础资产币种。每笔均须提供原币费用、换算来源
和时间；同核算币种换算率必须为 1，`fee_amount × fee_conversion_rate` 应等于
`fee_base_amount`。例如用第三币种抵扣交易费时，本版本会报
`third_currency_fee_inventory_migration_required`，不会把它默认为零费用。

## 3. 接入前的只读检查与无密钥命令示例

只查看接口不访问账户：

```powershell
python run_live.py --help
```

操作方先取得完整独立导出和文件外可信交付记录，再传入下面三个参数；必须同时提供：

```text
--account-facts <只读导出JSON路径>
--account-facts-sha256 <可信交付记录的64位小写SHA-256>
--account-facts-source-id <可信交付记录的来源ID>
```

可以在本地计算文件哈希与可信交付值比较，但自行计算得到的哈希不能充当独立来源证明。
接入参数组缺失、哈希格式错误、文件不可读或字节不符，会在凭据读取和网络连接之前拒绝。

以下是已有导出后的沙盒预检模板，尖括号内容必须由实际交付记录替换；其中没有密钥，也不
生成导出文件。交易所凭据和既有安全策略仍需由获授权的环境配置提供。这个预检会连接沙盒，
读取账户/订单事实并写本地预检报告，但不会启动主循环。

```powershell
python run_live.py --sandbox --exchange binance --market-type margin --account-id sandbox-ops-01 --base-currency USDT --symbols BTC/USDT ETH/USDT --account-facts "<已取得的只读账户导出路径>" --account-facts-sha256 "<来自独立交付记录的SHA-256>" --account-facts-source-id "<来源ID>" --preflight-only --preflight-report reports/account_preflight_review.json
```

当前参数文件采用 `spot_margin` 账户与相应费率身份，故示例使用 `--market-type margin`。
不要通过改成 `spot` 或 `swap` 消除检查失败；账户成本契约会核对一致性。真实资金入口还会
检查策略治理和 R8 参数，当前 `TrendBreakout=paused_revalidation` 仍阻止真实资金准入。

预检输出重点查看 `account_reconciliation`、`account_entry_gate`、`allows_new_risk`、
`protection_only_startup_allowed` 和具体 `health_reason_codes`。单独一项 `ok=true`
不足以放行。预检失败退出 2，不会因为“仅缺账户来源证明”而返回成功；正常运行模式只有在
其他启动检查全通过、唯一健康原因是 `ACCOUNT_FACTS_UNVERIFIED` 时，才允许进入只管理
已有持仓保护的循环。

## 4. 对账过程与故障处置

本地投影从 `OrderStore` 的权威逐笔成交和 lot 账本重建订单、净持仓、现金及已实现盈亏；
明确来源的开账资本、现金流、融资和估值从导出进入计算。仅由订单推测出的成交、缺费用事实、
未知/提交中/撤单待确认订单、缺独立成交、持仓投影异常都会拒绝形成可放行结论。

比较同时验证三条关系：现金可用量加冻结量等于现金总量；现金加净持仓估值等于权益；
开账资本加净现金流加已实现及未实现毛盈亏减成本等于权益。默认数值容差为 `1e-8`。
如果运行刚取得交易所余额，还会用该独立余额观察再核对导出的现金和全账户持仓，防止过时
导出漏掉新转账或外部资产。

| 情况 | 当前行为 | 操作意义 |
|---|---|---|
| 余额网络错误、缺字段或非法余额 | `account_balance_sync_failed`，同步未成功 | 不能把旧余额当本次新事实；新增风险停止，先恢复余额数据与健康状态 |
| 余额读取成功，但账务比较失败 | `SyncResult.error="account_reconciliation_failed"`；`balance_sync_succeeded` 仍认可本次余额已取得 | 新入场被账户门禁拒绝；已有可信持仓的退出、保护和减仓仍可执行，不把账务差异误作全部余额网络失效 |
| 算术通过但没有外部可信 attestor | 可能 `ok=true`，但 `production_account_source_verified=false`、`allows_new_risk=false` | 补齐可信来源接入；不能修改文件内标志自证 |
| 来源过期、未来时间或每 tick 检查超龄 | `account_facts_stale_or_future` 等拒绝原因 | 新证据须按新身份取得和接入，不能长期复用一次预检 |
| 对账报告持久化失败 | `account_reconciliation_persistence_failed`，新增风险拒绝 | 恢复记录存储并重新产生可留存对账，不能仅内存放行 |

只读固定导出的哈希不会自动跟随文件更新。原路径内容改变会触发哈希不符；90 秒时效也不会
因为每 300 秒配置一次周期对账而被放宽。长期运行需要部署端持续取得新导出及可信证明，并以
新的固定身份注入来源适配器；当前 CLI 静态文件接入不能自行实现持续可信刷新。

## 5. 按账户与日期留存的实际证据

运行状态按 `reports/runtime/<24位账户身份键>/` 隔离，包含 `orders.db`、`state.db`、
`safety.db`、`live_status.json` 和相关告警。周期对账将实际回执原子写入同目录的
`account_reconciliation/<UTC日期>.json`，并保存状态键 `account_reconciliation:last`
与 `account_reconciliation:<UTC日期>`。

日期文件是该 UTC 日最后一次实际观察，可以被当天后续观察替换；它不是不可变逐笔档案，
也不会补造停机期间的日子。正式验收需另行保全每份外部导出、来源身份、交付校验、实际
订单/成交证据及运行回执，确认覆盖边界。恰好有一个日期文件不等于获得完整日终独立导出。

`SYS-05` 的工程接入与一次算术对账通过，不能关闭真实账户证据要求。`SYS-17 / R7` 仍须
至少 56 个连续自然日、至少两种市场状态、完整逐笔及日终对账和相关运行前置；
[自动化方案](automated_backtest_plan.md) 的 14 日行情刷新统计既不是账户对账，也不能替代
这 56 日要求。真实告警送达、人工操作、交易所恢复和灰度批准依然需要实际回执。

本说明与[开发计划](development_plan.md)、[统一路线图](unified_roadmap.md)、
[R7 运行手册](r7_sandbox_runbook.md)共同使用；任何外部输入尚未提供的部分，保持待证据状态。
