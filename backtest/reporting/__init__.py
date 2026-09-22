import math
import pandas as pd
import numpy as np
import matplotlib
# Report generation only writes PNG files; it never opens a window. Pin the
# non-interactive Agg backend before pyplot is imported so rendering cannot
# depend on a GUI toolkit being installed and working. Otherwise matplotlib
# probes for Tk/Qt at first figure creation, which fails on machines with a
# broken Tcl/Tk install and — because the probe happens lazily — turns chart
# output into an intermittent failure.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import os
from collections import deque
from typing import Deque, List, Dict, Any, Optional, Tuple

from backtest.reporting.risk_metrics import summarize_exposure
from backtest.reporting.serialization import write_metrics_json
from core.diagnostics import build_diagnostics
from core.logger import get_logger
from core.metric_result import MetricResult
from core.metrics import (
    calculate_attribution,
    calculate_benchmark_comparison,
    calculate_cost_sensitivity,
    calculate_drawdown_events,
    calculate_equity_metrics,
    calculate_profit_factor,
    calculate_r_multiple_stats,
    calculate_signal_funnel,
    calculate_trade_quality,
    infer_periods_per_year,
    monthly_returns,
)

logger = get_logger(__name__)

"""
回测报告（ReportGenerator）模块

输出内容（写入 output_dir）：
- equity.csv：权益曲线（timestamp,equity,cash）
- benchmark.csv：基准曲线（可选）
- trades.csv：成交明细（来自 Broker.trades）
- report.txt：指标摘要与文件说明
- equity.png：净值、回撤、日收益、资金占用四联图

指标计算：
- 权益曲线：CAGR、最大回撤、月均收益、夏普（按 252 日年化）
- 交易明细：先用 FIFO 重建成交腿，再按 position_id 折叠成往返交易（一次持仓
  从建仓到清零），胜率/盈亏比/期望值一律按**往返**统计；成交腿数另以
  ``ClosedTradeLegs`` 报出，两个口径都可见
"""


# 控制台只展示这些核心指标；完整明细（含回撤事件、交易质量、归因、基准对比）
# 一律写入 report.txt，不在控制台重复输出。
PRIMARY_METRIC_KEYS = [
    "TotalReturn",
    "CAGR",
    "MaxDrawdownPct",
    "SharpeRatio",
    "TotalTrades",
    "WinRate",
    "ProfitFactor",
    "NetPnL",
    "EndEquity",
]


def format_primary_metrics(metrics: Dict[str, Any], bilingual: bool = True) -> str:
    """把 ``metrics`` 中的核心指标子集格式化为文本块。

    ``bilingual=False``（用于终端 stdout）只保留英文标签：Windows 控制台的默认
    代码页通常是 GBK（非 UTF-8），直接打印中文标签会花屏；``report.txt`` 用
    ``encoding="utf-8"`` 显式写文件，不受控制台代码页影响，可以放心用双语标签。
    """
    lines = []
    for key in PRIMARY_METRIC_KEYS:
        if key not in metrics:
            continue
        label = METRIC_NAMES.get(key, key)
        if not bilingual:
            label = label.split(" (")[0]
        value = metrics[key]
        lines.append(f"{label:<24}: {_format_metric_value(value)}")
    return "\n".join(lines)


def _format_metric_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


