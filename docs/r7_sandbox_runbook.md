# R7 sandbox 长跑 runbook

本文是 Windows PowerShell 下从零启动、监控、告警处置和安全停止的唯一操作步骤。R7 只允许交易所 sandbox/testnet；不要使用真实资金。所有命令都从仓库根目录（本地克隆 `QuantTradingV2` 的路径，下文用 `<REPO_ROOT>` 表示）执行。

2026-09-20 更新：本手册须与[账户事实操作说明](account_fact_operations.md)及[正式门槛契约](roadmap_policy_contract.md)共同使用。R7 / SYS-17 完整验收至少需要 **56 个连续自然日、两种市场状态、逐笔及完整日终对账**；下文的旧 14 日审计工具仅作中期检查。实际账户来源、人工接管和连续观察尚未验收，不能因预检、离线用例或某个 JSON 的 `ok/passed` 为真而宣布长跑或真实资金准入完成。

## 1. 一次性准备

1. 安装 Python 3.9+，打开 PowerShell，并进入仓库（把 `<REPO_ROOT>` 换成你本地实际克隆路径）：

   ~~~powershell
   Set-Location <REPO_ROOT>
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m unittest discover -s tests -p "test_*.py"
   ~~~

2. 在当前 PowerShell 会话中放入 sandbox 专用凭证。不要把值写入仓库、命令历史、日志或截图：

   ~~~powershell
   $env:EXCHANGE_API_KEY = Read-Host "Sandbox API key"
   $env:EXCHANGE_SECRET = Read-Host "Sandbox API secret" -AsSecureString | ConvertFrom-SecureString -AsPlainText
   ~~~

3. 固定本次范围与风险上限。以下值是 sandbox 示例，标的必须与实际 testnet 支持范围一致：

   ~~~powershell
   $env:QUANT_ALLOWED_EXCHANGES = "binance"
   $env:QUANT_ALLOWED_ACCOUNT_TYPES = "margin"
   $env:QUANT_ALLOWED_SYMBOLS = "BTC/USDT,ETH/USDT"
   $env:QUANT_SANDBOX_MAX_ORDER_NOTIONAL = "1000"
   $env:QUANT_SANDBOX_MAX_DAILY_NEW_RISK = "5000"
   $env:QUANT_KILL_SWITCH = ""
   ~~~

## 2. 每次启动前检查

1. 确认没有另一个实例正在运行，并确认命令明确包含 `--sandbox`。
2. 确认当前身份目录 `reports/runtime/<RuntimeIdentity.key>/` 中的 `state.db`、`orders.db` 和 `safety.db` 未被手工替换；账户、交易所、环境和模式不得混用。数据库损坏时进程应拒绝启动，不要删除数据库来绕过检查。旧的全局数据库路径不是当前默认身份目录。
3. 只连接并执行完整基线自检，不进入主循环：

   ~~~powershell
   python run_live.py --sandbox --exchange binance --market-type margin --account-id sandbox-ops-01 --base-currency USDT --symbols BTC/USDT ETH/USDT --interval 60 --preflight-only
   $LASTEXITCODE
   Get-Content reports/startup_preflight.json
   ~~~

4. 核对 `credentials_present`、`mode_confirmed`（detail 必须为 `SANDBOX`）、`kill_switch_inactive`、`account_sync_baseline`、`order_sync_baseline`、`health_baseline`、`circuit_breaker_state_restored`，同时读取 `account_reconciliation`、`account_entry_gate` 和 `allows_new_risk`。账户事实参数及完整 schema 按[账户事实操作说明](account_fact_operations.md)配置。上面的最小命令没有独立账户来源，不能据此通过新增风险检查；即使提供固定哈希文件，当前 CLI 也不会把文件自填声明作为可信来源证明。

当前参数文件使用 `spot_margin` 成本身份，示例因此使用 `--market-type margin`。仅在已批准配置、场地能力和账户范围一致时运行；不要通过改成 `spot` 或 `swap` 绕过成本检查。`--preflight-only` 失败返回 `2`；普通主循环只有在其他检查均通过且唯一阻断为 `ACCOUNT_FACTS_UNVERIFIED` 时才可进入保护性管理模式，其 `allows_new_risk` 仍为假。

