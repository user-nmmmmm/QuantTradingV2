# R7 sandbox 长跑 runbook

本文是 Windows PowerShell 下从零启动、监控、告警处置和安全停止的唯一操作步骤。R7 只允许交易所 sandbox/testnet；不要使用真实资金。所有命令都从仓库根目录（本地克隆 `QuantTradingV2` 的路径，下文用 `<REPO_ROOT>` 表示）执行。

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
   $env:QUANT_ALLOWED_ACCOUNT_TYPES = "spot"
   $env:QUANT_ALLOWED_SYMBOLS = "BTC/USDT,ETH/USDT"
   $env:QUANT_SANDBOX_MAX_ORDER_NOTIONAL = "1000"
   $env:QUANT_SANDBOX_MAX_DAILY_NEW_RISK = "5000"
   $env:QUANT_KILL_SWITCH = ""
   ~~~

## 2. 每次启动前检查

1. 确认没有另一个实例正在运行，并确认命令明确包含 `--sandbox`。
2. 确认 `reports/live_status_state.db`、`reports/live_orders.db` 和 `reports/live_safety_state.db` 未被手工替换。数据库损坏时进程应拒绝启动，不要删除数据库来绕过检查。
3. 只连接并执行完整基线自检，不进入主循环：

   ~~~powershell
   python run_live.py --sandbox --exchange binance --market-type spot --base-currency USDT --symbols BTC/USDT ETH/USDT --interval 60 --preflight-only
   $LASTEXITCODE
   Get-Content reports/startup_preflight.json
   ~~~

4. 只有退出码为 `0`、报告的 `ok` 为 `true`，且以下七项全部 `passed: true` 才可继续：`credentials_present`、`mode_confirmed`（detail 必须为 `SANDBOX`）、`kill_switch_inactive`、`account_sync_baseline`、`order_sync_baseline`、`health_baseline`、`circuit_breaker_state_restored`。

退出码 `2` 表示 fail-closed。根据报告的 `health_reason_codes` 排查凭证、网络、交易所 testnet、行情新鲜度或账户同步。`circuit_breaker_state_restored` 的 detail 为 `active` 时进程可继续运行和监控，但运维状态必须保持 `RISK_HALTED` 且禁止新风险，直到下一交易日自动恢复；不要反复重启规避熔断。

## 3. 启动与监控

前台启动，保留该窗口：

~~~powershell
python run_live.py --sandbox --exchange binance --market-type spot --base-currency USDT --symbols BTC/USDT ETH/USDT --interval 60
~~~

另开一个 PowerShell 窗口监控：

~~~powershell
Set-Location <REPO_ROOT>
.\.venv\Scripts\Activate.ps1
python -m dashboard
Get-Content reports/live_alerts.jsonl -Tail 20 -Wait
~~~

正常状态必须同时满足：dashboard 显示 `Health: HEALTHY (HEALTHY)`；`last_update` 持续推进；无 `unresolved_unknown_order`；账户权益和持仓能与 testnet 页面解释一致。dashboard 返回 `STATUS_FILE_INVALID` 或退出码 `2` 时，不得相信其中任何资金/持仓值，应按状态文件损坏处理。

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

交易日结束后，从 testnet 的权威账户/持仓导出生成 `reports/external_eod.json`，格式如下。数值使用字符串，且必须来自交易所，不能从 `live_status.json` 回填：

~~~json
{"cash": {"USDT": "1000"}, "positions": {"BTC/USDT": {"qty": "0.1"}}}
~~~

运行对账并保存退出码：

~~~powershell
python -m core.reconciliation_job --ledger-db reports/ledger.db --account-id sandbox --base-currency USDT --external-state reports/external_eod.json --output-dir reports/reconciliation
$LASTEXITCODE
~~~

退出码 `0` 表示当日报告无差异，`2` 表示有差异。任何差异都必须当天解释并修复；不得修改报告把差异抹掉。注意：开始长跑前必须确认当前实时事件链确实持续写入 `reports/ledger.db`；如果该文件没有随成交更新，G24 尚未具备开始条件。

## 6. 安全停止与恢复

1. 在运行窗口按一次 `Ctrl+C`，等待日志出现 `Live Trading Stopped by User` 并返回提示符。
2. 在 testnet 页面确认无意外未完成订单；未知订单不得靠重启跳过。
3. 运行 dashboard 并保存最后状态、当日告警及对账报告。
4. 只有进程已停止，才可按 `docs/r6_operations.md` 从经过完整性校验的 SQLite snapshot 恢复。不要删除状态库或订单库来“解除”熔断。

紧急情况下先在运行进程的环境设置 `QUANT_KILL_SWITCH=active` 只能阻止后续订单边界；已有进程不会自动重新读取父进程环境，因此仍应使用 `Ctrl+C` 停止并到 testnet 核对订单。

## 7. 2–4 周退出审计

长跑建议固定 14–28 个连续自然日。准备 `reports/r7_p0_p1_closures.json`，只有真实关闭后才能填写：

~~~json
{"G11": "closed", "G12": "closed", "G13": "closed", "G14": "closed", "G15": "closed", "G16": "closed"}
~~~

运行退出审计：

~~~powershell
python -m core.r7_acceptance --start 2026-08-10 --end 2026-08-23 --account-id sandbox --minimum-days 14
$LASTEXITCODE
Get-Content reports/r7_acceptance.json
~~~

审计会 fail-closed 检查：连续天数、每天一份零差异对账、所有 halt/alert 都有 `resolved` 或 `explained` 记录、G11–G16 全部关闭。只有退出码 `0` 且 `ok: true` 才满足 G24；本次代码交付不等同于已经完成 2–4 周真实运行。

## 8. G22 与依赖关系

G22 本轮决定为“暂不实现、设触发条件”：现有交易所 sandbox 是 R7 的目标执行边界，新增本地撮合会引入另一套成交、费用、部分成交和订单状态语义，不能用它替代 testnet 的端到端验证。若连续预检或长跑中因 testnet 可用性导致累计 24 小时以上不可运行，或一周内出现三次以上非本系统故障的中断，再单独立项本地 paper adapter；它只能用于可用性解耦，仍不能替代最终 sandbox 验收。

依赖顺序为：G21 和 G23 先于 G24；G22 与二者并行且当前跳过；G24 启动前必须确认每日权威 ledger 输入链有效；R7 退出要求 G21、G23、G24 完成并且 G11–G16 有可核验关闭证据。
