# 脚本索引

先在仓库根目录执行 `python -m pip install -e . --no-deps`（可编辑安装），之后用 `python scripts/<脚本名>.py` 运行，脚本不再依赖当前目录或 `sys.path` 补丁。需要参数的脚本先查看 `--help`，再确认输入数据、输出目录和对应的研究或验收文档。脚本生成的 `outputs/`、`reports/` 内容与源码分开管理。表中“离线”表示不请求外部服务，仍可能创建本地报告；“公开采集”会访问外部行情接口。

本索引按**用途**定位脚本，不表示研究结果已获策略准入。当前工作区还有尚未加入 Git 的脚本；发布或在其他机器运行前，需核对脚本及其依赖是否已进入版本控制。

## 常用入口

| 脚本 | 用途 |
| --- | --- |
| [`check_environment.py`](check_environment.py) | 检查 Python、依赖和运行环境；CI 也使用此入口。 |
| [`verify_lock.py`](verify_lock.py) | 校验运行依赖锁文件。 |
| [`fetch_binance_data.py`](fetch_binance_data.py) | 下载并增量缓存 Binance 历史行情及数据 manifest。 |
| [`run_backtest_matrix.py`](run_backtest_matrix.py) | 运行多标的、多周期、多时间窗的批量回测。 |
| [`run_portable_tests.py`](run_portable_tests.py) | 在受限 Windows 临时目录中运行 pytest。 |
| [`train_selector.py`](train_selector.py) | CPU 机器学习选币的准备、训练、历史评价和影子观察；依赖登记数据包及可选 ML 依赖，见[训练指南](../docs/ml_selection.md)。 |
| [`verify_ml_selector_baseline.py`](verify_ml_selector_baseline.py) | 使用原登记数据独立复现旧 smart 基线，核对每日权益、执行与资金摘要；数据包前提见[V1 总结](../docs/ml_selection_history.md)。 |
| [`start_dashboard.ps1`](start_dashboard.ps1) | 启动本地网页研究工作台；入口和任务行为见 [Dashboard 使用说明](../dashboard/README.md)。 |

回测主入口是仓库根目录的 [`main.py`](../main.py)，实盘主入口是 [`run_live.py`](../run_live.py)；二者不在 `scripts/` 中。

## 调度与数据运维

| 脚本 | 用途 |
| --- | --- |
| [`run_automation.py`](run_automation.py) | 执行受监督的周期性回测、查看状态和清理自有运行产物；`prune` 默认只预览。 |
| [`run_research_automation.py`](run_research_automation.py) | 运行月度、季度离线研究诊断。 |
| [`register_automation_tasks.ps1`](register_automation_tasks.ps1) | 注册 Windows 计划任务；执行前核对目标环境与任务配置。 |
| [`repair_binance_point_in_time_data.py`](repair_binance_point_in_time_data.py) | 修复已知上市边界数据并构建固定时点的标的池。 |
| [`run_offline_recovery_drill.py`](run_offline_recovery_drill.py) | 用合成沙盒事实检查 SQLite 停机恢复。 |

## 工程验证与证据

| 脚本 | 用途 |
| --- | --- |
| [`check_repository_hygiene.py`](check_repository_hygiene.py) | 检查已跟踪临时文件，并按[文件保留清单](../docs/file_retention.md)核验冻结的历史证据。 |
| [`main_acceptance.py`](main_acceptance.py)、[`verify_baseline_archive.py`](verify_baseline_archive.py) | 主分支离线验收与历史基线归档校验。 |
| [`roadmap_baseline.py`](roadmap_baseline.py)、[`roadmap_evidence.py`](roadmap_evidence.py)、[`roadmap_priority.py`](roadmap_priority.py) | 冻结工作树基线、绑定历史证据、核对任务优先级和依赖。 |
| [`verify_roadmap_completion.py`](verify_roadmap_completion.py)、[`verify_roadmap_policy.py`](verify_roadmap_policy.py) | 检查完成定义与 Roadmap 政策规则；工程证据不等于交易准入。 |
| [`evaluate_matrix.py`](evaluate_matrix.py) | 对固定矩阵产物给出只读工程判定。 |
| [`run_phase3_capacity.py`](run_phase3_capacity.py)、[`run_phase4_analysis.py`](run_phase4_analysis.py)、[`run_phase5_analysis.py`](run_phase5_analysis.py) | 重建各阶段容量、路由与研究治理证据。 |
| [`validate_signal_observation.py`](validate_signal_observation.py)、[`validate_signal_meta_layer.py`](validate_signal_meta_layer.py)、[`validate_signal_adaptive.py`](validate_signal_adaptive.py) | 离线验证 P0 信号观察、P1 条件 EV、P2/P3 自适应研究链路。 |

## 研究与性能实验

这些脚本绑定特定样本、研究协议或输出结构；复用前应阅读对应文档和固定输入条件。