退出码 `2` 表示 fail-closed。根据报告的 `health_reason_codes` 排查凭证、网络、交易所 testnet、行情新鲜度或账户同步。`circuit_breaker_state_restored` 的 detail 为 `active` 时进程可继续运行和监控，但运维状态必须保持 `RISK_HALTED` 且禁止新风险，直到下一交易日自动恢复；不要反复重启规避熔断。

## 3. 启动与监控

前台启动，保留该窗口：

~~~powershell
python run_live.py --sandbox --exchange binance --market-type margin --account-id sandbox-ops-01 --base-currency USDT --symbols BTC/USDT ETH/USDT --interval 60
~~~

另开一个 PowerShell 窗口监控：

~~~powershell
Set-Location <REPO_ROOT>
.\.venv\Scripts\Activate.ps1
python -m dashboard
Get-Content reports/live_alerts.jsonl -Tail 20 -Wait
~~~

允许新增风险还必须有持续新鲜且经可信来源校验的完整账户事实。上述最小命令只用于观察启动门禁和必要的保护性管理，不提供账户来源，也不会解除正式策略的 `paused_revalidation`。正常状态应同时检查当前身份目录的 `live_status.json`、持续推进的 `last_update`、未知订单和新增风险门禁；账户权益和持仓须与独立事实对账。dashboard 的旧默认路径不能代替当前账户身份快照；出现 `STATUS_FILE_INVALID` 或退出码 `2` 时不得相信其中资金/持仓值。

## 4. 常见告警处理

| 告警/原因码 | 立即动作 | 恢复条件 |
| --- | --- | --- |
| `MARKET_DATA_*` / `tick_unhealthy` | 保持进程 fail-closed，检查网络、代理、交易对和 testnet K 线时间 | 连续刷新后 health 恢复且时间戳推进 |
| `ACCOUNT_SYNC_*` | 在 testnet 页面核对余额和持仓，检查 API 权限和连接 | 权威账户同步成功且无差异 |
| `ORDER_STATE_UNKNOWN` | 禁止手工补单；按 client order id 在 testnet 查单 | 本地订单账本与交易所终态一致 |
| `circuit_breaker_triggered` | 当日禁止新风险，核对日初权益和当前权益 | 同一交易日重启仍应 halt；只在下一交易日自动清除 |
| `state_snapshot_failed` | 不影响当前 SQLite 权威状态，但立即检查磁盘空间和 snapshot 目录 | 新快照成功且完整性检查通过 |
| `STATUS_FILE_INVALID` | 不使用 dashboard 数值；检查写权限/磁盘，保留 SQLite | 新的原子状态快照正常生成 |

每个 `risk_halt`、`tick_unhealthy`、`circuit_breaker_triggered` 或 `circuit_breaker_restored` 都必须登记。事件键是告警行中的 `<timestamp>|<event>`：

~~~powershell
python -m core.incident_journal --event-key "2026-08-08T12:00:00+00:00|risk_halt" --outcome explained --explanation "Stale-market-data drill; no orders submitted; health recovered" --operator "operator-name"
~~~

## 5. 每日对账

完整账户事实和周期对账采用[账户事实操作说明](account_fact_operations.md)中的当前接口。该链路按账户、UTC 日期保存最新观察快照，不能自动视为不可变逐笔日志或完整日终账本；须另外留存真实日终覆盖、来源及复核记录。

以下为保留的研究账本日终诊断接口，不能替代当前完整账户来源或 R7 六层对账。只有实际运行事件已完整投影到指定 ledger 时才可使用。交易日结束后，从 testnet 的权威账户/持仓导出生成 `reports/external_eod.json`；下面仅演示结构，数值必须来自真实导出，不能从 `live_status.json` 回填：

~~~json
{"cash": {"USDT": "1000"}, "positions": {"BTC/USDT": {"qty": "0.1"}}}
~~~

运行对账并保存退出码：

~~~powershell
python -m research.audit.reconciliation_job --ledger-db reports/ledger.db --account-id sandbox --base-currency USDT --external-state reports/external_eod.json --output-dir reports/reconciliation
$LASTEXITCODE
~~~

退出码 `0` 仅表示所提供研究账本与外部快照在该工具的比较范围内无差异，`2` 表示有差异。任何差异都须解释并修复，不能修改报告抹掉差异。当前 `OrderStore`、账户事实对账和研究 ledger 不是同一接口，不应默认 `reports/ledger.db` 会随实时成交更新；缺少经过验证的投影时，此诊断接口不构成已发生的日终验收。

