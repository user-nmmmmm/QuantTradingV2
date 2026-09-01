"""Text report writer mixin."""

import os
from typing import Any, Dict, List, Optional


class ReportWritersMixin:
    output_dir: str
    METRIC_NAMES: Dict[str, str]
    _format_primary_metrics: Any
    _format_metric_value: Any

    def _save_report_text(
        self,
        metrics: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        analysis: Optional[Dict[str, Any]] = None,
    ):
        """
        将指标与配置写入 report.txt。

        控制台只打印 ``PRIMARY_METRIC_KEYS``；report.txt 是唯一的完整指标出口，
        按「核心摘要 → 完整指标明细 → 回撤事件 → 交易质量 → 归因分析 → 基准对比 →
        文件说明」分节写出，``analysis`` 里没有的分节会被跳过而不是留空标题。
        """
        analysis = analysis or {}

        path = os.path.join(self.output_dir, "report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Backtest Results (回测结果)\n")
            f.write("==========================\n\n")

            if metadata:
                f.write("Configuration (配置信息):\n")
                for k, v in metadata.items():
                    f.write(f"{k}: {v}\n")
                f.write("\n")

            f.write("Core Metrics (核心指标摘要):\n")
            f.write("-----------------\n")
            f.write(self._format_primary_metrics(metrics))
            f.write("\n\n")

            self._write_lifecycle_section(
                f,
                metrics.get("BacktestLifecycle"),
                metrics.get("FullCapitalPeriodMetrics"),
                metrics.get("ActiveStrategyPeriodMetrics"),
            )
            self._write_strategy_health_section(f, metrics.get("StrategyHealth"))
            self._write_protective_stop_section(f, metrics.get("ProtectiveStops"))

            f.write("Full Metrics (完整指标明细):\n")
            f.write("-----------------\n")
            for k, v in metrics.items():
                # Diagnostics is a deep nested structure with its own rendered
                # section below; dumping it here would bury the scalar metrics.
                if k == "Diagnostics":
                    continue
                display_key = self.METRIC_NAMES.get(k, k)
                f.write(f"{display_key:<45}: {self._format_metric_value(v)}\n")
            f.write("\n")

            self._write_drawdown_events_section(f, analysis.get("drawdown_events"))
            self._write_trade_quality_section(f, analysis.get("trade_quality"))
            self._write_attribution_section(f, analysis.get("attribution"))
            self._write_control_attribution(f, analysis.get("attribution"))
            self._write_benchmark_section(f, analysis.get("benchmark_comparison"))
            self._write_diagnostics_section(f, metrics.get("Diagnostics"))

            f.write("File Descriptions (文件说明):\n")
            f.write("===========================\n")

            f.write("1. report.txt (回测报告概要)\n")
            f.write("   - Contains summary metrics and configuration parameters.\n")
            f.write("   - 包含核心指标汇总与回测参数配置。\n\n")

            f.write("2. equity.csv (净值曲线数据)\n")
            f.write("   - timestamp: Date (日期)\n")
            f.write("   - equity: Total Account Equity (总权益 = 现金 + 持仓市值)\n")
            f.write("   - cash: Available Cash (可用现金)\n\n")

            f.write("3. trades.csv (交易明细记录 - Execution Log)\n")
            f.write("   - signal_time: Time signal was generated (信号产生时间)\n")
            f.write("   - fill_time: Time order was filled (成交时间)\n")
            f.write("   - symbol: Trading Pair (交易标的)\n")
            f.write("   - side: buy/sell/short/cover (交易方向)\n")
            f.write("   - qty: Executed Quantity (成交数量)\n")
            f.write("   - fill_price: Executed Price (成交价格)\n")
            f.write("   - commission: Transaction Fee (手续费)\n")
            f.write("   - slip: Slippage Value (滑点金额)\n")
            f.write("   - slip_dir: Slippage Direction (滑点方向)\n")
            f.write("   - strategy_id: Strategy Name (策略名称)\n")
            f.write(
                "   - exit_reason: Reason for order (成交原因: signal/stop/takeprofit)\n"
            )

            f.write("\n4. stop_order_audit.csv (保护性止损单审计 - STR-P1-01)\n")
            f.write(
                "   - Every resident protective-stop intent and fill: place /\n"
                "     replace (ratchet) / cancel / fill, with the level in\n"
                "     force and the price it actually filled at.\n"
            )
            f.write(
                "   - 常驻止损单的每一次挂单、上移、取消与成交；note 列记录\n"
                "     bar 内价格路径假设。\n"
            )

    def _write_lifecycle_section(
        self,
        f,
        lifecycle: Optional[Dict[str, Any]],
        full_metrics: Optional[Dict[str, Any]],
        active_metrics: Optional[Dict[str, Any]],
    ) -> None:
        lifecycle = lifecycle or {}
        full_metrics = full_metrics or {}
        active_metrics = active_metrics or {}
        f.write("Backtest Lifecycle (回测生命周期):\n")
        f.write("-----------------\n")
        for key in (
            "status", "active_start", "active_end", "termination_timestamp",
            "termination_reason", "inactive_bars", "resume_count",
            "inactive_days",
            "breaker_epochs", "suppressed_setups_after_termination",
        ):
            f.write(f"{key}: {lifecycle.get(key)}\n")
        f.write("\nFull Capital-Period Metrics (完整资金期):\n")
        f.write(self._format_primary_metrics(full_metrics))
        f.write("\n\nActive Strategy-Period Metrics (策略活跃期):\n")
        f.write(self._format_primary_metrics(active_metrics))
        f.write("\n\n")

    @staticmethod
    def _write_strategy_health_section(
        f, health: Optional[Dict[str, Dict[str, Any]]],
    ) -> None:
        """SR1-4: the health lifecycle of every strategy, next to the run status.

        Without this section a run can end as ``completed`` while a strategy
        has been in COOLDOWN or MANUAL_LOCK for years - exactly the 2021-2026
        silence this roadmap exists to make impossible to miss.
        """
        if not health:
            return
        f.write("Strategy Health Lifecycle (策略健康生命周期):\n")
        f.write("-----------------\n")
        for name, entry in sorted(health.items()):
            f.write(f"{name}:\n")
            for key in (
                "status", "status_changed_at", "cooldown_started_at",
                "cooldown_until", "trigger_reason", "trigger_event_id",
                "consecutive_negative_cohorts", "rolling_cohort_r",
                "probation_closed_cohorts", "probation_total_r",
                "probation_risk_multiplier", "failed_probation_cycles",
                "manual_lock_reason", "risk_multiplier", "resume_count",
                "total_cohorts", "counted_cohorts", "allows_new_entries",
                "raw_setup_count", "suppressed_raw_setups", "last_raw_setup_at",
            ):
                f.write(f"  {key}: {entry.get(key)}\n")
        f.write("\n")

    @staticmethod
    def _write_protective_stop_section(
        f, summary: Optional[Dict[str, Any]],
    ) -> None:
        """STR-P1-01: how this run's stops were executed, stated up front.

        A backtest that discovers the breach at the close and exits at the next
        open prices its stops differently from a venue-resident stop, so the
        mode and the intrabar path assumption belong next to the results rather
        than only in the code that produced them.
        """
        if not summary:
            return
        f.write("Protective Stop Execution (保护性止损执行口径):\n")
        f.write("-----------------\n")
        enabled = bool(summary.get("backtest_resident_enabled"))
        f.write(
            "mode: {}\n".format(
                "venue_resident_intrabar" if enabled
                else "legacy_close_detect_next_open_exit"
            )
        )
        f.write(f"intrabar_path: {summary.get('intrabar_path')}\n")
        f.write(f"triggered_stops: {summary.get('triggered_stops')}\n")
        f.write(
            f"unprotected_position_bars: "
            f"{summary.get('unprotected_position_bars')}\n"
        )
        if not enabled:
            f.write(
                "WARNING: stops were not venue-equivalent in this run; the "
                "exit price and timing are optimistic relative to live "
                "(STR-P1-01).\n"
            )
        f.write("\n")

    @staticmethod
    def _write_drawdown_events_section(f, events: Optional[List[Dict[str, Any]]]) -> None:
        """枚举每一段独立的峰→谷→恢复回撤（而非只报最差一次），按深度从大到小排序。

        底层 ``ExtendedAnalytics["drawdown_events"]`` 不做深度过滤（供其他消费者/
        回归基线使用完整数据）；这里只在渲染 report.txt 文本时过滤掉深度低于 1%%
        的噪声事件，不影响返回给调用方的 metrics 字典。
        """
        f.write("Drawdown Events (回撤事件明细):\n")
        f.write("-----------------\n")
        filtered = [
            e for e in (events or [])
            if e.get("depth_pct") is not None and abs(e["depth_pct"]) >= 0.01
        ]
        if not filtered:
            f.write("No drawdown events at or above the 1%% depth filter.\n")
            f.write("（未检测到深度超过 1%% 的回撤事件）\n\n")
            return
        ranked = sorted(
            filtered, key=lambda e: e["depth_pct"] if e["depth_pct"] is not None else 0.0
        )
        shown_limit = 20
        for i, event in enumerate(ranked[:shown_limit], start=1):
            status = "OPEN (尚未恢复)" if event["is_open"] else "RECOVERED (已恢复)"
            f.write(
                f"#{i} depth={event['depth_pct']:.2%} peak={event['peak']} "
                f"trough={event['trough']} recovery={event['recovery']} "
                f"duration_days={event['duration_days']} status={status}\n"
            )
        if len(ranked) > shown_limit:
            f.write(f"... 仅显示按深度排序的前 {shown_limit} 条，共 {len(ranked)} 条\n")
        f.write("\n")

    def _write_trade_quality_section(
        self, f, quality: Optional[Dict[str, Any]]
    ) -> None:
        """胜率/期望值/持仓时长，以及按策略、按标的的细分（core.metrics.calculate_trade_quality）。"""
        f.write("Trade Quality (交易质量与持仓时长):\n")
        f.write("-----------------\n")
        if not quality or not quality.get("sample_size"):
            f.write("No closed trades to analyze (无已闭合交易可供分析)\n\n")
            return
        if quality["status"] == "insufficient":
            f.write(
                "(样本数低于 30 笔的稳健性阈值，以下为原始统计，非稳健置信区间估计)\n"
            )
        duration = quality.get("holding_duration_hours") or {}
        f.write(f"Sample Size (样本数)              : {quality['sample_size']}\n")
        f.write(f"Win Rate (胜率)                   : {self._format_metric_value(quality['win_rate'])}\n")
        f.write(f"Expectancy (期望值)               : {self._format_metric_value(quality['expectancy'])}\n")
        f.write(
            f"Profit Factor (盈亏比)            : "
            f"{self._format_metric_value(quality['profit_factor'])} "
            f"[{quality['profit_factor_status']}]\n"
        )
        if duration.get("status") == "ok":
            f.write(
                f"Holding Duration hrs (持仓时长/小时): mean={duration['mean']:.2f} "
                f"median={duration['median']:.2f} min={duration['min']:.2f} max={duration['max']:.2f}\n"
            )
        f.write("\nBy Symbol (按标的):\n")
        for symbol, stats in (quality.get("by_symbol") or {}).items():
            f.write(
                f"  {symbol:<15} trades={stats['sample_size']:<6} "
                f"win_rate={stats['win_rate']:.2%} net_pnl={stats['net_pnl']:.4f} "
                f"profit_factor={self._format_metric_value(stats['profit_factor'])}\n"
            )
        f.write("\nBy Strategy (按策略):\n")
        for strategy, stats in (quality.get("by_strategy") or {}).items():
            f.write(
                f"  {strategy:<15} trades={stats['sample_size']:<6} "
                f"win_rate={stats['win_rate']:.2%} net_pnl={stats['net_pnl']:.4f} "
                f"profit_factor={self._format_metric_value(stats['profit_factor'])}\n"
            )
        f.write("\n")

    @staticmethod
    def _write_control_attribution(f, attribution: Optional[Dict[str, Any]]) -> None:
        """SR3-3: separate the alpha's own PnL from the risk overlay's.

        A profit factor computed over trades that a portfolio breaker closed
        says something about the breaker, not about the entry signal.
        """
        control = (attribution or {}).get("control_attribution")
        if not control:
            return
        counts = (attribution or {}).get("trade_count_by_exit_controller") or {}
        f.write("Control Attribution (退出控制器归因):\n")
        share = control.get("risk_overlay_share")
        share_text = f"{share:.1%}" if share is not None else "N/A"
        for key, label in (
            ("alpha_only", "Alpha 自有退出"),
            ("risk_overlay", "AccountRisk 组合熔断"),
            ("router_and_system", "Router/系统退出"),
            ("combined", "合计"),
        ):
            f.write(f"  {key:<20} ({label}): {control.get(key, 0.0):>14.2f}\n")
        f.write(f"  risk_overlay_share (熔断贡献占比): {share_text}\n")
        f.write(f"  trade counts (笔数): {counts}\n")
        if not control.get("reconciles", True):
            f.write("  [P0] control attribution does not sum to total net PnL\n")
        f.write("\n")

    @staticmethod
    def _write_attribution_section(f, attribution: Optional[Dict[str, Any]]) -> None:
        """按策略/标的/月份拆分净盈亏贡献（core.metrics.calculate_attribution）。"""
        f.write("Attribution (归因分析 — 按策略/标的/月份):\n")
        f.write("-----------------\n")
        if not attribution or not attribution.get("sample_size"):
            f.write("No closed trades to attribute (无已闭合交易可供归因)\n\n")
            return
        f.write(f"Total Net PnL (总净盈亏): {attribution['total_net_pnl']:.4f}\n\n")
        for label, key in (("By Strategy (按策略)", "by_strategy"),
                           ("By Symbol (按标的)", "by_symbol"),
                           ("By Month (按月份)", "by_month")):
            f.write(f"{label}:\n")
            for name, pnl in sorted((attribution.get(key) or {}).items()):
                f.write(f"  {name:<15}: {pnl:.4f}\n")
            f.write("\n")

    @staticmethod
    def _write_benchmark_section(f, comparison: Optional[Dict[str, Any]]) -> None:
        """策略 vs. 基准（等权买入并持有）总收益对比（core.metrics.calculate_benchmark_comparison）。"""
        if comparison is None:
            return
        f.write("Benchmark Comparison (对基准的超额收益):\n")
        f.write("-----------------\n")
        if comparison.get("status") != "ok":
            f.write("Insufficient overlapping history with benchmark (与基准重叠历史不足)\n\n")
            return
        f.write(f"Strategy Return (策略总收益)   : {comparison['strategy_return']:.4%}\n")
        f.write(f"Benchmark Return (基准总收益)   : {comparison['benchmark_return']:.4%}\n")
        f.write(f"Excess Return (超额收益)        : {comparison['excess_return']:.4%}\n")
        correlation = comparison.get("correlation")
        f.write(
            f"Return Correlation (收益相关性) : "
            f"{'%.4f' % correlation if correlation is not None else 'N/A'}\n\n"
        )

    @staticmethod
    def _write_diagnostics_section(f, diagnostics: Optional[Dict[str, Any]]) -> None:
        """结果可信度诊断（core.diagnostics）。

        与上面的绩效分节不同，本节回答的是「这个业绩数字能不能信」以及
        「系统的实际行为是否和代码描述一致」，因此即使收益为正也可能给出警告。
        """
        if not diagnostics:
            return
        f.write("Result Diagnostics (结果可信度诊断):\n")
        f.write("-----------------\n")

        concentration = diagnostics.get("pnl_concentration") or {}
        if concentration.get("status") == "ok":
            f.write("PnL Concentration (盈亏集中度):\n")
            for count, entry in sorted(
                concentration.get("top_n", {}).items(), key=lambda kv: int(kv[0])
            ):
                share = entry.get("share_of_total")
                share_text = f"{share:.1%}" if share is not None else "N/A"
                f.write(
                    f"  Top {int(count):>2} trades (最赚钱{int(count)}笔): "
                    f"{entry['contribution']:>12.2f}  = {share_text:>7} of net profit  "
                    f"| excluding them (剔除后): {entry['total_excluding']:>12.2f}\n"
                )
            hhi = concentration.get("profit_hhi")
            if hhi is not None:
                f.write(f"  Profit HHI (利润赫芬达尔指数): {hhi:.4f}  "
                        f"(1.0=单笔贡献全部利润 / 越低越分散)\n")
            top10 = concentration.get("top_n", {}).get("10", {})
            if top10.get("share_of_total") is not None and top10["share_of_total"] >= 1.0:
                f.write("  [WARNING] 剔除最赚钱的10笔后系统净亏损——收益依赖极少数交易，"
                        "不构成稳定 edge。\n")
            f.write("\n")

        exits = diagnostics.get("exit_attribution") or {}
        if exits.get("status") == "ok":
            f.write("Exit Attribution (出场归因 — 谁真正平掉了仓位):\n")
            ratio = exits.get("own_exit_ratio")
            if ratio is not None:
                f.write(f"  Own-exit ratio (策略自身出场占比): {ratio:.1%}\n")
            for reason, count in sorted(
                exits.get("by_reason", {}).items(), key=lambda kv: -kv[1]
            ):
                f.write(f"    {reason:<45} {count}\n")
            for name, entry in sorted(exits.get("by_strategy", {}).items()):
                own_ratio = entry.get("own_exit_ratio")
                own_text = f"{own_ratio:.1%}" if own_ratio is not None else "N/A"
                f.write(
                    f"  {name:<22} closed={entry['closed_trades']:<4} "
                    f"own={entry['own_exits']:<4} external={entry['external_exits']:<4} "
                    f"own_ratio={own_text}\n"
                )
            for name in exits.get("inert_exit_logic", []):
                f.write(f"  [WARNING] {name} 的出场规则几乎从未触发——其出场参数实际无效，"
                        f"仓位由外部（如 Router regime 切换）平掉。\n")
            f.write("\n")

        joint = diagnostics.get("joint_entry_exit_attribution") or {}
        if joint.get("sample_size"):
            f.write("Entry Strategy x Exit Controller (联合归因):\n")
            for entry, row in sorted((joint.get("matrix") or {}).items()):
                for controller, cell in sorted(row.items()):
                    f.write(
                        f"  {entry:<22} x {controller:<22} "
                        f"trades={cell['trades']:<4} net_pnl={cell['net_pnl']:.2f}\n"
                    )
            f.write("\n")

        holding = diagnostics.get("holding_period_audit") or {}
        if holding.get("sample_size"):
            f.write("Holding-period Tail Audit (持仓长尾):\n")
            f.write(
                f"  median={holding['median_holding_days']:.2f}d "
                f"p95={holding['p95_holding_days']:.2f}d "
                f"max={holding['max_holding_days']:.2f}d "
                f"timeouts={len(holding.get('timeouts') or [])}\n\n"
            )

        coverage = diagnostics.get("lifecycle_coverage") or {}
        if coverage.get("status") == "ok":
            f.write("Lifecycle Coverage (闭合事件覆盖率):\n")
            overall = coverage.get("overall_coverage")
            if overall is not None:
                f.write(f"  Overall (总体): {overall:.1%}\n")
            for name in coverage.get("blind_strategies", []):
                entry = coverage["by_strategy"][name]
                f.write(
                    f"  [WARNING] {name} 只观测到 {entry['observed_closures']}/"
                    f"{entry['expected_closures']} 次平仓——依赖平仓回调的风控"
                    f"（熄火/冷却）处于失效状态。\n"
                )
            f.write("\n")

        activity = diagnostics.get("strategy_activity_consistency") or {}
        if activity.get("status") == "ok":
            f.write("Strategy Activity Consistency (交易活跃度一致性):\n")
            f.write(
                f"  Longest no-trade gap (最长零交易间隔): "
                f"{activity.get('longest_no_trade_days')}d "
                f"({activity.get('longest_no_trade_start')} -> "
                f"{activity.get('longest_no_trade_end')})\n"
            )
            f.write(
                f"  Suppressed raw setups (被健康闸门抑制的信号): "
                f"{activity.get('suppressed_raw_setups')}\n"
            )
            f.write(
                f"  Strategy health (策略健康状态): "
                f"{activity.get('strategy_health_status')}\n"
            )
            for finding in activity.get("findings") or []:
                f.write(f"  [P0] {finding}\n")
            f.write("\n")

        calendar = diagnostics.get("calendar_returns") or {}
        if calendar.get("status") == "ok":
            f.write("Calendar Returns (逐年表现):\n")
            for entry in calendar.get("periods", []):
                trades = entry.get("trades")
                trades_text = f"trades={trades:<4}" if trades is not None else ""
                f.write(
                    f"  {entry['period']:<8} return={entry['return']:>9.2%}  "
                    f"{trades_text} end_equity={entry['end_equity']:.2f}\n"
                )
            negative = calendar.get("negative_periods")
            total = calendar.get("sample_size")
            if negative is not None:
                f.write(f"  Negative periods (亏损期数): {negative}/{total}\n")
            f.write("\n")

        streaks = diagnostics.get("streaks") or {}
        if streaks.get("status") == "ok":
            f.write("Streaks (连续盈亏):\n")
            f.write(
                f"  Max win streak (最长连盈):  {streaks['max_win_streak']:>3} "
                f"({streaks['max_win_streak_pnl']:.2f})\n"
            )
            f.write(
                f"  Max loss streak (最长连亏): {streaks['max_loss_streak']:>3} "
                f"({streaks['max_loss_streak_pnl']:.2f})\n\n"
            )