| 脚本 | 用途 |
| --- | --- |
| [`benchmark_backtest.py`](benchmark_backtest.py)、[`benchmark_kline_modes.py`](benchmark_kline_modes.py) | 离线引擎性能与 K 线模式成对基准。 |
| [`diagnose_strategy_health.py`](diagnose_strategy_health.py)、[`run_health_recovery_experiment.py`](run_health_recovery_experiment.py) | 策略健康诊断与固定候选对照。 |
| [`prepare_revalidation_data.py`](prepare_revalidation_data.py)、[`run_revalidation60.py`](run_revalidation60.py) | 冻结并复验预先声明的 60 标的样本。 |
| [`run_p0_attribution_experiment.py`](run_p0_attribution_experiment.py)、[`run_p0_recovery_backtest.py`](run_p0_recovery_backtest.py)、[`run_p0_recovery_validation.py`](run_p0_recovery_validation.py) | P0 归因及恢复政策的固定实验。 |
| [`run_deep_recovery_backtest.py`](run_deep_recovery_backtest.py)、[`run_expanded_universe_backtest.py`](run_expanded_universe_backtest.py)、[`run_strategy_remediation.py`](run_strategy_remediation.py) | 预声明恢复、扩展标的池和策略修复对照。 |
| [`run_strategy_branch_example.py`](run_strategy_branch_example.py) | 通过离线 Broker 演示 S2/S3 分支。 |
| [`collect_trend_portfolio_v3_data.py`](collect_trend_portfolio_v3_data.py)、[`run_trend_portfolio_v2.py`](run_trend_portfolio_v2.py)、[`run_trend_portfolio_v3.py`](run_trend_portfolio_v3.py) | 收集 V3 官方历史资料并运行 V2/V3 固定比较。 |
| [`render_capital_growth.py`](render_capital_growth.py) | 从已完成的资本对照结果渲染图表。 |
| [`benchmark_live_latency.py`](benchmark_live_latency.py) | 在明确的合成 I/O 延迟下成对比较旧版/当前实盘适配器；输入基线源码路径，输出本地基准报告，不测真实交易所延迟。 |

## 论文方法与收益复核（离线）

从[论文应用路线图](../docs/research/paper_applications.md)了解研究范围，从[统计验证契约](../docs/research/paper_validation_contract_20261003.md)核对方法输入。以下脚本复用既有历史资料，运行前登记本次候选及输入身份；本次登记不使已查看的历史变成独立留出集，也不改变生产准入。

| 脚本 | 输入与产物 |
| --- | --- |
| [`run_strategy_p0_p1.py`](run_strategy_p0_p1.py) | 读取策略审查批次的 `frozen_inputs`，执行固定窗口和对照组；参数可限定 `--windows`、`--arms`，结果写入独立研究目录。 |
| [`run_paper_applications.py`](run_paper_applications.py) | 读取 BTC/ETH 日线 manifest，冻结输入并运行既定引擎对照；生成登记、逐任务结果及 P0 信号观察资料。 |
| [`run_paper_roadmap.py`](run_paper_roadmap.py) | 读取日线 manifest 与 P0 观察，执行论文方法工作包并保留尝试日志；可附加细周期 manifest 和执行观测输入。 |
| [`run_return_followup.py`](run_return_followup.py) | 使用脚本内固定的日线 manifest 和前序论文对比结果，研究现货目标权重、年度选择和连续账户；`--output` 必填，`--workers` 限定 1–4。 |
| [`run_return_followup_engine.py`](run_return_followup_engine.py) | 用现有 `BacktestEngine` 复核固定策略分支及旧对照，输入 manifest 和前序论文批次；`--register-only` 只登记，执行并发限定 1 或 2。 |
| [`plot_return_followup.py`](plot_return_followup.py) | 从 `--report` 指定的已完成连续账户结果导出图表，不重新选参或重跑账户。 |
| [`review_barrier_first_touch.py`](review_barrier_first_touch.py) | 用细周期 manifest 复核历史双障碍歧义，保存新标签、变化表和输入身份。 |
| [`review_barrier_cascade.py`](review_barrier_cascade.py) | 读取上一轮复核目录，仅细化仍有歧义的标签，保留已验证路径并写入新目录。 |

两类收益复核使用不同执行路径：`run_return_followup.py` 的研究账户由 `analysis/paper_portfolio.py` 驱动 Broker；`run_return_followup_engine.py` 使用完整回测引擎。它们的结果应按各自协议解释。新导入细周期数据保留本次真实可用时间，不能倒填成历史决策时已知。

## 公开资料、执行证据与后续观察

