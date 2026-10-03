# 架构 Roadmap 后续修复与历史回测验收

验收日期：2026-10-02，Asia/Singapore。对应 [架构 Roadmap](architecture_performance_roadmap_20261002.md) 和 [AP01–AP09 初次验收](architecture_performance_validation_20261002.md)。

AP10、AP11、AP13 的工程实现和离线故障验收已完成；AP12 的计时与沙盒采集工具已完成，但用户确认暂无沙盒凭证，真实订单 ACK、撤单和成交回报采样仍未完成。本次还完成一组本地历史回测和输入快照重放验证。所有代码保留在当前工作树，未提交、未启动实盘服务。

## 1. 本次交付与启用边界

| Roadmap | 实现与验收 | 当前边界 |
| --- | --- | --- |
| AP10，有界追赶 | 新增 `live_trading/catchup.py`、`runtime_controls.py`；顺序恢复历史市场状态、路由与冷却进度；原生 DataFetcher 单页回补；游标与恢复状态持久化；最新 bar 的游标、状态和处理事实原子保存。 | 必须显式开启 `--runtime-controls`。历史重放没有 Broker 或候选分配入口；未补齐的缺口禁止该标的最新信号进入下单链路。 |
| AP11，总预算与保护 | 新增 `core/market_read_batch.py`、`live_trading/protection_schedule.py`；读取整批截止时间、原生 REST 剩余超时、晚到结果丢弃、残留任务去重；行情读取前先维护保护单，轮询等待期间按保护周期维护。 | 必须显式开启 `--runtime-controls`。所有账户、风险、撤单和成交状态仍由主线程执行；没有后台线程修改 Broker。 |
| AP12，订单计时 | `core/order_latency.py` 接入真实 Broker 提交、撤单和订单核对；新增 `scripts/measure_sandbox_orders.py`，记录客户端发送/接收、错误类别、ACK 与终态观察。 | 缺少沙盒凭证时退出码为 2，状态为 `blocked_missing_sandbox_credentials`，不构造交易所、不发网络请求或订单。真实延迟和服务指标仍待验收。 |
| AP13，共同请求预算 | `core/request_budget.py` 使用 SQLite 跨进程序列化加权请求准入，覆盖受管公开客户端、Broker 和资金费率/持仓历史客户端；公共/研究请求不能消费最后 10% 关键容量。 | 采用 SDK 请求权重和已观察到的最严格基础节拍；不能替代交易所账户/IP/方法级实际限额核对。仅统一采用同一预算数据库的受管进程。 |

AP10/AP11 参数：`--runtime-controls --market-timeout 5 --protection-interval 5 --catchup-max-bars 100`。开关默认关闭。AP13 的客户端预算钩子和 AP12 的计时记录独立接入，不依赖此开关。默认日线策略和 60 秒轮询保持原配置。

部署共享路径默认为 `D:/QuantTradingV1/reports/request_budget.sqlite3`。不同工作目录或进程应通过环境变量 `QUANT_REQUEST_BUDGET_DB` 指向同一部署路径；没有采用该钩子的外部软件不会自动纳入预算。SDK 元数据请求也进入预算；Broker 使用关键优先级，研究接口使用研究优先级。

## 2. 断线、超时与保护的具体语义

首次启动没有游标时只接管最新已闭合 bar，不重新提交历史信号。有游标时，追赶按时间顺序推进，每标的每轮最多恢复配置的历史 bar 数。超过最新快照覆盖范围的断线，原生客户端从游标之前的指标窗口开始取一个有界 REST 页；合并最新快照后分轮推进。真实时间缺口不会被“推进游标”掩盖，只有连续前缀能被恢复。

历史恢复只更新状态识别、路由切换和已观测 bar 的冷却计数；不调用历史撤单、退出或开仓。账户同步与保护单维护继续依据当前交易所事实执行。追赶游标与策略/Router 检查点在同一事务保存，最新 bar 的处理事实也与检查点在同一事务提交。离线用例覆盖滚动窗口、分轮追赶、游标恢复、当前 bar 去重及长断线原生分页回补。

