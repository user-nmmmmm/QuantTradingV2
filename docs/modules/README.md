# 模块文档导航

本目录逐包说明代码库的职责划分，作为「读代码」与「读路线图/验收文档」之间的中间层——回答"这个包/文件是做什么的、关键类是什么、和其他模块怎么交互"，不涉及项目当前阶段或验收状态（那些以 [`../unified_roadmap.md`](../unified_roadmap.md) 等文档为准）。

| 文档 | 覆盖范围 |
| :--- | :--- |
| [`core.md`](core.md) | `core/`：数据获取与校验、状态机、事件驱动运行时、经纪商/执行、订单与风控、账本与对账、可观测性与运维安全（34 个文件，最核心也是最大的包） |
| [`backtest.md`](backtest.md) | `backtest/`：回测调度引擎、模拟执行、报告生成 |
| [`live_trading.md`](live_trading.md) | `live_trading/`：实盘轮询引擎、执行适配器 |
| [`router.md`](router.md) | `router/`：市场状态 → 策略的路由与切换风控 |
| [`strategies.md`](strategies.md) | `strategies/`：策略基类与四个具体策略实现 |
| [`analysis_dashboard_research_config.md`](analysis_dashboard_research_config.md) | `analysis/`、`dashboard/`、`research/`、`config/`，以及根目录的 `main.py`/`run_live.py` 入口脚本 |

## 阅读建议

- 想先建立整体图景：从 [`core.md`](core.md) 末尾的"模块关系速览"开始，再看 [`backtest.md`](backtest.md) 或 [`live_trading.md`](live_trading.md) 了解回测/实盘各自的调度外壳。
- 想了解某个具体策略怎么触发/怎么出场：看 [`strategies.md`](strategies.md) 和 [`router.md`](router.md)。
- 想了解风控/资金安全边界：看 [`core.md`](core.md) 里"四、订单、状态机与风控"和"七、可观测性、运维安全与验收"两节。
- 想了解现货/多币种等当前系统能力边界：见根目录 [`../../README.md`](../../README.md) 的「当前能力边界」一节。

这些文档基于当前代码库结构手工整理，不是自动生成；代码演进后如与文档不符，以代码为准，并欢迎更新对应文档。
