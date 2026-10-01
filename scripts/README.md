# 脚本索引

从仓库根目录运行 `python scripts/<脚本名>.py`。需要参数的脚本先查看 `--help`，再确认输入数据、输出目录和对应的研究或验收文档。脚本生成的 `outputs/`、`reports/` 内容与源码分开管理。

本索引按**用途**定位脚本，不表示研究结果已获策略准入。当前工作区还有尚未加入 Git 的脚本；发布或在其他机器运行前，需核对脚本及其依赖是否已进入版本控制。

## 常用入口

| 脚本 | 用途 |
| --- | --- |
| [`check_environment.py`](check_environment.py) | 检查 Python、依赖和运行环境；CI 也使用此入口。 |
| [`verify_lock.py`](verify_lock.py) | 校验运行依赖锁文件。 |
| [`fetch_binance_data.py`](fetch_binance_data.py) | 下载并增量缓存 Binance 历史行情及数据 manifest。 |
| [`run_backtest_matrix.py`](run_backtest_matrix.py) | 运行多标的、多周期、多时间窗的批量回测。 |
| [`run_portable_tests.py`](run_portable_tests.py) | 在受限 Windows 临时目录中运行 pytest。 |

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
