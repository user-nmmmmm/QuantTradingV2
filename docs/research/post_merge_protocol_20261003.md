# 合并后固定回测对比协议

本次集成保留原工作区、旧实验目录和主分支已经完成的归档整理。提交范围包括现有工程与论文研究的增量，以及三标的回测启动、完整报告回放和逐笔对比所需修复；生成报告、行情和临时输出继续遵守忽略规则。

## 两套固定输入

主回测使用 `reports/architecture_followup_20261002/run_manifest.json` 保存的 BTC/ETH/SOL 输入快照：2024-01-01 至 2026-08-27，每个标的 970 根日线。资金 10,000 USDT、seed 42、预热 30、union、spot_margin、原健康控制、成本与期末标记政策保持登记值。输入通过 `load_data_snapshots(..., verify=True)` 加载，其 CSV 浮点精度采用 `round_trip`。不得使用普通 CSV 读取替代该加载器。

论文研究使用 `reports/paper_applications_20261003_v3/registration.json` 的原始 BTC/ETH 行情、七个候选、三个窗口、标准与 1.5 倍成本及元层支持门槛，完整运行 42 个窗口账户和一次长历史覆盖诊断。各窗口独立重启；健康关闭的研究参照与默认主回测分别比较。

原配置字节 SHA256 为 `49665830ead75ec74f6ef736f3f8b1ee7fdb3122ba9f334cda9d944b26decb4e`。Windows Git 的换行转换可能改变文件字节而不改变 Git 内容或参数。回放前应恢复原配置的确切字节并核对其哈希，同时确认 Git diff 为空；不得改写旧 manifest 或放宽严格配置校验。

## 执行与身份

合并前完成整仓质量流程、55% 覆盖率门槛和实际 `weekly-smoke --pin`。GitHub 上 `quality` 与离线回测工作流均通过后，使用 merge commit 合并，保留保护规则。

合并后从实际合并 SHA 建立干净工作树。全部新报告放入唯一目录 `reports/post_merge_20261003_<合并SHA前12位>/`；目录已存在就停止。其下分别保存 `main/`、`paper/`、`comparison/`。主回测只运行一次引擎，完整报告和摘要对比共享这次结果：

```text
python main.py --replay-manifest <旧主回测manifest绝对路径> --output-dir <新main目录> --report-profile full
python scripts/run_paper_applications.py --manifest <原行情manifest绝对路径> --output <新paper目录>
python scripts/compare_post_merge_backtests.py --old-main <旧main目录> --new-main <新main目录> --old-paper <旧paper目录> --new-paper <新paper目录> --output-dir <新comparison目录> --merge-sha <实际合并SHA>
```

回放保留原引擎 run_id 核对确定性，另登记新比较身份、合并 SHA、源码摘要、环境与生成时间。报告元数据变化和经济结果变化分别记录。收益提升不是验收要求；差异必须逐项定位或明确标为未解释。

## 对账与解释

主回测比较收益、权益、回撤、Sharpe、胜率、PF及样本状态、交易、佣金、融资、暴露、健康和生命周期，附共同时间轴图。逐笔优先按仓位身份配对，再核对标的、策略、方向和入场时间；身份语义冲突列为新增、删除或未匹配。

滑点已经进入成交价，不能在净损益上再次扣减；融资与交易费用分别核对。分析全部闭合交易和成交、年份和标的贡献、退出原因、连续亏损、盈利集中度及最大亏损。2026 年无成交必须结合原始信号、路由及健康门控证据解释。未知市场状态、无法逐笔分配的融资或无法确认的代码归因保留未知，不以推断补齐。

论文研究按同名候选、窗口和成本配对，比较统计、成本压力、P1/P2覆盖和标签，并保留主要亏损及交易差异。真实交易、未成熟最终样本、独立行情核验及策略准入边界继续保持原登记状态。
