# Still Water QuantTrading

> **规范目录名**：`QuantTradingV1`。旧拼写 `QauntTradingV1` 已废弃，部署脚本与文档均不应再使用。
>
> **能力边界声明**：本仓库未实现任何机器学习训练或预测子系统；曾经的占位包 `models/` 已被移除，避免仓库宣称不存在的 ML 能力。

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/Status-Alpha-orange.svg)]()
[![Tests: pytest](https://img.shields.io/badge/tests-pytest-green.svg)](#测试)

---

## 概述

**Still Water QuantTrading** 是一个以「**市场状态识别（Regime）→ 策略路由（Routing）→
风险约束（Risk）→ 执行与归因（Execution & Reporting）**」为主线的 Python 量化交易研究框架。
它提供**多数据源**（Synthetic / Yahoo / CCXT）、**多标的统一时间轴回测**、
**Next-Bar Execution 防前视偏差**的撮合模型，以及一个**实盘轮询引擎**
（CCXT 下单 + 状态导出 + 只读运维 Dashboard）。项目当前处于 **Alpha** 阶段：
回测链路（数据 → 状态机 → 路由 → 撮合 → 风控 → 归因）已具备工程化的正确性验证
（固定基线回归、OOS/Walk-Forward/Bootstrap/Monte Carlo 稳健性检验），实盘链路
仍在按 `docs/unified_roadmap.md` 的 R0–R8 门槛逐步补齐安全冗余与运维能力。

### 适用范围

| | 说明 |
| --- | --- |
| ✅ 适用 | 策略研究、回测工程化、交易系统原型验证 |
| ⚠️ 不适用 | 直接用于真实资金的生产级交易（需要补齐安全、重试、风控冗余与运维能力） |

---

## 架构总览

```text
┌──────────────┐   ┌───────────────────┐   ┌──────────────┐   ┌───────────────────┐
│   数据层      │──▶│  市场状态机 Regime │──▶│  策略路由    │──▶│  风控 + 撮合/归因  │
│ Data Layer   │   │  (core/state.py)  │   │  Router      │   │ Risk + Execution   │
│ Synthetic/   │   │  TREND_UP/DOWN    │   │  Regime→     │   │ & Attribution      │
│ Yahoo/CCXT   │   │  SIDEWAYS/        │   │  Strategy    │   │ (core/risk.py,     │
│              │   │  VOLATILE         │   │  (router/)   │   │  core/broker.py,   │
│              │   │                   │   │              │   │  core/metrics.py)  │
└──────────────┘   └───────────────────┘   └──────────────┘   └───────────────────┘
```

回测与实盘共享同一条流水线的抽象（数据 → 状态 → 路由 → 风控 → 执行），区别仅在于
执行器（`core/broker.py` 撮合模拟 vs. `core/live_broker.py` 经 CCXT 下真实/沙盒单）
和驱动方式（回测一次性跑完整段历史；实盘按 tick 轮询）。

---

## 核心特性

- **事件驱动回测引擎**：多标的时间对齐、指标预热、Next-Bar 执行（信号在 bar $t$
  生成，订单在 bar $t+1$ 撮合，杜绝前视偏差）。
- **市场状态机（Regime Detection）**：基于 SMA 快/慢线结构 + ADX 强度过滤 +
  ATR% 波动扩张识别 `TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE` 四种状态，
  并带去抖动（`stability_period`）防止状态频繁跳变。
- **动态策略路由（Router）**：按 regime 将每根 bar 的决策路由到对应策略，并带
  **状态切换互斥 + 冷却期**（`router.cooldown_bars`），避免频繁翻转造成过度交易。
- **成本与执行模拟**：支持 Market / Limit / Stop 三种订单类型，模拟 maker/taker
  双边手续费、固定/随机滑点，并可选启用基于成交量比例的简化冲击成本模型。
- **风控**：杠杆上限（`risk.max_leverage`）、单标的持仓集中度上限
  （`risk.max_pos_size_pct`）、流动性约束（单笔订单不超过 bar 成交量的固定比例）、
  日内回撤熔断（触发后禁止新开仓，不强平存量持仓）。
- **报告与归因**：自动输出 `equity.csv / trades.csv / report.txt / equity.png`，
  并提供按策略、标的、月份切分的归因、稳健性验证（OOS / Walk-Forward /
  Bootstrap / Monte Carlo / 多重检验校正）等指标，详见
  [`docs/glossary.md`](docs/glossary.md)。
- **实盘轮询引擎**：每次 tick 拉取最新 K 线、同步账户、路由策略、提交订单，并
  导出 `reports/live_status.json` 供外部监控读取；异常订单可通过
  `resolve_live_order.py` 走人工核实与审计流程恢复。

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

> CI 与 `mypy` 目标 Python 版本为 **3.11**（见 `.github/workflows/tests.yml`、
> `pyproject.toml`），本地开发环境固定为 3.13.2（`.python-version`）。

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

回测要求显式传参；无参数运行 `python main.py` 会打印用法并以非零状态退出，
不会阻塞在交互式输入上，便于脚本化调用：

```bash
python main.py --source synthetic --days 365 --symbols BTC-USDT ETH-USDT
```

### 3) 连接交易所 sandbox

```bash
python run_live.py --exchange binance --symbols BTC/USDT ETH/USDT --interval 60 --sandbox
```

- **API Key**：仅建议通过 `EXCHANGE_API_KEY` / `EXCHANGE_SECRET` 环境变量提供测试环境
  凭据；不要把密钥放入命令行、仓库或日志。
- **安全范围**：当前只进行回测和 sandbox 验证；R7 完成前不使用真实资金，详见
  [`docs/deployment.md`](docs/deployment.md)。
- **状态导出**：每次 tick 写入 `reports/live_status.json`（实现见
  `live_trading/engine.py`）。该文件不能替代交易所订单/持仓对账。

#### 恢复未知状态的实盘订单

若某笔实盘订单停留在 `UNKNOWN` 状态，**先停止交易并在交易所订单历史中独立核实
该订单确实不存在**，再使用恢复命令：

```bash
python resolve_live_order.py CLIENT_ORDER_ID --order-store reports/live_orders.db \
  --operator YOUR_ID --reason 'verified absent at exchange' \
  --confirm-not-submitted
```

该命令会拒绝已有交易所订单 ID 或任何成交记录的订单，把符合条件的订单迁移到
`EXPIRED_UNSUBMITTED` 状态，并把操作人、原因、迁移前状态和时间戳写入 SQLite
审计账本。**切勿**仅因某次交易所查询超时或临时无结果就使用此命令。

---

## 只读运维 Dashboard

Dashboard 是一个可运行的最小化 CLI，消费 `live_status.json` 与近期告警记录；
它**不是** Web UI，也**从不控制交易**：

```bash
python -m dashboard --status reports/live_status.json --alerts reports/live_alerts.jsonl
```

状态快照缺失或非法时，以退出码 `2` 结束。

---

## 回测参数说明（`main.py`）

| 参数 | 默认 | 说明 |
| :--- | :--- | :--- |
| `--source` | `synthetic` | 数据源：`synthetic` / `yahoo` / `ccxt` |
| `--symbols` | `BTC-USDT ETH-USDT` | 标的列表；`ccxt` 支持 `BTC/USDT` 与 `BTC-USDT`（内部会尝试转换） |
| `--days` | `365` | 从"当前时间"向前回测 N 天（未指定 start/end 时生效） |
| `--start` / `--end` | — | 指定日期区间（优先级高于 `--days`） |
| `--capital` | `10000.0` | 初始资金（USDT） |
| `--seed` | `42` | 随机种子（Synthetic 数据 + 随机滑点可复现） |
| `--slippage` | `None`（省略时使用 `config` 中的 `execution.slippage_bps`，默认 5 bps） | 滑点比例（例如 `0.001` = 0.1%） |
| `--random_slip` | `False` | 启用随机滑点（`[0, --slippage]` 均匀分布） |
| `--disable-routing-log` | `False` | 关闭逐 bar 路由 CSV 输出，用于参数优化批量跑 |

---

## 配置（`config/params.yaml`）

回测引擎会读取 `config/params.yaml` 来配置手续费、风控与路由映射
（`backtest/engine.py`）。以下为当前生效的关键默认值：

- **执行与费率**
  - `execution.commission_rate_taker` = `0.0005`（0.05%，taker 双边费率）
  - `execution.commission_rate_maker` = `0.0002`（0.02%，maker 双边费率）
  - `execution.slippage_bps` = `5`（默认滑点，5 个基点）
  - `execution.use_impact_cost`：是否启用简化冲击成本（默认关闭）
- **风控**
  - `risk.max_leverage` = `3.0`（最大杠杆，gross exposure / equity）
  - `risk.max_pos_size_pct` = `0.30`（单标的最大持仓占权益比例）
  - `risk.max_drawdown_limit` = `0.20`（日内回撤熔断阈值）
  - `risk.liquidity_limit_pct` = `0.01`（单笔订单占该 bar 成交量的最大比例）
- **路由映射**
  - `routing.TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE` → 策略名（例如 `"TrendBreakout"`）
  - `router.cooldown_bars` = `2`（状态切换后的冷却 bar 数）

> 以上数值随 `config/params.yaml` 演进，如与文件内容不一致，以文件为准。

---

## 输出与报告（`reports/`）

每次回测会在 `reports/` 下生成独立的时间戳文件夹（命名规则见 `main.py`），典型包含：

| 文件 | 说明 |
| --- | --- |
| `report.txt` | 核心指标 + 回测元信息 |
| `equity.csv` | 权益曲线（`timestamp, equity, cash`） |
| `trades.csv` | 逐笔成交（含手续费、滑点、`strategy_id`、`exit_reason`） |
| `benchmark.csv` | 基准净值（当前实现为"多标的等权买入并持有"） |
| `equity.png` | 净值/回撤/收益/资金占用四联图 |
| `data_quality_report.json` | 数据质量报告（缺失、重复、gap、spike 等） |
| `routing_log.csv` | 每根 bar 的 regime 与路由策略记录 |

---

## 关键机制

（与代码保持一致的"可落地描述"）

### 市场状态机（`core/state.py`）

- **状态集合**：`TREND_UP / TREND_DOWN / SIDEWAYS / VOLATILE`
- **识别逻辑**（简述）：
  - 趋势结构：`close` 与 SMA 快/慢线的结构关系
  - 强度过滤：`ADX > threshold`
  - 波动扩张：`ATR/close > atr_pct_threshold` 时覆盖为 `VOLATILE`
  - 去抖动：`stability_period` 连续确认后才切换状态

### 订单执行模型（`core/broker.py`）

- **Market**：bar $t+1$ 开盘价成交（叠加滑点）
- **Limit**：若触及限价则成交；开盘即可成交时按开盘价（taker），盘中触及时按限价（maker）
- **Stop**：触发后按更不利的开盘/触发价成交（taker）

更完整细节见 [`docs/backtest_assumptions.md`](docs/backtest_assumptions.md)。

---

## 当前能力边界

- **现货 vs 衍生品**：所有入口（`run_live.py`、安全配置默认值）目前都以**现货
  （spot）**为默认交易模式。底层 `core/exchange_boundary.py` 已经定义了衍生品
  相关的能力检测（`future/futures/swap/margin`），`run_live.py --market-type`
  也接受这些取值，但要在生产中真正启用合约/杠杆交易，还需要额外验证保证金、
  强平、`core/live_safety.py` 账户类型白名单等相关行为是否已经打通。术语定义
  见 [`docs/glossary.md` 第 9 节](docs/glossary.md#9-合约衍生品相关术语当前为能力预留尚未打通)。
- **多标的并发交易**：实盘引擎（`live_trading/engine.py`）和回测引擎均原生支持
  **同时**处理多个标的（如 `--symbols BTC/USDT ETH/USDT SOL/USDT ...`），并非依次
  轮询触发。仓位状态按标的独立记录（`core/portfolio.py`），仓位计算基于组合整体
  权益（会自动为已占用资金让路），风控（`core/risk.py`）在组合层面统一管控总杠杆
  和单标的集中度上限（默认 `max_pos_size_pct=30%`）——也就是说资金可以同时分散到
  多个币种、一起捕捉行情，而不是被锁定为单一持仓。详见
  [`docs/modules/core.md`](docs/modules/core.md) 中风控与组合相关章节。

---

## 项目结构

```text
QuantTradingV1/
├── main.py                    # 回测入口（CLI / 交互式）
├── run_live.py                # 实盘入口（轮询式）
├── resolve_live_order.py      # UNKNOWN 状态实盘订单的人工核实/恢复工具
├── config/
│   ├── config.py              # 配置加载器（读取 params.yaml）
│   └── params.yaml            # 手续费/风控/路由映射等
├── core/                      # 40+ 模块，按职责分组（完整清单见 docs/modules/core.md）
│   ├── data_fetcher.py / data.py      # 数据获取（Synthetic / Yahoo / CCXT）与质量校验
│   ├── state.py                       # 市场状态机（Regime）
│   ├── broker.py / live_broker.py     # 回测撮合 与 实盘 broker（CCXT）
│   ├── exchange_boundary.py           # CCXT 市场元数据/精度/衍生品能力的唯一边界层
│   ├── risk.py / persistent_risk_guard.py  # 风控与跨重启持久化风控状态
│   ├── portfolio.py                   # 组合/权益/敞口
│   ├── ledger.py / valuation.py       # 权威账本（fill/费用/持仓）与估值
│   ├── metrics.py / metric_result.py  # 绩效、交易质量、归因、稳健性验证指标
│   ├── events.py / event_store.py     # 事件模型、因果 ID、幂等消费与回放
│   ├── indicators.py                  # 指标库
│   ├── health.py / supervisor.py / alerting.py / telegram_heartbeat.py  # 心跳/重启/告警
│   └── reconciliation_job.py / sqlite_backup.py  # 日终对账与状态备份
├── strategies/                 # 策略插件（基类 + 实现）
├── router/                     # Regime → Strategy 的路由器
├── backtest/                   # 回测引擎与报告
├── live_trading/                # 实盘轮询引擎（导出 live_status.json）
├── analysis/                   # optimize.py（参数优化 + OOS）/ validation.py（walk-forward/bootstrap）/ plot_performance.py
├── dashboard/                   # 只读运维 Dashboard（消费 live_status.json，不控制交易）
├── scripts/                     # 环境检查、依赖锁校验等运维脚本
├── research/                    # 研究脚本（原型验证）
├── docs/                        # 文档
└── tests/                       # unittest 用例
```

---

## 文档

| 文档 | 说明 |
| --- | --- |
| [`docs/README.md`](docs/README.md) | 文档入口、权威层级和历史归档说明 |
| [`docs/modules/README.md`](docs/modules/README.md) | 逐包代码说明（各模块职责、关键类、模块间关系） |
| [`docs/unified_roadmap.md`](docs/unified_roadmap.md) | 唯一项目总路线图、R0–R8 和放行门槛 |
| [`docs/development_plan.md`](docs/development_plan.md) | 当前开发批次、任务顺序和验收产物 |
| [`docs/backtest_assumptions.md`](docs/backtest_assumptions.md) | 当前执行模型、费率/滑点、数据对齐与局限性 |
| [`docs/deployment.md`](docs/deployment.md) | 回测、sandbox 和运维安全边界 |
| [`docs/glossary.md`](docs/glossary.md) | 专业词汇表：执行/风控/绩效指标/交易质量/归因/稳健性验证/合约术语的中英文与计算口径 |

---

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 常见问题（FAQ）

- **CCXT/Yahoo 拉数失败**：`core/data_fetcher.py` 的 `DataFetcher` 会从环境变量
  `QUANT_PROXY_URL` 读取代理地址并设置 `HTTP_PROXY`/`HTTPS_PROXY`；未设置该环境
  变量时不使用代理。如果你需要代理但拉数仍失败，检查 `QUANT_PROXY_URL` 是否正确
  指向本机代理端口；也可以在创建 `DataFetcher(...)` 时显式传入 `proxy_url=None`
  或具体地址覆盖。
- **多标的无公共时间轴**：回测会优先按"时间戳交集"对齐；若检测为日线或更慢周期，
  会尝试按"日历日期"退化对齐（见 `backtest/engine.py`）。

---

## 免责声明

本项目仅用于研究、教学与工程演示；不构成投资建议。数字资产与衍生品交易风险极高，
可能导致全部损失。实盘部署前请自行补齐安全、风控与运维能力。
