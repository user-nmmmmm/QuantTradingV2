# 0012 行情与时间/标的约定移到 `core/market/`（B2 第 1 簇）

- 状态：已采纳
- 日期：2026-10-12
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B2，[ADR 0007](0007-ops-package.md)、[ADR 0011](0011-signal-meta-package.md)

## 背景
B2 的目标是让 `core/` 不再有平铺模块（B1 之后还剩 63 个）。依赖图（含函数内延迟导入）把它们分成几簇，行情/数据簇最清楚：这些模块都只向下依赖 `core` 内部的少数模块，被 `backtest`、`live_trading`、`analysis`、`scripts` 和测试使用，`core` 内部彼此的依赖也集中在簇内。

## 决定
- 在 `core/` 里新建子包 `core/market/`（仍属于 `core` 层，规则不变；包的 `__init__.py` 保持为空，避免门面导入形成环），搬入 17 个模块，**模块名不变**：`data`、`data_fetcher`、`market_data`、`market_data_errors`、`market_read_batch`、`websocket_market`、`derivatives_data`、`public_data_clients`、`quote_observations`、`quote_borrow`、`binance_depth_observer`、`temporal_data`、`data_versions`、`trend_portfolio_data`、`universe`、`timeframes`、`request_budget`。导入路径 `core.data_fetcher` → `core.market.data_fetcher`。不留垫片：所有导入点（含测试里的 `patch("…")` 字符串和 `from core import x as module`）一次性改写。
- 相对仓库根定位的代码要同步修正：`request_budget.deployment_budget()` 原用 `parents[1]` 找仓库根，搬深一层后改为 `parents[2]`，由 `tests/test_market_package.py` 固定默认数据库仍在 `<仓库根>/reports/`。搬迁前已检查所有搬迁模块里的 `__file__`、`__module__`、pickle、动态导入，只有这一处受影响。
- 冻结的历史证据（`docs/archive/2026-08*`、`2026-09*`、三个冻结 JSON、记录当时文件哈希的 `paper_continuation_acceptance_20261003.json`）不改写。

## 备选方案
- 按"数据 / 适配 / 时间"再细分：模块间交叉依赖多，拆细只会增加跨包导入；先按簇搬，之后再看是否需要二级拆分。
- 保持 `core.market_data` 之类旧路径并加垫片：多一轮清理，且新旧路径并存会让导入规则难以检查。

## 后果
- 行为身份报告显示为"移动 17 个文件、若干文件仅导入变化"，行为变化只来自 `request_budget` 的路径修正。
- **已登记的纸面观察实验**（`reports/paper_forward_observation_20261003`）按设计会校验源码哈希；其源码清单里的 `core/quote_observations.py`、`core/request_budget.py`、`core/data_versions.py` 已移位，且 `analysis/` 里相关文件的导入也变了，因此该实验不能再 `--resume` 追加会话，需要按脚本提示登记新协议。该实验的两次采集已结束，历史证据保留不动。
- 日志里这些模块的 logger 名（`__name__`）随路径变化。
- 之后的簇（订单与账户状态、选币与组合、指标与审计、基础件）按同一方法各出一条 ADR。
