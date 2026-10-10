# 回测性能优化记录(合并)

> 合并自 2026-09-22 至 09-26 的四份性能优化与验证记录。
> 合并说明:各节由下列原文档原样并入(仅调整标题层级与相对链接;原有章节锚点可能变化),原路径可在 Git 历史查到。


---

<!-- 合并自 docs/backtest_performance_20260922.md -->
## 回测性能优化与验证（2026-09-22）

后续性能与内存优化见 [第二轮测量](backtest_performance.md)。下文保留第一轮测量口径与结果。

本次在工作区现有版本上做性能优化，不修改交易、风控、撮合或费用参数。固定场景中，回测与默认 Excel 报告的合计耗时中位数由 **8.399 秒降至 4.922 秒**，耗时减少 **41.4%**，速度约为原来的 **1.71 倍**。

### 改动

1. `core/benchmarks.py`：动态等权基准使用预分配 NumPy 数组计算，最后一次性构造 pandas 输出，省去逐时间点创建 Series、标签对齐和赋值的开销。保留原来的权重漂移、现金换手、费用、缺失行情和预热规则。
2. `backtest/reporting/__init__.py`：默认 `workbook` 模式直接生成 Excel 原生图表，跳过随后会删除的四张 PNG、文本报告及原始行情/交易/基准 CSV。最终工作簿、指标与核对 JSON、平仓交易 CSV 继续输出；旧产物清理及 `compact` / `full` 模式保持原行为。
3. `analysis/optimize.py`：候选评分只使用账户绝对指标，因此关闭不会返回给调用方的基准计算。
4. `analysis/walk_forward.py`：窗口默认跳过不使用的基准计算；显式传入 `engine_kwargs={"calculate_benchmarks": True}` 仍会计算基准。
5. `scripts/benchmark_backtest.py`：提供离线固定数据测速、结果一致性比较和可选 cProfile 输出。

### 配对测量

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

### 正确性验证

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

### 今后复测

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


---

<!-- 合并自 docs/backtest_performance_memory_20260922.md -->
## 第二轮回测性能与内存优化（2026-09-22）

在第一轮基准计算、报告与参数搜索优化基础上，本轮进一步优化历史行情索引、事件对象及引擎内部记账。与本轮开始时的版本比较，固定回测场景耗时减少 **26.4%**，回测新增分配峰值减少 **13.6%**；长期行情适配器的保留分配量减少约 **84%**。

### 整体回测测量

固定 seed `20260812`，10 个合成标的、各 720 根日线，当前相同配置和固定 run ID，关闭路由日志，保留基准计算。每次产生 **1,068 笔成交、10,678 条事件**。

| 指标 | 本轮优化前 | 本轮优化后 | 减少 |
| --- | ---: | ---: | ---: |
| 引擎运行耗时中位数 | 4.0895 s | 3.0115 s | **26.4%** |
| 回测新增分配峰值 | 25.34 MiB | 21.89 MiB | **13.6%** |
| 回测结束时保留的新增分配 | 24.76 MiB | 21.55 MiB | **12.9%** |

测量方法：

- 优化前后的源文件分别加载到独立 Python 进程中；使用本轮修改前保存的源码快照，不用 Git HEAD 代替工作区基线。
- 计时进程前后交替运行，各三次。原始耗时：优化前 `4.1171 / 3.8897 / 4.0895` 秒，优化后 `3.0115 / 3.0104 / 3.1326` 秒。
- 耗时只覆盖 `BacktestEngine.run()`，不包含启动、导入、数据生成、报告和结果序列化。
- 内存各另跑一个独立进程，原始数据准备完成后清理无用对象，再开启 `tracemalloc(1)`，覆盖引擎创建与运行。保留引擎和结果读取当前/峰值分配，随后关闭追踪，再做结果序列化。
- 内存数据是被追踪的新增 Python/NumPy 分配，不是整个进程 RSS；不包括预先加载的解释器、依赖库、输入数据和追踪器自身开销。
- 内存追踪会拖慢程序，因此追踪运行的耗时未进入速度统计。
- 环境与第一轮相同：Windows 11、Python 3.13.2、pandas 2.3.3、NumPy 2.4.2。测量期间停止本任务其他重型测试。

