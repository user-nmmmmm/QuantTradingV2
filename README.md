# Still Water QuantTrading

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/Status-Alpha-orange.svg)]()

**Still Water QuantTrading** 是一个以「**市场状态识别（Regime）→ 策略路由（Routing）→ 风险约束（Risk）→ 执行与归因（Execution & Reporting）**」为主线的 Python 量化交易研究框架。

它提供 **多数据源**（Synthetic / Yahoo / CCXT）、**多标的统一时间轴回测**、**Next-Bar Execution 防前视偏差**、以及 **实盘轮询引擎**（CCXT 下单 + 状态导出）。

> 适用范围：策略研究 / 回测工程化 / 交易系统原型。  
> 不适用：直接用于真实资金的生产级交易（需要补齐安全、重试、风控冗余与运维能力）。

---

## 核心特性

- **事件驱动回测引擎**：多标的时间对齐、指标预热、Next-Bar 执行（信号在 bar \(t\) 生成，订单在 bar \(t+1\) 撮合）。
- **市场状态机（Regime Detection）**：基于 SMA 结构 + ADX 强度 + ATR% 波动扩张识别 `TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE`。
- **动态策略路由（Router）**：按 regime 将每根 bar 的决策路由到对应策略，并带 **状态切换互斥 + 冷却期**，避免频繁翻转造成过度交易。
- **成本与执行模拟**：支持 Market/Limit/Stop，模拟 maker/taker 手续费、固定/随机滑点（可选冲击成本）。
- **风控**：杠杆限制、单标的集中度、流动性约束（单笔不超过 bar 成交量比例）、日内回撤熔断（触发后禁止开仓）。
- **报告与归因**：自动输出 `equity.csv / trades.csv / report.txt / equity.png`，并按 `strategy_id` 做归因指标。
- **实盘轮询引擎**：每次 tick 拉取最新 K 线、同步账户、路由策略、提交订单，并导出 `reports/live_status.json` 供外部监控读取。

---

## 快速开始

### 1) 安装依赖

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
# 运行测试套件（pytest）额外需要开发依赖：
pip install -r requirements-dev.txt
```

### 2) 运行回测（推荐从 Synthetic 开始）

- **交互式模式**：

```bash
python main.py
```

- **命令行模式**（可复现，建议固定 seed）：

```bash
# 近 1 年（默认）
python main.py --days 365 --capital 10000 --symbols BTC-USDT ETH-USDT --seed 42

# 指定日期区间
python main.py --source synthetic --start 2019-01-01 --end 2020-12-31 --capital 10000 --symbols BTC-USDT ETH-USDT --seed 42