# Metrics Translation Map（覆盖 core/metrics.py 与本模块产出的全部指标键，
# 保证 report.txt 里不出现未翻译的裸 camelCase 键名）。
METRIC_NAMES = {
    "CAGR": "CAGR (年化收益率)",
    "CAGRStatus": "CAGR Status (年化收益率状态)",
    "MaxDrawdownPct": "Max Drawdown % (最大回撤率)",
    "MaxDrawdownAmount": "Max Drawdown $ (最大回撤金额)",
    "CurrentDrawdownPct": "Current Drawdown % (当前回撤率)",
    "DrawdownStatus": "Drawdown Status (回撤计算状态)",
    "MaxDrawdownPeak": "Max Drawdown Peak Time (最大回撤峰值时间)",
    "MaxDrawdownTrough": "Max Drawdown Trough Time (最大回撤谷底时间)",
    "MaxDrawdownRecovery": "Max Drawdown Recovery Time (最大回撤恢复时间)",
    "MaxDrawdownDurationPeriods": "Max Drawdown Duration (periods) (最大回撤持续周期数)",
    "MaxDrawdownDurationDays": "Max Drawdown Duration (days) (最大回撤持续天数)",
    "MaxDrawdownRecoveryPeriods": "Max Drawdown Recovery (periods) (最大回撤恢复周期数)",
    "MaxDrawdownRecoveryDays": "Max Drawdown Recovery (days) (最大回撤恢复天数)",
    "MaxDrawdownOpen": "Max Drawdown Still Open (最大回撤是否尚未恢复)",
    "UnderwaterRatio": "Underwater Ratio (水下时间占比)",
    "AvgMonthlyReturn": "Avg Monthly Return (月均收益率)",
    "MonthlyReturnStatus": "Monthly Return Status (月收益计算状态)",
    "MonthlyReturnSamples": "Monthly Return Samples (月收益样本数)",
    "SharpeRatio": "Sharpe Ratio (夏普比率)",
    "SharpeStatus": "Sharpe Status (夏普比率计算状态)",
    "SharpeSamples": "Sharpe Samples (夏普比率样本数)",
    "PeriodsPerYear": "Periods Per Year (年化周期数)",
    "EndEquity": "End Equity (最终净值)",
    "TotalReturn": "Total Return (总收益率)",
    "MetricsFormulaVersion": "Metrics Formula Version (指标公式版本)",
    "TotalTrades": "Total Trades (往返交易次数)",
    "ClosedTradeLegs": "Closed Trade Legs (成交腿配对数)",
    "WinRate": "Win Rate (胜率)",
    "ProfitFactor": "Profit Factor (盈亏比)",
    "ProfitFactorStatus": "Profit Factor Status (盈亏比计算状态)",
    "ProfitFactorSamples": "Profit Factor Samples (盈亏比样本数)",
    "ProfitFactorLosses": "Profit Factor Loss Count (盈亏比亏损笔数)",
    "ProfitFactorLower95": "Profit Factor 95% CI Lower (盈亏比 95% 置信区间下限)",
    "ProfitFactorUpper95": "Profit Factor 95% CI Upper (盈亏比 95% 置信区间上限)",
    "Expectancy": "Expectancy (期望值)",
    "AvgWin": "Avg Win (平均盈利)",
    "AvgLoss": "Avg Loss (平均亏损)",
    "GrossPnL": "Gross PnL (毛利润)",
    "TotalCommission": "Total Commission (总手续费)",
    "TotalSlippage": "Total Slippage (总滑点成本)",
    "NetPnL": "Net PnL (净利润)",
}


from backtest.reporting.render.charts import ReportChartsMixin
from backtest.reporting.metrics import ReportMetricsMixin
from backtest.reporting.trades import TradeReconstructionMixin
from backtest.reporting.render.text import ReportWritersMixin