### 实现

#### 历史行情索引和标准化

`core/market_data.py` 原来为每标的每根 K 线保留 Python `Timestamp → int` 字典。现在每个流独立按最多 **2,048 根时间戳**分块，通过有序索引生成原生整数位置数组；该块处理完即释放，不建立全历史稠密矩阵。

行情标准化仍保留独立数据副本，只有确有无效日期、重复索引或乱序时才追加筛选和排序。`bars` 仍是原生 pandas Series；union/intersection、时区起点、原历史行号、重复/交错读取和策略中途添加列保持一致。

独立扩展场景：20 个标的 × 10,000 根 K 线，关闭指标计算，输入准备完成后开始追踪适配器分配：

| 适配器阶段 | 优化前 | 优化后 | 减少 |
| --- | ---: | ---: | ---: |
| 构造后保留内存 | 48.03 MiB | 7.67 MiB | 84.0% |
| 读取首个事件后保留内存 | 49.28 MiB | 8.27 MiB | 83.2% |

该比例只反映行情适配器，不代表整个回测的内存减少比例。10 × 720 小场景的流遍历时间基本持平，主要收益是初始化和长历史内存。

#### 引擎敞口采样与强制减仓回调

`backtest/engine.py` 在权益行生成时直接累计总敞口、净敞口、已定价持仓数和两个权益比例，消除两份历史持仓/价格字典及结束时的二次遍历和 join。敞口存储从随“历史长度 × 持仓数”增长变为随历史长度增长。

保持原先数值计算顺序、缺失价格规则、零权益处理，以及正常时序、熔断冻结尾部和期末合成平仓点的敞口记录。独立 `calculate_exposure()` 接口保持原行为。

强制减仓后策略回调直接复用 `event.positions`；兼容缺少行号的事件时，每个标的仅查找一次。原固定场景里该路径执行了 6,120 次重复时间索引查找，现在正常历史回测无需这些查找。

#### 事件存储与规范化

- `EventEnvelope`、`OrderEvent`、`FillEvent` 使用 slots，减少逐对象属性字典；支持旧 dict-state 与新 slots-state pickle，未修改仍依赖 `__dict__` 的 `OrderIntent`。
- 发布时 payload 完整校验和冻结一次，构建事件封装时不再重复规范化。直接构造与 `dataclasses.replace()` 仍执行完整校验。
- 默认 `StructuredPayload` 路径去掉重复递归；自定义构造器或 Mapping 视图保留旧路径。
- 字符串 UUID 转换使用最多 **1,024 项**的缓存，复用相同关联标识；UUID 算法、用途区分和特殊字符串子类行为保持一致。
- 事件数量、字段、审计留存、幂等冲突检测和持久化策略保持原行为。

独立 10,000 个订单事件场景，耗时中位数 `0.7771 s → 0.4604 s`，事件分配峰值减少约 13.0%。这是事件模块测量，不等同于整体回测速度。

### 正确性与测试

全部八次整体测量（六次计时、两次内存）均比较了交易、权益、基准、会计核对、融资/保证金账本、生命周期、策略健康、止损/分配/风险审计，以及完整事件序列。

仅忽略事件的墙钟观测时间 `observed_at`；事件 ID、关联/因果 ID、业务时间、类型和 payload 均参与比较。数值沿用既有严格浮点容差；另外五类固定事件的完整 JSON SHA256 与修改前逐字节一致。

验证覆盖：