# 引入滑点（0.001 = 0.1%）；随机滑点在 [0, slippage] 均匀分布
python main.py --days 365 --slippage 0.001 --random_slip
```

### 3) 连接交易所 sandbox

```bash
python run_live.py --exchange binance --symbols BTC/USDT ETH/USDT --interval 60 --sandbox
```

- **API Key**：仅建议通过 `EXCHANGE_API_KEY` / `EXCHANGE_SECRET` 环境变量提供测试环境凭据；不要把密钥放入命令行、仓库或日志。
- **安全范围**：当前只进行回测和 sandbox 验证；R7 完成前不使用真实资金，详见 `docs/deployment.md`。
- **状态导出**：每次 tick 写入 `reports/live_status.json`（实现见 `live_trading/engine.py`）。该文件不能替代交易所订单/持仓对账。

---

## 回测参数说明（`main.py`）

| 参数 | 默认 | 说明 |
| :--- | :--- | :--- |
| `--source` | `synthetic` | 数据源：`synthetic` / `yahoo` / `ccxt` |
| `--symbols` | `BTC-USDT ETH-USDT` | 标的列表；`ccxt` 支持 `BTC/USDT` 与 `BTC-USDT`（内部会尝试转换） |
| `--days` | `365` | 从“当前时间”向前回测 N 天（未指定 start/end 时生效） |
| `--start` / `--end` | — | 指定日期区间（优先级高于 days） |
| `--capital` | `1000.0` | 初始资金 |
| `--seed` | `42` | 随机种子（Synthetic + 随机滑点可复现） |
| `--slippage` | `0.0` | 滑点比例（例如 0.001 = 0.1%） |
| `--random_slip` | `False` | 启用随机滑点（均匀分布） |

---

## 配置（`config/params.yaml`）

回测引擎会读取 `config/params.yaml` 来配置手续费、风控与路由映射（`backtest/engine.py`）。

- **执行与费率（节选）**：
  - `execution.commission_rate_taker`：taker 双边费率
  - `execution.commission_rate_maker`：maker 双边费率
  - `execution.use_impact_cost`：是否启用简化冲击成本
- **风控（节选）**：
  - `risk.max_leverage`：最大杠杆（gross exposure / equity）
  - `risk.max_drawdown_limit`：日内回撤熔断阈值
- **路由映射（节选）**：
  - `routing.TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE` → 策略名（例如 `"TrendBreakout"`）
  - `router.cooldown_bars`：状态切换后的冷却 bars

---

## 输出与报告（`reports/`）

每次回测会在 `reports/` 下生成独立文件夹（命名规则见 `main.py`），典型包含：

- **`report.txt`**：核心指标 + 回测元信息
- **`equity.csv`**：权益曲线（timestamp,equity,cash）
- **`trades.csv`**：逐笔成交（包含手续费、滑点、`strategy_id`、`exit_reason`）
- **`benchmark.csv`**：基准（当前实现为“多标的等权买入并持有”）
- **`equity.png`**：净值/回撤/收益/资金占用四联图
- **`data_quality_report.json`**：数据质量报告（缺失、重复、gap、spike 等）
- **`routing_log.csv`**：每根 bar 的 regime 与路由策略记录

---

## 关键机制（与代码一致的“可落地描述”）

### 市场状态机（`core/state.py`）

- **状态集合**：`TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE`
- **识别逻辑**（简述）：
  - 趋势结构：`close` 与 SMA 快/慢线结构关系
  - 强度过滤：`ADX > threshold`
  - 波动扩张：`ATR/close > atr_pct_threshold` 时覆盖为 `VOLATILE`
  - 去抖动：`stability_period` 连续确认后才切换状态

### 订单执行模型（`core/broker.py`）

- **Market**：bar \(t+1\) 开盘价成交（叠加滑点）
- **Limit**：若触及限价则成交；开盘即可成交时按开盘（taker），盘中触及时按限价（maker）
- **Stop**：触发后按更不利的开盘/触发价成交（taker）

更完整细节见 `docs/backtest_assumptions.md`。

---

## 项目结构

```
QauntTrading/
├── main.py                    # 回测入口（CLI / 交互式）
├── run_live.py                # 实盘入口（轮询式）
├── config/
│   ├── config.py              # 配置加载器（读取 params.yaml）
│   └── params.yaml            # 手续费/风控/路由映射等
├── core/
│   ├── data_fetcher.py        # 数据获取（Synthetic / Yahoo / CCXT）
│   ├── data.py                # 数据验证与质量报告
│   ├── state.py               # 市场状态机
│   ├── broker.py              # 回测撮合与成本模型
│   ├── live_broker.py         # 实盘 broker（CCXT）
│   ├── risk.py                # 风控
│   ├── portfolio.py           # 组合/权益/敞口
│   └── indicators.py          # 指标库
├── strategies/                # 策略插件（基类 + 实现）
├── router/                    # Regime → Strategy 的路由器
├── backtest/                  # 回测引擎与报告
├── live_trading/              # 实盘轮询引擎（导出 live_status.json）
├── research/                  # 研究脚本（原型验证）
├── docs/                      # 文档
└── tests/                     # unittest 用例
```

---

## 文档

- **`docs/README.md`**：文档入口、权威层级和历史归档说明
- **`docs/unified_roadmap.md`**：唯一项目总路线图、R0–R8 和放行门槛
- **`docs/development_plan.md`**：当前开发批次、任务顺序和验收产物
- **`docs/backtest_assumptions.md`**：当前执行模型、费率/滑点、数据对齐与局限性
- **`docs/deployment.md`**：回测、sandbox 和运维安全边界

---

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 常见问题（FAQ）

- **CCXT/Yahoo 拉数失败**：`core/data_fetcher.py` 的 `DataFetcher` 默认会将 `HTTP_PROXY/HTTPS_PROXY` 设置为 `127.0.0.1:7897`。如果你本机没有代理，需要在代码里禁用代理（例如将默认 `proxy_url` 改为 `None`，或在创建 `DataFetcher(...)` 时显式传入 `proxy_url=None`）。
- **多标的无公共时间轴**：回测会优先按“时间戳交集”对齐；若检测为日线或更慢周期，会尝试按“日历日期”退化对齐（见 `backtest/engine.py`）。

---

## 免责声明

本项目仅用于研究、教学与工程演示；不构成投资建议。数字资产与衍生品交易风险极高，可能导致全部损失。实盘部署前请自行补齐安全、风控与运维能力。
