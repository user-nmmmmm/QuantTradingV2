# 回测性能优化与验证（2026-09-22）

后续性能与内存优化见 [第二轮测量](backtest_performance_memory_20260922.md)。下文保留第一轮测量口径与结果。

本次在工作区现有版本上做性能优化，不修改交易、风控、撮合或费用参数。固定场景中，回测与默认 Excel 报告的合计耗时中位数由 **8.399 秒降至 4.922 秒**，耗时减少 **41.4%**，速度约为原来的 **1.71 倍**。

## 改动

1. `core/benchmarks.py`：动态等权基准使用预分配 NumPy 数组计算，最后一次性构造 pandas 输出，省去逐时间点创建 Series、标签对齐和赋值的开销。保留原来的权重漂移、现金换手、费用、缺失行情和预热规则。
2. `backtest/reporting/__init__.py`：默认 `workbook` 模式直接生成 Excel 原生图表，跳过随后会删除的四张 PNG、文本报告及原始行情/交易/基准 CSV。最终工作簿、指标与核对 JSON、平仓交易 CSV 继续输出；旧产物清理及 `compact` / `full` 模式保持原行为。
3. `analysis/optimize.py`：候选评分只使用账户绝对指标，因此关闭不会返回给调用方的基准计算。
4. `analysis/walk_forward.py`：窗口默认跳过不使用的基准计算；显式传入 `engine_kwargs={"calculate_benchmarks": True}` 仍会计算基准。
5. `scripts/benchmark_backtest.py`：提供离线固定数据测速、结果一致性比较和可选 cProfile 输出。

## 配对测量

- 数据：固定 seed `20260812`，10 个合成标的 `ASSET0/USDT` 至 `ASSET9/USDT`，每个 720 根日线。
- 引擎：当前配置，初始资金 10,000，预热 30 根，固定 run ID，不启用随机滑点，关闭路由日志；每次产生 1,068 笔成交。
- 报告：`workbook`，包含交易明细和基准。
- 环境：Windows 11，Python 3.13.2，pandas 2.3.3，NumPy 2.4.2。
- 方法：使用本次修改前保存的源码与优化后源码，在同一进程中交替运行，各测三次。计时仅覆盖 `BacktestEngine.run()` 和 `ReportGenerator.generate()`，不包含进程启动、导入、数据生成/下载和额外的结果核对。期间暂停了本任务其他测试。

| 阶段 | 优化前中位耗时 | 优化后中位耗时 | 耗时减少 |
| --- | ---: | ---: | ---: |
| 回测引擎 | 4.750 s | 4.064 s | 14.4% |
| 默认 Excel 报告 | 3.436 s | 0.867 s | 74.8% |
| 每次回测与报告合计 | 8.399 s | 4.922 s | **41.4%** |

合计列按每次总耗时取中位数，因此不等于两阶段中位数简单相加。三个原始总耗时为：优化前 `7.9489 / 8.3993 / 8.4915` 秒；优化后 `4.9101 / 4.9220 / 5.1878` 秒。

动态基准单独测量：10 个标的 × 720 根行情、3% 缺失价格、预热 30 根、成本 10 bps，预热后交替测量五次，耗时中位数 `0.88504 s → 0.02845 s`，该模块约加速 **31.1 倍**。这一倍数只适用于动态基准计算。

独立的无成交 Excel 报告场景中，720 根日线生成中位耗时 `2.7744 s → 0.3584 s`；完整回测包含较多成交，使用上表的配对结果作为主要结论。实际提速随标的数量、历史长度、交易量、报告模式和机器负载变化。

## 正确性验证

- 冻结原 pandas 基准算法作为独立对照：普通/nullable 浮点数、整数、混合列、稀疏及无效行情、NaN/Infinity、预热、起始索引和成本场景；净值、权重、换手、成本与元数据采用精确相等检查。
- 固定引擎 v4 基线与同进程确定性、共享运行时回放、真实时间轴、订单时序、防前视测试通过；未重新生成历史基线。
- 参数搜索及滚动窗口分别与启用基准计算的真实引擎对照，候选输出、交易数及收益序列精确一致；显式基准开关继续生效。
- 配对测量的交易、权益、两类基准、权重、换手、费用、会计核对、融资与保证金账本、平仓计数以及报告指标通过既有严格浮点容差的结构比较；Excel 所有单元格内容完全一致，原生图表存在。
- 独立报告对照中，保留的 CSV/JSON 文件字节一致；工作簿除生成时间元数据外，所有 ZIP 部件（数据、图表、样式及关系）字节一致。
- workbook 不再调用无用渲染器，旧文件仍清理；compact/full 仍输出所需图表；三个模式的 `metrics_only` 均不生成文件。
- 修改范围的 Ruff 检查和空白错误检查通过。

本任务运行的主要验证组（部分现有测试在组间重复）：

```powershell
.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_research_performance_equivalence.py tests/test_walk_forward.py tests/test_roadmap_optimizer_execution.py -q
# 23 passed

.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_backtest_regression.py tests/test_backtest_engine.py tests/test_phase2_reproducibility_audit.py tests/test_roadmap_system_metrics.py tests/test_p1_shared_runtime_replay.py tests/test_p1_timeline_orders.py tests/test_no_lookahead.py -q
# 58 passed, 11 subtests passed

.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_benchmark_performance_equivalence.py tests/test_phase2_reproducibility_audit.py tests/test_roadmap_system_metrics.py -q
# 61 passed

.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_report_profile_performance.py tests/test_pdf_report.py -q
# 10 passed

.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_benchmark_cli.py -q
# 12 passed
```

## 今后复测

以下命令只测引擎，不包含 Excel 报告阶段；数据离线生成，配置和输入数据摘要会保存到 JSON。

```powershell
.venv/Scripts/python.exe scripts/benchmark_backtest.py --bars 720 --symbols 10 --repeats 3 --output outputs/perf_baseline.json

# 后续代码修改后，用相同参数比较：
.venv/Scripts/python.exe scripts/benchmark_backtest.py --bars 720 --symbols 10 --repeats 3 --reference outputs/perf_baseline.json --output outputs/perf_current.json --profile outputs/perf_current.prof
```

同进程重复运行要求结果完全一致；跨版本参考使用现有引擎基线的严格浮点容差。输入、配置不匹配或回测结果有差异时返回失败，不把不同工作量作为有效加速证据。profile 另跑一次，不混入正常计时。

本次本地测量与原始源码快照保存在 `outputs/backtest_performance_20260922/`（生成产物目录，不进入 Git）：

- `paired_measurements.json`：三轮完整配对测量。
- `measure_end_to_end.py`：复现配对测量，依赖同目录的原始代码快照。
- `benchmark_microbenchmark.json`：动态基准单独测量。
- `workbook_before.json`、`workbook_after.json`：独立报告计时和产物校验。
- `before.prof`、`before_profile.txt`：修改前的瓶颈分析；profile 自身会增加耗时，不用于上表速度比较。
