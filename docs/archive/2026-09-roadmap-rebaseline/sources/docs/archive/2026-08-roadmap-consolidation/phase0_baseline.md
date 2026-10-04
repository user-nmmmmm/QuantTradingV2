# Phase 0 基线记录

记录日期：2026-07-22

## 工作区保护

执行 Phase 0 时发现工作区已有大量未提交的业务代码、报告和文件删除改动。这些改动早于本次 Phase 0 工作，因此未执行以下破坏性操作：

- 未删除 `archive/`；
- 未删除历史回测报告；
- 未删除 `.venv/` 或 `venv/`；
- 未删除 Git 已跟踪的 `__pycache__` 文件；
- 未恢复、覆盖或暂存任何既有改动。

后续经人工确认后，才可使用 `git rm --cached` 将已跟踪的生成文件从版本控制中移除。

## 编码与格式规范

新增 `.editorconfig`，要求新增和修改的文本文件使用 UTF-8、LF 换行和结尾换行；Python 使用 4 空格缩进，YAML/JSON/Markdown 使用 2 空格缩进。

未对现有文件做批量编码转换，避免覆盖当前未提交的 README 与源码修改。后续应在确认差异后，将确认存在乱码的文件逐个转换为 UTF-8。

## 可重复测试基线

执行命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

结果：41 项测试，40 项通过，1 项失败。

| 状态 | 测试 | 说明 |
| --- | --- | --- |
| 通过 | 40 项 | 指标、数据源代理、订单撮合、风险、路由、实盘 broker 和工厂配置测试均通过。 |
| 失败 | `tests.test_no_lookahead.TestNoLookahead.test_execution_timing` | 断言失败：`No trades generated`。该测试期望验证 Next-Bar Execution，但当前回测未生成成交；需要在 Phase 1 调查策略路由/入场风险校验或测试夹具。 |

## 后续基线操作

在修复上述失败测试后，使用固定参数生成回归基准：

```powershell
.\.venv\Scripts\python.exe main.py --source synthetic --days 120 --capital 10000 --symbols BTC-USDT ETH-USDT --seed 42
```

将生成目录中的 `report.txt`、`equity.csv`、`trades.csv` 与 `routing_log.csv` 保存为人工审核的基准样本；运行产物不应提交到 Git。