行情预算默认 5 秒，其中 80% 用于并行读取，剩余预算在各标的本地计算前检查。Python 不能强制杀掉已运行的任意自定义读取函数：超时函数可继续占据一个 worker，但没有交易状态写入入口，同标的也不会启动重复残留请求。原生 SDK 的每次 REST 调用使用剩余截止时间约束网络 timeout。操作系统调度、网络实现以及最后一段不可中断的指标计算仍可能超出预算；关闭进程也可能等待非合作的自定义读取。这是可控退出和状态隔离机制，不是绝对硬实时保证。

保护周期默认 5 秒，实际执行在主线程且不可重入。行情读取前及正常/失败退避等待期间都会检查保护任务，防止公共数据长尾独占整个轮询周期。保护任务本身的私有 API 延迟、状态核对耗时及当前不可中断的工作仍可能延后下一次保护，5 秒是调度目标而非保护单保证成交时间。原有账户事实门禁、未知订单恢复和风险准入继续有效。

## 3. 自动审核与测试证据

自动审批曾拒绝直接替换默认实盘循环的初始方案，理由是高影响调度变更、超时后残留读取和重复账户状态变更尚缺充分验证。随后先建立默认关闭的独立模块与故障测试，再通过显式开关接入；这一方案通过审核。没有启用被拒绝的默认替换方案。

最终完整测试包含整个 `tests` 目录：**2,694 passed、1 skipped、46 subtests passed，168.34 秒**，没有警告输出。跳过项 `tests/test_exchange_sandbox_e2e.py` 是需要启用条件与凭证的只读认证同步测试，不提交订单；本次没有凭证，因此自动跳过。新文件 `tests/test_runtime_roadmap_followup.py` 提供 19 个测试；修改范围 Ruff 与 `git diff --check` 通过。

混合负载测试同时启动公共、研究、关键三个进程，各发起四次权重为 2 的请求准入；统一 SQLite 预算的相邻准入时间至少满足 30 毫秒节拍。另验证公共负载不能消耗关键保留容量、截止时间能中止等待。这验证本地准入规则，不把准入时间当成网络实际发包时间，也不宣称所有交易所方法级限额已经实测。

行情全部阻塞的离线演练每场景五次，预算 50 毫秒；晚到结果始终没有被采纳，同一运行中标的最多一次调用：

| worker | 标的数 | 返回耗时 p50 | p95 | 峰值 worker |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 10 | 58.05 ms | 62.56 ms | 1 |
| 1 | 60 | 62.76 ms | 64.95 ms | 1 |
| 4 | 10 | 55.96 ms | 63.86 ms | 4 |
| 4 | 60 | 55.53 ms | 64.36 ms | 4 |
| 8 | 10 | 57.91 ms | 62.75 ms | 8 |
| 8 | 60 | 56.47 ms | 58.27 ms | 8 |

这组数字反映 Windows 小样本调度与故障返回，不能当作真实行情吞吐或绝对 50 毫秒服务承诺。实际启用的 LiveTradingEngine 测试另外验证主线程先保护后读行情、历史追赶不下单、追赶结束后最新 bar 只处理一次。

## 4. 公开行情复测与 WebSocket 评估

采样起点为 **2026-10-02 17:48:14，UTC+8**。Binance、OKX 各三个现货标的、三个 worker、五轮、每标的 200 根 1m K 线，**30 次逐标的抓取全部成功**。本次使用新共享请求预算；旧采样快照保留在原目录，新快照单独存于 `outputs/performance_20261002/followup_market/`。

| 交易所 | 冷启动整轮 | 预热后四轮 p50 | 预热后四轮 p95 | 最后快照闭合 bar 年龄 |
| --- | ---: | ---: | ---: | ---: |
| Binance | 4.526 s | 165.13 ms | 202.92 ms | 19.45 s |
| OKX | 3.365 s | 255.05 ms | 296.68 ms | 23.93 s |