## 6. 安全停止与恢复

1. 在运行窗口按一次 `Ctrl+C`，等待日志出现 `Live Trading Stopped by User` 并返回提示符。
2. 在 testnet 页面确认无意外未完成订单；未知订单不得靠重启跳过。
3. 运行 dashboard 并保存最后状态、当日告警及对账报告。
4. 只有进程已停止，才可按 `docs/r6_operations.md` 从经过完整性校验的 SQLite snapshot 恢复。不要删除状态库或订单库来“解除”熔断。

紧急情况下先在运行进程的环境设置 `QUANT_KILL_SWITCH=active` 只能阻止后续订单边界；已有进程不会自动重新读取父进程环境，因此仍应使用 `Ctrl+C` 停止并到 testnet 核对订单。

## 7. 中期检查与正式退出验收

14–28 个连续自然日仅可安排中期检查，不能作为 R7 / SYS-17 正式退出门槛。旧工具 `core.r7_acceptance` 默认检查 14 日、逐日研究账本对账、告警解释及 G11–G16 关闭记录；它不验证两种市场状态，也不替代完整账户来源及候选身份验收。准备 `reports/r7_p0_p1_closures.json` 时，只有真实关闭后才能填写：

~~~json
{"G11": "closed", "G12": "closed", "G13": "closed", "G14": "closed", "G15": "closed", "G16": "closed"}
~~~

已有真实记录后运行中期诊断，日期必须替换为实际发生的观察区间：

~~~powershell
python -m core.r7_acceptance --start "<实际开始日期YYYY-MM-DD>" --end "<实际结束日期YYYY-MM-DD>" --account-id sandbox-ops-01 --minimum-days 14
$LASTEXITCODE
Get-Content reports/r7_acceptance.json
~~~

退出码 `0` 和 `ok: true` 只证明旧工具上述有限检查通过，不代表 G24、SYS-17 或实盘准入完整通过。增加该命令的 `--minimum-days` 也不会使它自动具有市场状态、六层逐笔对账或冻结身份校验能力。

正式门槛实现位于 [core/admission_gates.py](../core/admission_gates.py)：`audit_paper_run` 与 `review_admission` 强制至少 56 个连续自然日及两种市场状态，较长的冻结要求继续生效。`evaluate_phase6(bundle)` 会按依赖顺序计算 shadow、paper、六层对账、执行校准、监控和人工裁决，并继续评价小额运行及扩容。实际完整证据包准备好后，命令接口为：

~~~powershell
python -m core.admission_gates --input "<实际Phase6证据包路径.json>" --output reports/phase6/phase6_review.json
~~~

输入使用 `paper_observations`、`minimum_paper_days`、`minimum_market_regimes`、`paper_required_start/end`、`expected_lifecycle/actual_lifecycle`、`execution_observations`、`monitoring_snapshots`、`alert_delivery_verified`、`holdout_report` 及 `admission_approval` 等真实证据字段。不得填入合成记录或人为声明已通过来填补缺口。完整输出中的 `admission_passed` / `tasks.T-6.6` 与包含小额运行和扩容的总 `passed` 分开阅读；命令只在所有阶段均通过时返回 `0`，否则返回 `2`。该工具的计算通过也不能自行认证输入来源，更不能替代绑定精确候选、账户、范围和有效期的独立复核及人工授权。

## 8. G22 与依赖关系

G22 本轮决定为“暂不实现、设触发条件”：现有交易所 sandbox 是 R7 的目标执行边界，新增本地撮合会引入另一套成交、费用、部分成交和订单状态语义，不能用它替代 testnet 的端到端验证。若连续预检或长跑中因 testnet 可用性导致累计 24 小时以上不可运行，或一周内出现三次以上非本系统故障的中断，再单独立项本地 paper adapter；它只能用于可用性解耦，仍不能替代最终 sandbox 验收。

依赖顺序为：G21 和 G23 先于 G24；G22 与二者并行且当前跳过；G24 启动前必须确认每日权威 ledger 输入链有效；R7 退出要求 G21、G23、G24 完成并且 G11–G16 有可核验关闭证据。