- 行情 union/intersection、稀疏索引、时区、混合/扩展类型、输入隔离、并发/重复流、中途新增列及内存上限。
- 引擎固定基线、敞口、期末处理、非同步交易事实、V3 终值策略及强制减仓。
- 规范事件、权威账本、风险预留、持久化恢复、本地闭环与安全边界。
- 嵌套可变输入隔离、非有限数值及无时区日期拒绝、新旧 pickle、UUID 缓存上限。
- 测速工具的重复一致性、历史参考、独立 profile 和独立内存运行。

主要验证组（有重叠）：行情与运行时组 **73 passed / 5 subtests**；事件与持久化组 **125 passed**；引擎/敞口/终值组 **53 passed / 6 subtests**；新增引擎合同 **17 passed**；测速工具、引擎合同与固定基线组 **44 passed / 6 subtests**。修改范围静态检查通过。未执行全仓库全量测试。

### 内存复测工具

已有 `scripts/benchmark_backtest.py` 新增 `--memory`：

```powershell
.venv/Scripts/python.exe scripts/benchmark_backtest.py --bars 720 --symbols 10 --repeats 3 --memory --output outputs/perf_memory_baseline.json

# 后续优化后使用同一输入、配置比较：
.venv/Scripts/python.exe scripts/benchmark_backtest.py --bars 720 --symbols 10 --repeats 3 --memory --reference outputs/perf_memory_baseline.json --output outputs/perf_memory_current.json
```

内存采样独立于计时运行，并在序列化之前结束。工具报告明确区分新增追踪分配与整个进程 RSS；该命令的内存运行在计时重复之后执行，因此不统计已存在的缓存。只有参考的内存统计口径一致时才显示峰值变化比例。配置、数据或结果不一致时拒绝速度比较。

本轮隔离进程实测与源码快照保存于 `outputs/backtest_performance_round2_20260922/`：

- `paired_measurements.json`：整体速度、保留/峰值分配及一致性结果。
- `timing_before_*.json`、`timing_after_*.json`、`memory_before.json`、`memory_after.json`：包含交易及审计结果的原始记录。
- `measure_round2.py`：加载原始源码快照或当前源码的隔离测量工具。
- `adapter_measurements.json`、`event_measurements.json`：独立组件测量。
- `before.prof`、`before_profile.txt`：本轮开始时的性能分析。

该目录是本地产物目录，不纳入 Git。第一轮报告保留在 `docs/backtest_performance_20260922.md`。


---

<!-- 合并自 docs/backtest_event_performance_20260924.md -->
## 回测事件发布性能优化（2026-09-24）

本轮针对回测订单与成交事件的发布路径。`OrderEvent`、`FillEvent` 是冻结的 dataclass；普通实例的字段均为不可变标量，构造时已经完成数值校验。以前每次发布仍递归复制全部字段并重新构造对象。现在 `core/events/types.py` 先核对**精确类型和值的有效性**，符合条件时复用该对象；自定义子类、可变字段或被异常修改的对象继续走原有递归冻结与校验路径。

### 测量

固定输入包含 6,374 个 `OrderEvent` 和 1,068 个 `FillEvent`，总计 7,442 个事件，与离线回测中这两类事件的数量一致。事件提前构造；计时只覆盖带显式幂等键、固定时钟及订阅者的 `TradingEventPipeline.publish()`。在同一 Python 进程中交替使用修改前后的规范化函数，各测四次，并逐事件比较输出。

| 指标 | 修改前中位数 | 修改后中位数 | 变化 |
| --- | ---: | ---: | ---: |
| 事件发布墙钟耗时 | 0.2144 s | 0.1151 s | 减少 46.3%，约 1.86 倍速度 |
| 事件发布 CPU 耗时 | 0.2188 s | 0.1172 s | 约 1.87 倍速度 |

这个数字只适用于上述事件发布场景，不代表完整回测提速比例。720 根日线 × 10 标的的完整回测做过交替测量，但不同轮次的整体耗时波动较大，因此本轮不宣称可靠的完整回测提速。

### 等价性与边界

