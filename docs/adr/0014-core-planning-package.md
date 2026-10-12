# 0014 选币、打分与分配移到 `core/planning/`；`portfolio`、`valuation` 归入 `core/trading/`（B2 第 3 簇）

- 状态：已采纳
- 日期：2026-10-13
- 相关：[基础设施路线图](../infrastructure_roadmap.md) B2，[ADR 0012](0012-core-market-package.md)、[ADR 0013](0013-core-trading-package.md)

## 背景
B2 前两簇之后，`core/` 根还剩 26 个平铺模块。其中一组只做"从候选里选哪些、各分多少、目标仓位是多少"：选币（`selection`、`selection_v2`）、候选打分、分配（含成本感知）、目标仓位及其控制器。它们被 `composition`、`strategies`、`router`、`analysis`、`scripts` 和测试使用，`core` 内部几乎没有别的模块依赖它们。`portfolio`（账户现金/持仓/批次状态）和 `valuation` 描述的是账户事实，与 `core/trading/` 里的 `accounts`、`lots`、`state` 同类，而不是"规划"。

## 决定
- 新建子包 `core/planning/`（仍属 `core` 层，`__init__.py` 为空），搬入 7 个模块，**模块名不变**：`selection`、`selection_v2`、`candidate_scoring`、`allocation`、`cost_aware_allocation`、`target_position`、`portfolio_target_controller`。
- `portfolio`、`valuation` 搬到已有的 `core/trading/`（ADR 0013 的补充，不是新簇）。
- 不留垫片，所有导入点一次性改写（含 `assertLogs("core.planning.allocation")` 这类按模块名的 logger 字符串）。
- **回放指纹键保持旧名**：`backtest/signal_meta_replay.py` 的 `core/portfolio.py` 键不变，只改读取位置；测试固定键集合。
- 批量改写不得触碰哈希固定的输入：`config/` 下非 `.py` 的文件（`params.yaml` 的哈希就是冻结的 `config_hash`）和 `tests/fixtures/` 都被排除在改写之外（B2-2 事故后加入的保护）。
- 冻结的历史证据不改写。

## 备选方案
- 把 `portfolio` 也放进 `planning`：它是账户状态而不是规划，会让 `trading` 与 `planning` 之间出现反向依赖。
- 把 `selection` 与 `allocation` 分成两个包：互相引用多（`target_position` 依赖二者），拆开会产生环。

## 后果
- 行为身份报告显示为"移动 9 个文件、若干文件仅导入变化"，行为变化只来自回放指纹的文件映射。
- 这些模块的 logger 名随路径变化。
- `core/` 根平铺模块 26 → 17。
