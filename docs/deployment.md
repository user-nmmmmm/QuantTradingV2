# 部署与运维指南

本指南介绍如何在生产环境或服务器中部署、配置及运行 QuantTrading 系统。

## 1. 环境要求

- **操作系统**: Linux (推荐 Ubuntu 20.04+) 或 Windows。
- **Python**: 3.9+。
- **依赖安装**: `pip install -r requirements.txt`。

## 2. 系统配置

### API 密钥 (API Keys)
进行实盘交易需要交易所 (如 Binance) 的 API 密钥。
可以通过环境变量或命令行参数进行设置。

**环境变量设置 (.env 或 export):**
```bash
export EXCHANGE_API_KEY="your_api_key"
export EXCHANGE_SECRET="your_api_secret"
```

### 策略参数
默认参数定义在 `strategies/` 目录的代码中。如有需要，可在 `run_live.py` 初始化策略时进行覆盖。

## 3. 运行回测

使用 `main.py` 运行回测任务。

```bash
# 基础运行 (交互模式)
python main.py

# 带参数运行 (命令行模式)
python main.py --days 365 --capital 100000 --symbols BTC-USDT ETH-USDT --seed 42
```

## 4. 运行实盘交易

使用 `run_live.py` 启动实盘交易引擎。

### 安全优先 (沙盒模式)
请务必先在沙盒模式下测试。
```bash
python run_live.py --sandbox --symbols BTC/USDT
```

### 实盘交易 (生产环境)
**警告**: 此操作涉及真实资金交易。

1. **前台运行**:
   ```bash
   python run_live.py --symbols BTC/USDT --api_key "..." --secret "..."
   ```

2. **后台运行 (Linux - nohup/screen)**:
   建议使用 `screen` 或 `tmux` 保持会话。
   
   ```bash
   # 使用 nohup
   nohup python run_live.py --symbols BTC/USDT > live_trading.log 2>&1 &
   
   # 使用 screen
   screen -S quant
   python run_live.py --symbols BTC/USDT
   # 按 Ctrl+A, D 分离会话
   ```

## 5. 监控仪表盘

系统会导出可被外部监控读取的状态文件；你可以据此自建 Streamlit/Plotly 看板。

### 状态文件

- **实时监控 (Live Monitor)**: 实盘引擎每次 tick 写入 `reports/live_status.json`，包含最新 `cash/equity/positions/symbols` 等信息。
- **回测分析 (Backtest Analysis)**: 回测会在 `reports/<timestamp>_.../` 下输出 `equity.csv / trades.csv / report.txt / equity.png` 等文件，便于二次可视化。

如需快速搭建界面，可复用 `dashboard/utils.py` 的主题样式（该仓库当前不包含可直接运行的 dashboard 应用入口）。

## 6. 运维维护

- **日志**: 查看 `live_trading.log` (如已配置) 或控制台输出。
- **状态文件**: `reports/live_status.json` 包含最新的心跳信息。
- **停止服务**: 使用 `Ctrl+C` 或 `kill <pid>` 停止实盘引擎。系统设计支持优雅退出。