计时含预算等待、客户端元数据、网络和标准化，不含策略执行或订单 ACK。bar 年龄是采样时刻与理论收盘时刻之差，主要受采样相位影响，不能直接解释为交易所发布延迟。本次与初次样本的市场时间和网络条件不同，不将二者差异作为代码版本提速比例。

价格速度定义为 `10000 × log(close_t / close_previous) / elapsed_minutes`；每份快照使用 199 根闭合 bar、198 个有效相邻价格对，时间窗口为 **14:29–17:47，UTC+8**，没有缺口，排除一根未闭合 bar：

| 标的 | Binance 绝对速度 p50 | Binance p95 | Binance 最大值 | OKX p95 |
| --- | ---: | ---: | ---: | ---: |
| BTC/USDT | 2.56 bp/min | 9.10 bp/min | 16.11 bp/min | 9.22 bp/min |
| ETH/USDT | 3.43 bp/min | 10.82 bp/min | 77.11 bp/min | 10.53 bp/min |
| SOL/USDT | 4.10 bp/min | 13.13 bp/min | 26.11 bp/min | 13.90 bp/min |

此窗口 SOL 的 p95 变化速度高于 BTC，ETH 出现单分钟较大变化。它们是已观测闭合 K 线的统计，不能还原逐笔路径、预测未来，或代替收盘到订单成交的端到端测量。

`core/websocket_market.py` 提供仅公开读取的 Binance 闭合事件缓冲：忽略未闭合事件，按时间去重，缺口、重连、溢出要求 REST 修复后才释放数据。相关协议测试通过。实际连接评估运行约 65.18 秒，当前网络返回 `ClientConnectorDNSError`，收到 0 个事件，因而没有可用的推送延迟样本。该模块作为输入评估保留，未接入实际交易引擎；不能把协议测试通过描述为生产 WebSocket 已就绪。

## 5. 本地历史回测

回测使用现有本地 CSV，不访问交易所。选取 BTC、ETH、SOL 三者共同完整区间，避免 SOL 缓存终点之后的数据不足影响比较。

| 输入或设置 | 本次值 |
| --- | --- |
| 标的 | BTC/USDT、ETH/USDT、SOL/USDT |
| 区间 | 2024-01-01 至 2026-08-27，UTC 日线 |
| 数据量 | 每标的 970 根，合计 2,910 个标的-bar |
| 初始资金 | 10,000 USDT |
| 配置 | 原有 `config/params.yaml`，`spot_margin`，TrendBreakout 路由；其余市场状态为 Cash |
| 种子与预热 | seed 42，30 根预热 |
| 成本 | maker/taker 0.1%；基础滑点 5 bp，并使用现有价差、波动滑点和市场冲击模型 |
| 报告目录 | `reports/architecture_followup_20261002/`，完整报告模式 |

| 指标 | 结果 |
| --- | ---: |
| 总收益 | **-5.2891%** |
| 年化复合收益 | -2.0275% |
| 期末权益 | **9,471.0922 USDT** |
| 净盈亏 | **-528.9078 USDT** |
| 最大回撤幅度 | **5.3995%** |
| Sharpe | -0.7620 |
| 完整交易 | 10 笔，20 个买卖成交记录 |
| 胜率 | 40% |
| 利润因子 | 0.02246，系统标记样本不足 |
| 手续费合计 | 10.9790 USDT |
| 滑点归因 | 21.6458 USDT，已体现在成交价，不能从净盈亏再次扣除 |
| 固定等权买入持有基准 | +34.3140%，预热后建立持仓，基准成本为 0 |

当前策略明显落后于这一无成本持有基准。2024、2025 年各五笔完整交易，2026 年没有成交；最后成交为 2025-09-23。策略健康机制记录 159 个受门禁影响的日线 bar，结束时 TrendBreakout 处于 probation，不能把较低回撤归因于持续有效的风险收益能力。此次工程验收没有改变策略参数以改善回测数字。

