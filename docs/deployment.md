# 部署与运维指南

> 当前状态：研究与 sandbox 验证阶段
>
> 安全门槛：[unified_roadmap.md](unified_roadmap.md) 的 R7 完成前不使用真实资金；R8 仅允许经过复核的小额、少标的、单交易所灰度。

本文档只描述当前允许的安装、回测、sandbox 和基础监控方式。项目尚未达到无人值守生产交易标准；订单恢复、交易所规则、对账、告警、故障演练和连续 paper trading 未全部验收前，不应连接真实资金账户。

## 1. 环境要求

- **操作系统**：Windows 或 Linux。
- **Python**：项目固定版本见根目录 `.python-version`，当前为 Python 3.13.2。
- **依赖安装**：优先使用锁定版本 `python -m pip install -r requirements.lock.txt`；`requirements.txt` 只保存直接依赖。
- **隔离环境**：建议使用项目独立虚拟环境，不复用系统 Python 环境。

安装后运行完整测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

测试通过只说明自动化用例通过，不代表达到真实资金放行门槛。

## 2. 配置和凭据

回测与交易参数位于 `config/params.yaml`，实际取值应同时以配置加载器和运行输出为准。

sandbox 连接需要交易所测试环境凭据。只通过进程环境变量提供，不把密钥写入仓库、配置文件、命令行、日志或截图：

```powershell
$env:EXCHANGE_API_KEY = "sandbox-key"
$env:EXCHANGE_SECRET = "sandbox-secret"
```

```bash
export EXCHANGE_API_KEY="sandbox-key"
export EXCHANGE_SECRET="sandbox-secret"
```

建议使用只具备必要交易权限、禁止提现、限制来源 IP 的专用测试凭据。运行结束后清除当前终端中的环境变量。虽然代码仍兼容 `--api_key` 和 `--secret`，但命令行参数可能出现在 shell 历史或进程列表中，因此文档不推荐该方式。

## 3. 运行回测

```powershell
# 默认交互模式
python main.py

# 固定随机种子
python main.py --days 365 --capital 100000 --symbols BTC-USDT ETH-USDT --seed 42

# 固定起止日期，提高跨日期可复现性
python main.py --source synthetic --start 2019-01-01 --end 2020-12-31 --capital 10000 --symbols BTC-USDT ETH-USDT --seed 42
```

回测输出写入 `reports/<timestamp>_.../`。当前执行、成本、数据和指标口径见 [`backtest_assumptions.md`](backtest_assumptions.md)；固定回归证据见 [`baselines/batch0_fixed_baseline.md`](baselines/batch0_fixed_baseline.md)。

## 4. 运行 sandbox

确认使用交易所测试凭据和测试环境后运行：

```powershell
python run_live.py --exchange binance --symbols BTC/USDT --interval 60 --sandbox
```

运行前至少确认：

- 命令包含 `--sandbox`；
- API 凭据属于测试环境且无提现权限；
- 完整测试已经运行，失败项有明确责任任务；
- `reports/` 可写且磁盘空间充足；
- 可以人工观察订单、持仓、权益和错误；
- 已准备人工停止方式，异常时立即停止进程并在交易所侧复核未结订单和持仓。

当前 sandbox 运行不等于生产就绪，也不应无人值守长期运行。达到 R7/R8 前，本文档不提供真实资金启动命令。

## 5. 状态和输出

- **实时状态**：每次 tick 尝试写入 `reports/live_status.json`，包含基础账户快照。
- **回测结果**：写入 `reports/<timestamp>_.../`，具体文件以当次运行产物和回测假设文档为准。
- **控制台输出**：当前主要运行信息来自前台控制台；项目尚未建立完整的结构化日志、告警和守护体系。
- **Dashboard**：`dashboard/` 目前不是已验收的生产监控入口。

`live_status.json` 不能替代交易所事实查询。网络超时、状态陈旧或同步失败时，应按 fail-closed 原则停止增加风险，并人工核对交易所订单和持仓。

## 6. 停止和异常处理

正常停止使用 `Ctrl+C`。停止进程后仍需在交易所测试环境中检查：

1. 是否存在未结订单；
2. 本地持仓是否与交易所持仓一致；
3. 最后状态文件是否新鲜、完整；
4. 是否发生超时、未知订单状态或同步失败；
5. 是否需要在下一次启动前先人工对账。

在原子状态快照、启动前检查、订单对账、告警、故障注入、重启恢复验证和连续 paper trading 完成验收前，不宣称支持生产部署或优雅故障恢复。

## 7. 生产放行前置条件

真实资金能力由 [`unified_roadmap.md`](unified_roadmap.md) 的 R7/R8 管理，而不是由本文件单独授权。至少需要：

- 交易所精度、最小数量、最小名义金额和 reduce-only 规则校验；
- 确定性订单 ID、未知状态查询、部分成交和重启恢复闭环；
- 组合估值、现金、持仓、已实现/未实现 PnL 和成本对账；
- 原子快照、数据新鲜度、心跳、告警、熔断和人工 kill switch；
- 网络超时、进程崩溃、重复事件和状态损坏的故障演练；
- sandbox 与 paper trading 连续稳定运行并保留审计证据；
- 小额灰度方案、回滚方案、最小权限凭据和人工批准。

任何一项尚未验收时，继续使用回测、回放、sandbox 或 paper trading，不扩大到真实资金。