class ReportGenerator(
    ReportMetricsMixin,
    TradeReconstructionMixin,
    ReportWritersMixin,
    ReportChartsMixin,
):
    METRIC_NAMES = METRIC_NAMES
    _format_metric_value = staticmethod(_format_metric_value)
    _format_primary_metrics = staticmethod(format_primary_metrics)

    def __init__(self, output_dir: str):
        """
        参数：
        - output_dir：报告输出目录（不存在则创建）
        """
        self.output_dir = output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

    def generate(
        self,
        trades: List[Dict],
        equity_curve: pd.DataFrame,
        metadata: Optional[Dict[str, Any]] = None,
        benchmark_curve: pd.Series = None,
        metrics_only: bool = False,
        close_events: Optional[Dict[str, int]] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        strategy_health: Optional[Dict[str, Dict[str, Any]]] = None,
        protective_stops: Optional[Dict[str, Any]] = None,
        report_profile: str = "full",
        data_quality: Optional[Dict[str, Any]] = None,
        event_log: Optional[Any] = None,
        max_holding_days: Optional[int] = None,
        periods_per_year: Optional[float] = None,
        *,
        group_equity: Optional[pd.DataFrame] = None,
        group_cashflows: Optional[pd.DataFrame] = None,
        external_cashflows: Optional[pd.Series] = None,
        cashflow_timing: str = "end_of_interval",
        terminal_valuations: Optional[Any] = None,
        independent_quotes: Optional[Any] = None,
        execution_as_of: Optional[Any] = None,
        max_quote_age_seconds: float = 1.0,
    ):
        """
        生成报告并返回指标字典。

        参数：
        - trades：Broker 记录的成交列表（每条为 dict）
        - equity_curve：以 timestamp 为索引的权益曲线 DataFrame
        - metadata：回测配置元信息（写入 report.txt）
        - benchmark_curve：基准曲线（Series，可选）
        - metrics_only：为 True 时跳过 CSV/报告文本/图表落盘，只返回指标字典
          （用于网格搜索等只关心指标、丢弃产物的场景）
        - close_events：策略名 -> 该策略实际观测到的平仓次数（来自
          ``BacktestEngine.run`` 的 ``close_events``）。传入后诊断会计算
          ``lifecycle_coverage``，用于发现"平仓回调从未触发导致熄火/冷却风控
          失效"这类问题；不传则跳过该项（例如只有成交明细、没有引擎上下文时）。
        - group_equity/group_cashflows/external_cashflows：同一时钟、同一报告币种的
          全账户分组估值和显式资金流；仅支持区间末资金流，缺事实不估算回撤贡献。
        - terminal_valuations/independent_quotes：独立市场事实，字段契约见
          calculate_execution_quality；终值需 execution_as_of，报价不得晚于成交。
        """
        if report_profile not in {"workbook", "compact", "full"}:
            raise ValueError("report_profile must be 'workbook', 'compact' or 'full'")
        trades_df = pd.DataFrame(trades)

        if not metrics_only and report_profile != "workbook":
            # Workbook embeds raw data directly; avoid writing disposable CSVs.
            # 1. Save CSVs
            equity_curve.to_csv(os.path.join(self.output_dir, "equity.csv"))

            if benchmark_curve is not None:
                benchmark_curve.to_csv(os.path.join(self.output_dir, "benchmark.csv"))

            if not trades_df.empty:
                trades_df.to_csv(os.path.join(self.output_dir, "trades.csv"), index=False)

        # 2. Calculate Metrics
        # Legs are the raw FIFO open/close matches; round trips fold the legs
        # of one position back together. Everything below reports round trips
        # (what a trader calls "a trade") except lifecycle_coverage, whose
        # counterpart counter is leg-level. See backtest/reporting/trades.py.
        all_closed_legs = self._reconstruct_closed_trades(trades_df)
        invalid_trades = [row for row in all_closed_legs if row.get("status") == "invalid_input"]
        closed_legs = [row for row in all_closed_legs if row.get("status") != "invalid_input"]
        closed_trades = self._aggregate_round_trips(closed_legs)
        trade_metrics = self._trade_metrics_from_closed(closed_trades)
        equity_metrics = self._calculate_equity_metrics(equity_curve, periods_per_year=periods_per_year)

        metrics = {**equity_metrics, **trade_metrics}
        metrics["TradeInputIntegrity"] = {"status": "invalid_input" if invalid_trades else "ok",
                                         "invalid_records": len(invalid_trades),
                                         "reason": "legacy ownership/cost facts incomplete" if invalid_trades else None}
        if invalid_trades:
            for key in ("ProfitFactor", "ProfitFactorLower95", "ProfitFactorUpper95", "WinRate",
                        "Expectancy", "AvgWin", "AvgLoss", "GrossPnL", "TotalCommission", "TotalSlippage", "NetPnL"):
                metrics[key] = None
            metrics["ProfitFactorStatus"] = "invalid_input"
        # Both counting conventions stay visible: TotalTrades is round trips,
        # this is the fill-leg count behind them. A large gap means the
        # participation cap is splitting orders heavily.
        metrics["ClosedTradeLegs"] = len(closed_legs)
        lifecycle_provided = lifecycle is not None
        lifecycle = dict(lifecycle or {})
        active_end = lifecycle.get("active_end")
        active_curve = equity_curve
        if active_end is not None and not equity_curve.empty:
            active_curve = equity_curve.loc[:pd.Timestamp(active_end)]
        if lifecycle_provided:
            metrics["FullCapitalPeriodMetrics"] = dict(equity_metrics)
            metrics["ActiveStrategyPeriodMetrics"] = (
                self._calculate_equity_metrics(active_curve, periods_per_year=periods_per_year)
                if not active_curve.empty else {}
            )
            metrics["BacktestLifecycle"] = lifecycle

        extended = dict(metrics.get("ExtendedAnalytics") or {})
        extended["drawdown_events"] = calculate_drawdown_events(equity_curve["equity"])
        drawdowns = extended["drawdown_events"]
        extended["drawdown_summary"] = {
            "status": "ok" if drawdowns else "insufficient_data",
            "top_n": 5, "top_depth_events": sorted(drawdowns, key=lambda row: row["depth_pct"] or 0.0)[:5],
            "longest_underwater": max(drawdowns, key=lambda row: row["duration_periods"]) if drawdowns else None,
            "unrecovered_count": sum(row["is_open"] for row in drawdowns)}
        # BM3: the book behind the returns. Reports "not_recorded" for curves
        # that predate the engine's exposure columns rather than guessing.
        extended["exposure"] = summarize_exposure(equity_curve)
        from core.metrics.attribution import calculate_group_drawdown_contribution
        extended.setdefault("attribution", {})["group_drawdown_contribution"] = (
            calculate_group_drawdown_contribution(
                equity_curve["equity"], group_equity, group_cashflows, external_cashflows,
                cashflow_timing=cashflow_timing))
        from core.metrics.execution import calculate_execution_quality
        event_log = list(event_log) if event_log is not None else None
        extended["execution_quality"] = calculate_execution_quality(
            event_log, terminal_valuations=terminal_valuations, independent_quotes=independent_quotes,
            as_of=execution_as_of, max_quote_age_seconds=max_quote_age_seconds)
        elapsed_days = ((equity_curve.index[-1] - equity_curve.index[0]).total_seconds() / 86400
                        if len(equity_curve) > 1 else 0.0)
        average_equity = float(equity_curve["equity"].mean()) if not equity_curve.empty else 0.0
        turnover_notional = (sum(abs(float(row["qty"]) * float(row["fill_price"])) for row in trades)
                             if not invalid_trades else None)
        turnover_valid = bool(not invalid_trades and average_equity > 0 and elapsed_days > 0)
        extended["turnover"] = {"status": "invalid_input" if invalid_trades else "ok" if turnover_valid else "insufficient_data",
            "policy": "gross two-way executed notional / arithmetic mean observed equity; quote currency of report",
            "executed_notional": turnover_notional, "observed_days": elapsed_days,
            "ratio": turnover_notional / average_equity if turnover_valid else None,
            "annualized_ratio": turnover_notional / average_equity * 365.25 / elapsed_days if turnover_valid else None}
        if event_log is not None:
            # BM3: where the signal chain leaks. Only computable from the
            # engine's event pipeline, so it is absent (not zero) when a caller
            # has trades and a curve but no run context.
            extended["signal_funnel"] = calculate_signal_funnel(event_log)
        if benchmark_curve is not None:
            extended["benchmark_comparison"] = calculate_benchmark_comparison(
                equity_curve["equity"], benchmark_curve
            )
            if active_end is not None and not active_curve.empty:
                extended["active_benchmark_comparison"] = calculate_benchmark_comparison(
                    active_curve["equity"], benchmark_curve.loc[:pd.Timestamp(active_end)]
                )
        # Trustworthiness diagnostics: whether the headline numbers can be
        # believed and whether the system behaves as its code claims. Kept in
        # its own key so consumers can tell "performance" from "credibility".
        metrics["Diagnostics"] = build_diagnostics(
            closed_trades, equity_curve["equity"], close_events, strategy_health,
            closed_legs=closed_legs,
            max_holding_days=max_holding_days,
        )
        # SR1-4: the lifecycle section must state the health status alongside
        # the run status, so "completed" can never stand alone next to a
        # multi-year silence.
        if strategy_health:
            metrics["StrategyHealth"] = {
                name: dict(entry) for name, entry in strategy_health.items()
            }
        # STR-P1-01: a stop number cannot be read without knowing whether the
        # run used resident intrabar stops or the legacy next-open exit, and
        # which intrabar path priced them.
        if protective_stops:
            metrics["ProtectiveStops"] = dict(protective_stops)
        metrics["ExtendedAnalytics"] = extended
        metrics["MetricResults"] = self._headline_metric_results(metrics)
        if invalid_trades:
            for row in metrics["MetricResults"]:
                if row["name"] in {"ProfitFactor", "WinRate", "SQN"}:
                    row.update(value=None, status="invalid_input", reason="incomplete legacy trade facts")

        if not metrics_only:
            write_metrics_json(os.path.join(self.output_dir, "metrics.json"), metrics, metadata)
            exported_trades = [*closed_trades, *invalid_trades]
            pd.DataFrame(exported_trades, columns=None if exported_trades else
                         ["position_id", "symbol", "strategy_id", "net_pnl", "initial_risk"]).to_csv(
                             os.path.join(self.output_dir, "closed_trades.csv"), index=False)
            legs_pnl = sum(float(row.get("net_pnl", 0.0)) for row in closed_legs)
            positions_pnl = sum(float(row.get("net_pnl", 0.0)) for row in closed_trades)
            headline_pnl = float(metrics["NetPnL"]) if metrics.get("NetPnL") is not None else None
            tolerance = max(1e-8, abs(positions_pnl) * 1e-10)
            reconciled = (headline_pnl is not None and abs(legs_pnl - positions_pnl) <= tolerance
                          and abs(headline_pnl - positions_pnl) <= tolerance)
            write_metrics_json(os.path.join(self.output_dir, "reconciliation.json"), {
                "schema_version": "closed-trade-reconciliation/v1", "status": "invalid_input" if invalid_trades else "pass" if reconciled else "fail",
                "scope": "closed lot legs -> position round trips -> report NetPnL",
                "closed_leg_net_pnl": legs_pnl, "position_net_pnl": positions_pnl, "report_net_pnl": headline_pnl,
                "tolerance": tolerance, "account_cash_bridge": {"status": "not_modeled",
                    "reason": "report inputs do not include the capital/transfer ledger"}}, metadata)
            write_metrics_json(os.path.join(self.output_dir, "execution_quality.json"), extended["execution_quality"], metadata)
            write_metrics_json(os.path.join(self.output_dir, "invalid_closed_trades.json"),
                               {"status": "invalid_input" if invalid_trades else "ok", "records": invalid_trades}, metadata)
            if report_profile != "workbook":
                # 3. Save Report Text（report.txt 的分析型分节直接读 ExtendedAnalytics，
                # 不重新计算一遍——避免 trade_quality/attribution 等函数在同一次
                # generate() 调用里跑两次）。
                self._save_report_text(metrics, metadata, metrics["ExtendedAnalytics"])

                # 4. Generate Plots. Workbook builds native Excel charts from
                # the inputs and does not consume these PNG files.
                self._plot_equity(equity_curve, benchmark_curve)
                self._plot_monthly_heatmap(equity_curve)
                self._plot_rolling_metrics(equity_curve)
                self._plot_pnl_distribution(closed_trades)

            if report_profile == "workbook":
                from backtest.reporting.render.workbook import write_workbook_report
                write_workbook_report(
                    self.output_dir, metrics, equity_curve, trades, closed_trades,
                    benchmark_curve=benchmark_curve, metadata=metadata,
                    data_quality=data_quality,
                )
            else:
                # Directly viewable reading layer. In compact mode the PDF embeds
                # detailed charts while dashboard.png remains a one-glance view.
                from backtest.reporting.render.pdf import write_pdf_report
                write_pdf_report(
                    self.output_dir, metrics, equity_curve, closed_trades,
                    benchmark_curve=benchmark_curve, metadata=metadata,
                )
            if report_profile == "compact":
                for name in (
                    "report.txt", "equity.png", "monthly_returns_heatmap.png",
                    "rolling_metrics.png", "pnl_distribution.png",
                ):
                    try:
                        os.remove(os.path.join(self.output_dir, name))
                    except FileNotFoundError:
                        pass
            elif report_profile == "workbook":
                for name in (
                    "report.txt", "equity.csv", "trades.csv", "benchmark.csv",
                    "equity.png", "monthly_returns_heatmap.png",
                    "rolling_metrics.png", "pnl_distribution.png",
                ):
                    try:
                        os.remove(os.path.join(self.output_dir, name))
                    except FileNotFoundError:
                        pass

        return metrics