会计一致性检查 **970 次通过**，差异数为 0，最大绝对浮点差异 `3.64e-12`。闭合交易净盈亏、组合完整交易净盈亏与报告净盈亏一致，非法闭合记录为 0。事件审计有 185 个事件，没有缺失必需事件类型；保护止损触发 1 次，报告中的无保护持仓 bar 为 0。这些是模拟账户与报告口径的核对，不代表真实交易所账户现金流水已核验。

数据审计发现三标的均无重复、缺失、时间缺口、非正价格或非法 OHLC；ETH 有 1 根、SOL 有 2 根被标记为价格尖峰。没有删除这些 bar。未提供第二独立数据源，最高盈利/亏损交易的第二来源核验状态为 `unverified`；本次结论只适用于所保存的本地数据与当前成本假设。

从保存的输入快照重放同一场景，成交、权益、基准及报告/会计摘要四组确定性 digest 与原回测完全一致，24 个清单内输出文件的 SHA256 均核对通过。一次重放的 `BacktestEngine.run` 耗时 **0.4521 秒**，不含输入读取与报告生成；它用于重放验证，没有旧版同场景配对，不作为提速比例。

## 6. 文件与复现

源代码身份记录覆盖 457 个受控源文件，聚合 SHA256 为 `c983ef84a8f2a7d30627f387a3438810fcfd97d69db071dc072282a42117d4a0`。配置文件原始 SHA256 为 `49665830ead75ec74f6ef736f3f8b1ee7fdb3122ba9f334cda9d944b26decb4e`。完整测试与重放后仅补充运行期请求预算数据库的 Git 忽略规则，业务源码没有变化。Git HEAD 仍为 `b7e6913`；实际修改属于未提交工作树，不能只凭该 commit 重建本次实现。

| 文件 | 用途 |
| --- | --- |
| `outputs/performance_20261002/followup_full_tests.txt` | 全仓库测试原始输出 |
| `outputs/performance_20261002/followup_runtime_probe.py`、`followup_runtime_timeout.json` | 有界读取阻塞演练与数字 |
| `outputs/performance_20261002/followup_market/market_speed.json` 与同目录 CSV | 新预算下公开链路与价格速度，含每份快照哈希 |
| `outputs/performance_20261002/followup_websocket.json` | 公开 WebSocket 连接失败与修复状态证据 |
| `outputs/performance_20261002/followup_sandbox_orders.json` | AP12 无凭证状态；真实订单样本为空 |
| `outputs/performance_20261002/followup_verify_backtest.py`、`followup_backtest_verification.json` | 同场景重放、会计检查和输出哈希核对 |
| `outputs/performance_20261002/followup_build_manifest.json` | 实际源码及本次验收文件哈希 |
| `reports/architecture_followup_20261002/run_manifest.json` 与 `data_inputs/` | 原回测配置、输入快照与确定性输出身份 |
| `reports/architecture_followup_20261002/report.pdf`、`report.txt`、`metrics.json`、`equity.png` | 完整回测报告、可读指标与净值/回撤图 |

在仓库根目录的 PowerShell 中运行：

```powershell
& '.\.venv\Scripts\python.exe' -u 'scripts\run_portable_tests.py' -q tests --maxfail=5

& '.\.venv\Scripts\python.exe' -u 'outputs\performance_20261002\followup_verify_backtest.py'

# 新建独立目录；main.py 会拒绝覆盖已有输出目录。
& '.\.venv\Scripts\python.exe' -u 'main.py' --source local --data-dir 'data\binance\1d' --symbols BTC/USDT ETH/USDT SOL/USDT --start 2024-01-01 --end 2026-08-27 --capital 10000 --timeframe 1d --seed 42 --report-profile full --output-dir 'reports\architecture_followup_20261002_reproduce'
```

实验目录与回测目录按仓库规则不纳入 Git，当前本地文件保留。复跑公开行情会得到新市场窗口；复跑测试或验证工具会刷新对应本地验收输出，不能将不同时间采样直接当作同输入版本性能对照。