| 脚本 | 外部访问与输入输出边界 |
| --- | --- |
| [`fetch_spot_intraday.py`](fetch_spot_intraday.py) | 公开采集 Binance 现货 1h 历史；保存原始分页、哈希、CSV 和会话 manifest，`--resume` 续传。历史事件时间不替代本次导入可用时间。 |
| [`fetch_spot_refinement.py`](fetch_spot_refinement.py) | 根据 `--labels` 中歧义候选涉及的 UTC 日期，定向采集现货 1m 历史；保存回顾性路径证据，支持续传。 |
| [`collect_factor_evidence.py`](collect_factor_evidence.py) | 有界采集公开日度因子输入，保留来源与接收时间；可续传，不认证历史时点可得性。 |
| [`diagnose_public_market.py`](diagnose_public_market.py) | 有界诊断选定官方主机的公开连接，输出脱敏结果；`--configuration-only` 仅检查本地配置。 |
| [`measure_market_speed.py`](measure_market_speed.py) | 请求公开 OHLCV，分别报告刷新耗时和已收盘价格变化；可附加本地 CSV，历史统计单列。 |
| [`collect_execution_quotes.py`](collect_execution_quotes.py) | 有界采集公开最优买卖报价（BBO），保存报价库和会话证据；`--resume-store` 追加已有库，`--output` 仍须为新目录。 |
| [`collect_execution_depth.py`](collect_execution_depth.py) | 有界采集 Binance 现货快照与有序深度流，保存深度日志、报价库、时钟诊断和可见深度测算；不提交订单。 |
| [`calibrate_execution.py`](calibrate_execution.py) | 离线读取事件 JSON，可关联请求、报价和订单账本；按决策/提交时点向后匹配独立报价，输出校准报告。`--output` 是新文件路径。 |
| [`audit_execution_readiness.py`](audit_execution_readiness.py) | 离线审计订单、请求、报价和深度证据；在新 `--output` 目录输出 `readiness.json`、`summary.json`、每日覆盖及分层 CSV。 |
| [`measure_sandbox_orders.py`](measure_sandbox_orders.py) | 沙盒订单探针；显式传入 `--execute-sandbox-orders` 并通过沙盒凭据门槛后会提交测试网订单、撤单和观察成交。结果不能代替真实市场执行证据。 |
| [`start_paper_observation.py`](start_paper_observation.py) | 登记并封存有限时长的公开报价观察。首次用 `--output`，后续用 `--resume` 验证原协议和证据后追加会话；每次必填 `--duration-seconds`。 |
| [`prepare_strategy_forward.py`](prepare_strategy_forward.py) | 在新目录冻结当前候选和后续公开采集协议，重新起算观察时间；不打开前序留出集。 |
| [`collect_strategy_forward.py`](collect_strategy_forward.py) | 从冻结候选的源码副本运行，以 `--batch` 指定登记目录；校验冻结身份后只收集登记后的已收盘 bar，不执行策略或计算选择指标。 |

公开报价、深度、网络耗时、订单确认和真实成交是不同证据。没有成交时，报价采集会明确保留零成交和校准不足状态；可见深度测算不等于已验证成交容量。公开采集中的 `live` 指数据环境，不表示真实订单获准提交。具体证据口径见[研究与分析模块说明](../docs/modules/analysis_dashboard_research_config.md)。

需要新目录的研究脚本会拒绝覆盖既有证据。部分脚本的默认路径绑定历史批次，复用时应显式指定新的 `--output`；续传仅使用该脚本公开提供的 `--resume` 或 `--resume-store`，不手工重写登记和哈希。

## 历史专项：策略审查交付

以下脚本服务于已登记的策略审查批次，包含固定输入、独立任务、汇总、归档和交付。它们不是通用回测或实盘入口；应从相应的批次文档和证据目录复现。

| 环节 | 脚本 |
| --- | --- |
| 公共资料与校验 | [`collect_strategy_review_lifecycle.py`](collect_strategy_review_lifecycle.py)、[`collect_strategy_review_public.py`](collect_strategy_review_public.py)、[`verify_strategy_review_binance_archives.py`](verify_strategy_review_binance_archives.py) |
| 登记与执行 | [`run_strategy_review.py`](run_strategy_review.py)、[`strategy_review_worker.py`](strategy_review_worker.py)、[`complete_strategy_review.py`](complete_strategy_review.py) |
| 扩展对照 | [`run_strategy_review_cross_market.py`](run_strategy_review_cross_market.py)、[`run_strategy_review_meta.py`](run_strategy_review_meta.py)、[`replay_strategy_review_controls.py`](replay_strategy_review_controls.py) |
| 成本与汇总 | [`strategy_review_cost_evidence.py`](strategy_review_cost_evidence.py)、[`strategy_review_report_helpers.py`](strategy_review_report_helpers.py)、[`summarize_strategy_review_families.py`](summarize_strategy_review_families.py)、[`summarize_strategy_review_meta.py`](summarize_strategy_review_meta.py)、[`plot_strategy_review.py`](plot_strategy_review.py) |
| 冻结与后继 | [`freeze_strategy_review.py`](freeze_strategy_review.py)、[`register_strategy_successor.py`](register_strategy_successor.py) |
| 打包与交付 | [`package_strategy_review.py`](package_strategy_review.py)、[`publish_strategy_review.py`](publish_strategy_review.py)、[`finalize_strategy_review_delivery.py`](finalize_strategy_review_delivery.py)、[`recover_strategy_review_delivery.py`](recover_strategy_review_delivery.py) |
