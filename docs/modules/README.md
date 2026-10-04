# 模块文档导航

本目录逐包说明代码库的职责划分，作为「读代码」与「读路线图/验收文档」之间的中间层——回答"这个包/文件是做什么的、关键类是什么、和其他模块怎么交互"，不涉及项目当前阶段或验收状态（那些以 [`../unified_roadmap.md`](../unified_roadmap.md) 等文档为准）。

| 文档 | 覆盖范围 |
| :--- | :--- |
| [`core.md`](core.md) | `core/`：行情适配、共享决策运行时、经纪商接口与实现、订单风控、运维基础设施及其边界 |
| [`backtest.md`](backtest.md) | `backtest/`：历史调度、模拟执行、保护止损、逐 bar 核算与报告 |
| [`live_trading.md`](live_trading.md) | `live_trading/`：实盘轮询、订单恢复、账户/保护单对账、执行适配器和状态导出 |
| [`router.md`](router.md) | `router/`：市场状态 → 策略的路由与切换风控 |
| [`strategies.md`](strategies.md) | `strategies/`：基类接口与成交生命周期、默认策略规则、TrendPortfolioV2/V3、配对研究、配置和扩展验证 |
| [`analysis_dashboard_research_config.md`](analysis_dashboard_research_config.md) | `analysis/`、`dashboard/`、`research/`、`config/`，以及根目录的 `main.py`/`run_live.py` 入口脚本 |
| [`../authoritative_ledger.md`](../authoritative_ledger.md) | 离线事件账本的设计约束、计算口径及与交易运行时的集成边界 |
| [`../../dashboard/FRONTEND.md`](../../dashboard/FRONTEND.md) | 本地网页工作台的前后端模块、HTTP 接口、实验持久化和验证命令 |

## 阅读建议

- 想先建立整体图景：从 [`core.md`](core.md) 末尾的"模块关系速览"开始，再看 [`backtest.md`](backtest.md) 或 [`live_trading.md`](live_trading.md) 了解回测/实盘各自的调度外壳。
- 想了解某个具体策略怎么触发/怎么出场：看 [`strategies.md`](strategies.md) 和 [`router.md`](router.md)。
- 想了解风控/资金安全边界：看 [`core.md`](core.md) 里"四、订单、状态机与风控"和"七、可观测性、运维安全与验收"两节。
- 想了解现货/多币种等当前系统能力边界：见根目录 [`../../README.md`](../../README.md) 的第 10 节「能力边界」。
- 想找到数据抓取、验收或研究脚本：见 [`../../scripts/README.md`](../../scripts/README.md) 的用途索引。
- 想区分交易持仓事实与离线事件账本：先看 [`core.md`](core.md) 的“账本、组合与快照”，再看 [`../authoritative_ledger.md`](../authoritative_ledger.md)。
- 想理解论文研究如何使用时间版本和执行证据：先看 [`core.md`](core.md) 的行情与时间版本说明，再看 [`analysis_dashboard_research_config.md`](analysis_dashboard_research_config.md)；批次结果从[文档总索引](../README.md#最新行为变更)进入。
- 想运行网页回测或维护界面：先看[工作台使用说明](../../dashboard/README.md)，再看[前端工程说明](../../dashboard/FRONTEND.md)。

这些文档基于当前代码库结构手工整理，不是自动生成；代码演进后如与文档不符，以代码为准，并欢迎更新对应文档。