- 同进程比较修改前后回测的交易、权益、基准、会计核对等结果，以及完整事件序列；仅排除由墙钟生成的 `observed_at` 字段，其他事件字段逐项相等。
- 既有事件编解码固定 SHA256、pickle 兼容、幂等、订单账本及回测基线测试通过：`107 passed, 6 subtests passed`。
- 新增测试覆盖普通冻结事件的复用、可变可选字段的隔离，以及通过 `object.__setattr__` 异常修改后仍拒绝非法值。
- 修改范围的 Ruff 检查和 `git diff --check` 通过。未运行全仓库测试。

本地配对数据与测量脚本存放在 `outputs/performance_20260924/`，该目录不纳入 Git。主要结果见 `event_publish_benchmark.json`；`event_parity_final.json` 记录最终代码的完整回测和事件序列等价性核对。


---

<!-- 合并自 docs/backtest_kline_performance_20260926.md -->
## 历史 K 线回测快路径（2026-09-26）

`BacktestEngine` 默认启用 `fast_bars=True`。历史行情适配器仍逐事件读取当前
DataFrame，因此策略在回测期间新增或替换的列对后续 K 线可见。快路径将 pandas
原本整行读取时完成的类型提升结果保存为独立行快照，常用字段读取省去重复的
`Series.__getitem__` / `Series.get` 开销。需要原生 pandas 行的调用方可使用
`BacktestEngine(fast_bars=False)`；直接调用适配器的 `stream()` 默认仍返回 Series。

适配器对完全对齐的时间轴直接使用连续行号；稀疏时间轴继续分块查找。
当行值不是 NumPy 数组、列名重复或 pandas 内部快速行读取不可用时，快路径退回
原生 Series。清算等需要复制 K 线的路径可从快照生成原生 Series。

### 配对测量

使用 `scripts/benchmark_kline_modes.py` 在同一个 Python 进程中交替运行普通和
快速模式；每次新建引擎、深拷贝相同输入。计时仅覆盖 `BacktestEngine.run()`，
不含数据生成、结果序列化和正确性核对。固定 seed 为 `20260812`，关闭路由日志，
启用现有基准计算。速度比是各配对速度比的中位数，因此可能与两列耗时中位数的
比值略有差异。

| 场景 | 配对次数 | 普通模式耗时中位数 | 快速模式耗时中位数 | 配对耗时减少 |
| --- | ---: | ---: | ---: | ---: |
| 10 标的 × 720 根 | 3 | 2.867 s | 2.419 s | 17.0% |
| 20 标的 × 2,000 根 | 2 | 14.030 s | 11.187 s | 20.2% |

两组均比较了核心回测结果、资金与执行审计，以及排除墙钟 `observed_at` 后的完整事件序列；
分别有 10,678 和 37,829 条事件，结果一致。最后的边界处理改动后，单次配对复测
10 × 720 根耗时减少 18.5%，20 × 2,000 根减少 20.3%；两次结果和事件仍一致。
不同机器、行情结构、策略、成交量与系统负载会改变提速幅度。

### 正确性与复测

- 行情适配器和回测基线组：38 passed、6 subtests passed。
- 引擎、无前视、日内保护止损和 V3 集成/期末处理组：34 passed。
- 快速与普通模式的端到端结果及事件等价性测试：1 passed。
- 修改范围 Ruff 检查与 `git diff --check` 通过；未运行全仓库测试。

```powershell
.venv/Scripts/python.exe scripts/benchmark_kline_modes.py --bars 720 --symbols 10 --pairs 3 --output outputs/kline_modes.json
.venv/Scripts/python.exe scripts/benchmark_kline_modes.py --bars 2000 --symbols 20 --pairs 2 --output outputs/kline_modes_large.json
```

工作区原有改动很多，旧基线是在本轮修改前的同一工作区上测得；本页的速度结论只使用
同进程普通/快速模式配对结果，不把不同系统构建版本间的独立测速当成可靠提速证据。
