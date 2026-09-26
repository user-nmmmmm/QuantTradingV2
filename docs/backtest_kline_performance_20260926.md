# 历史 K 线回测快路径（2026-09-26）

`BacktestEngine` 默认启用 `fast_bars=True`。历史行情适配器仍逐事件读取当前
DataFrame，因此策略在回测期间新增或替换的列对后续 K 线可见。快路径将 pandas
原本整行读取时完成的类型提升结果保存为独立行快照，常用字段读取省去重复的
`Series.__getitem__` / `Series.get` 开销。需要原生 pandas 行的调用方可使用
`BacktestEngine(fast_bars=False)`；直接调用适配器的 `stream()` 默认仍返回 Series。

适配器对完全对齐的时间轴直接使用连续行号；稀疏时间轴继续分块查找。
当行值不是 NumPy 数组、列名重复或 pandas 内部快速行读取不可用时，快路径退回
原生 Series。清算等需要复制 K 线的路径可从快照生成原生 Series。

## 配对测量

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

## 正确性与复测

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
