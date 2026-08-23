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
- 交易明细：基于 FIFO 重建已平仓交易，计算胜率、盈亏比、期望值等
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
    "TotalTrades": "Total Trades (总交易次数)",
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


class ReportGenerator:
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
        metadata: Dict[str, Any] = None,
        benchmark_curve: pd.Series = None,
        metrics_only: bool = False,
        close_events: Optional[Dict[str, int]] = None,
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
        """
        trades_df = pd.DataFrame(trades)

        if not metrics_only:
            # 1. Save CSVs
            equity_curve.to_csv(os.path.join(self.output_dir, "equity.csv"))

            if benchmark_curve is not None:
                benchmark_curve.to_csv(os.path.join(self.output_dir, "benchmark.csv"))

            if not trades_df.empty:
                trades_df.to_csv(os.path.join(self.output_dir, "trades.csv"), index=False)

        # 2. Calculate Metrics
        closed_trades = self._reconstruct_closed_trades(trades_df)
        trade_metrics = self._trade_metrics_from_closed(closed_trades)
        equity_metrics = self._calculate_equity_metrics(equity_curve)

        metrics = {**equity_metrics, **trade_metrics}

        extended = dict(metrics.get("ExtendedAnalytics") or {})
        extended["drawdown_events"] = calculate_drawdown_events(equity_curve["equity"])
        if benchmark_curve is not None:
            extended["benchmark_comparison"] = calculate_benchmark_comparison(
                equity_curve["equity"], benchmark_curve
            )
        # Trustworthiness diagnostics: whether the headline numbers can be
        # believed and whether the system behaves as its code claims. Kept in
        # its own key so consumers can tell "performance" from "credibility".
        metrics["Diagnostics"] = build_diagnostics(
            closed_trades, equity_curve["equity"], close_events
        )
        metrics["ExtendedAnalytics"] = extended
        metrics["MetricResults"] = self._headline_metric_results(metrics)

        if not metrics_only:
            # 3. Save Report Text（report.txt 的分析型分节直接读 ExtendedAnalytics，
            # 不重新计算一遍——避免 trade_quality/attribution 等函数在同一次
            # generate() 调用里跑两次）。
            self._save_report_text(metrics, metadata, metrics["ExtendedAnalytics"])

            # 4. Generate Plots
            self._plot_equity(equity_curve, benchmark_curve)
            self._plot_monthly_heatmap(equity_curve)
            self._plot_rolling_metrics(equity_curve)
            self._plot_pnl_distribution(closed_trades)

        return metrics

    def _calculate_equity_metrics(self, equity_curve: pd.DataFrame) -> Dict[str, Any]:
        """
        根据权益曲线计算绩效指标。

        约定：
        - equity_curve.index 为 DatetimeIndex
        - 根据 DatetimeIndex 的中位正间隔自动推断年化因子
        """
        return calculate_equity_metrics(equity_curve)

    def _headline_metric_results(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Wrap the report's headline scalar metrics as MetricResult entries (M-02).

        core.metric_result.MetricResult defines a typed, JSON-schema-validated
        contract that distinguishes a real zero from "not computable" — but it
        had zero production callers; core/metrics.py's ten functions each
        return their own ad hoc dict shape instead. Rewriting those functions
        to return MetricResult would touch every call site across the repo for
        uncertain benefit. Adopting the contract at the report boundary instead
        gives the schema real, tested production output without that blast
        radius.
        """
        extended = metrics.get("ExtendedAnalytics") or {}
        trade_quality = extended.get("trade_quality") or {}
        r_multiple = (extended.get("r_multiple") or {}).get("r_multiple") or {}

        candidates = (
            ("SharpeRatio", metrics.get("SharpeRatio"), metrics.get("SharpeStatus"),
             metrics.get("SharpeSamples"), None),
            ("CAGR", metrics.get("CAGR"), metrics.get("CAGRStatus"),
             metrics.get("MonthlyReturnSamples"), "ratio"),
            ("MaxDrawdownPct", metrics.get("MaxDrawdownPct"), metrics.get("DrawdownStatus"),
             None, "ratio"),
            ("ProfitFactor", metrics.get("ProfitFactor"), metrics.get("ProfitFactorStatus"),
             metrics.get("ProfitFactorSamples"), None),
            ("WinRate", trade_quality.get("win_rate"), trade_quality.get("status"),
             trade_quality.get("sample_size"), "ratio"),
            ("SQN", r_multiple.get("sqn"), r_multiple.get("status"),
             r_multiple.get("sample_size"), None),
        )

        results = []
        for name, value, status, sample_size, unit in candidates:
            status = status or ("ok" if value is not None else "undefined")
            if status == "ok":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = None
                if value is None or not math.isfinite(value):
                    status, value = "undefined", None
            else:
                value = None
            results.append(
                MetricResult(
                    name=name, value=value, status=status,
                    sample_size=int(sample_size or 0), unit=unit,
                ).to_dict()
            )
        return results

    def _extended_trade_analytics(
        self, closed_trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Trade-level analytics beyond the headline PF/win-rate summary (BM2/BM5/BM7).

        These call core.metrics functions that were previously computed
        nowhere in the production report path (core/metrics.py had them
        implemented and tested, but backtest/reporting.py never invoked
        them). Every one of these degrades to an "insufficient"/"excluded"
        status on empty or partial input rather than raising, so this is
        safe to call unconditionally, including on an empty closed_trades
        list.

        calculate_exposure/calculate_signal_funnel are deliberately not
        included here: they need per-timestamp position/price snapshots and
        the event pipeline's correlation-id stream respectively, neither of
        which is currently passed to ReportGenerator. Wiring those in would
        require threading extra state through backtest/engine.py and is out
        of scope for this pass.
        """
        return {
            "trade_quality": calculate_trade_quality(closed_trades),
            "attribution": calculate_attribution(closed_trades),
            "r_multiple": calculate_r_multiple_stats(closed_trades),
            "cost_sensitivity": calculate_cost_sensitivity(closed_trades),
        }

    def _analyze_trades(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        """基于 trades.csv 重建已平仓交易并统计交易级指标（薄封装，见下两个方法）。"""
        return self._trade_metrics_from_closed(self._reconstruct_closed_trades(trades_df))

    def _reconstruct_closed_trades(self, trades_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        基于成交明细重建已平仓交易（FIFO 配对开平仓）。

        方法：
        - 按 symbol 分组，使用 FIFO 栈（long_stack/short_stack）配对开平仓
        - 每条闭合记录附带 symbol、entry_time、exit_time（若成交明细无 fill_time
          列则为 None），供 core.metrics 的归因/交易质量函数使用
        """
        if trades_df.empty:
            return []

        closed_trades: List[Dict[str, Any]] = []

        # Group by symbol
        for symbol, group in trades_df.groupby("symbol"):
            long_stack: Deque[
                Tuple[float, float, str, float, float, Any]
            ] = deque()  # (qty, price, strategy_id, unit_comm, unit_slip, entry_time)
            short_stack: Deque[
                Tuple[float, float, str, float, float, Any]
            ] = deque()

            columns = list(group.columns)
            column_index = {name: index for index, name in enumerate(columns)}
            slip_index = column_index.get("slip")
            strategy_index = column_index.get("strategy_id")
            fill_time_index = column_index.get("fill_time")
            exit_reason_index = column_index.get("exit_reason")
            for row in group.itertuples(index=False, name=None):
                side = row[column_index["side"]]
                qty = row[column_index["qty"]]
                price = row[column_index["fill_price"]]
                comm = row[column_index["commission"]]
                # Broker stores 'slip' as unit price difference (absolute)
                unit_slip = row[slip_index] if slip_index is not None else 0.0

                unit_comm = comm / qty if qty > 0 else 0.0

                strategy_id = (
                    row[strategy_index] if strategy_index is not None else "Unknown"
                )
                fill_time = row[fill_time_index] if fill_time_index is not None else None
                # The closing fill carries who ended the trade and why. Keeping
                # only the entry strategy hides the case where a position is
                # force-closed by the router rather than by the strategy's own
                # exit rule — which is invisible in strategy/symbol attribution.
                exit_reason = (
                    row[exit_reason_index] if exit_reason_index is not None else None
                )

                if side == "buy":
                    # Check if covering short
                    remaining = qty
                    while remaining > 0 and short_stack:
                        s_qty, s_price, s_strat, s_unit_comm, s_unit_slip, s_time = (
                            short_stack.popleft()
                        )
                        matched = min(remaining, s_qty)

                        # Short PnL: (Entry - Exit) * qty
                        gross_pnl = (s_price - price) * matched

                        # Commission: Entry + Exit
                        trade_comm = (s_unit_comm + unit_comm) * matched

                        # Slippage: Entry + Exit
                        # Note: Slippage is always a cost (positive value in record)
                        trade_slip = (s_unit_slip + unit_slip) * matched

                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                                "symbol": symbol,
                                "entry_time": s_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.appendleft(
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                    s_time,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip, fill_time)
                        )

                elif side == "sell":
                    # Close Long
                    remaining = qty
                    while remaining > 0 and long_stack:
                        l_qty, l_price, l_strat, l_unit_comm, l_unit_slip, l_time = (
                            long_stack.popleft()
                        )
                        matched = min(remaining, l_qty)

                        # Long PnL: (Exit - Entry) * qty
                        gross_pnl = (price - l_price) * matched
                        trade_comm = (l_unit_comm + unit_comm) * matched
                        trade_slip = (l_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": l_strat,
                                "symbol": symbol,
                                "entry_time": l_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                            }
                        )

                        remaining -= matched
                        if l_qty > matched:
                            long_stack.appendleft(
                                (
                                    l_qty - matched,
                                    l_price,
                                    l_strat,
                                    l_unit_comm,
                                    l_unit_slip,
                                    l_time,
                                ),
                            )

                    if remaining > 0:
                        short_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip, fill_time)
                        )

                elif side == "short":
                    # Open Short
                    short_stack.append(
                        (qty, price, strategy_id, unit_comm, unit_slip, fill_time)
                    )

                elif side == "cover":
                    # Close Short (Buy to Cover)
                    remaining = qty
                    while remaining > 0 and short_stack:
                        s_qty, s_price, s_strat, s_unit_comm, s_unit_slip, s_time = (
                            short_stack.popleft()
                        )
                        matched = min(remaining, s_qty)

                        gross_pnl = (s_price - price) * matched
                        trade_comm = (s_unit_comm + unit_comm) * matched
                        trade_slip = (s_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                                "symbol": symbol,
                                "entry_time": s_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.appendleft(
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                    s_time,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip, fill_time)
                        )

        return closed_trades

    def _trade_metrics_from_closed(
        self, closed_trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """把 `_reconstruct_closed_trades` 的输出聚合为控制台/report.txt 用的扁平指标字典。"""
        if not closed_trades:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": None,
                "ProfitFactorStatus": "insufficient",
                "ProfitFactorSamples": 0,
                "ProfitFactorLosses": 0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
                "ExtendedAnalytics": self._extended_trade_analytics([]),
            }

        # 1. Global Metrics
        all_net_pnls = [t["net_pnl"] for t in closed_trades]
        all_gross_pnls = [t["gross_pnl"] for t in closed_trades]
        all_comms = [t["commission"] for t in closed_trades]
        all_slips = [t["slippage"] for t in closed_trades]

        if not all_net_pnls:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": None,
                "ProfitFactorStatus": "insufficient",
                "ProfitFactorSamples": 0,
                "ProfitFactorLosses": 0,
                "Expectancy": 0.0,
                "AvgWin": 0.0,
                "AvgLoss": 0.0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
                "ExtendedAnalytics": self._extended_trade_analytics([]),
            }

        wins = [p for p in all_net_pnls if p > 0]
        losses = [p for p in all_net_pnls if p < 0]

        win_rate = len(wins) / len(all_net_pnls)
        pf = calculate_profit_factor(all_net_pnls)

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

        metrics = {
            "TotalTrades": len(all_net_pnls),
            "WinRate": win_rate,
            "ProfitFactor": pf["value"],
            "ProfitFactorStatus": pf["status"],
            "ProfitFactorSamples": pf["sample_size"],
            "ProfitFactorLosses": pf["loss_count"],
            "ProfitFactorLower95": pf["lower"],
            "ProfitFactorUpper95": pf["upper"],
            "Expectancy": expectancy,
            "AvgWin": avg_win,
            "AvgLoss": avg_loss,
            "GrossPnL": sum(all_gross_pnls),
            "TotalCommission": sum(all_comms),
            "TotalSlippage": sum(all_slips),
            "NetPnL": sum(all_net_pnls),
            "ExtendedAnalytics": self._extended_trade_analytics(closed_trades),
        }

        # 2. Per Strategy Metrics
        strat_map = {}
        for t in closed_trades:
            s = t["strategy"]
            if s not in strat_map:
                strat_map[s] = []
            strat_map[s].append(t)

        for s, trades in strat_map.items():
            pnls = [t["net_pnl"] for t in trades]
            s_wins = [p for p in pnls if p > 0]
            s_losses = [p for p in pnls if p < 0]
            s_wr = len(s_wins) / len(pnls)
            s_pf = calculate_profit_factor(pnls)
            s_total = sum(pnls)
            s_comm = sum(t["commission"] for t in trades)
            s_slip = sum(t["slippage"] for t in trades)

            metrics[f"Strat_{s}_Trades"] = len(pnls)
            metrics[f"Strat_{s}_WinRate"] = s_wr
            metrics[f"Strat_{s}_ProfitFactor"] = s_pf["value"]
            metrics[f"Strat_{s}_ProfitFactorStatus"] = s_pf["status"]
            metrics[f"Strat_{s}_ProfitFactorSamples"] = s_pf["sample_size"]
            metrics[f"Strat_{s}_ProfitFactorLosses"] = s_pf["loss_count"]
            metrics[f"Strat_{s}_NetPnL"] = s_total
            metrics[f"Strat_{s}_Comm"] = s_comm
            metrics[f"Strat_{s}_Slip"] = s_slip

        return metrics

    def _save_report_text(
        self,
        metrics: Dict[str, Any],
        metadata: Dict[str, Any] = None,
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
            f.write(format_primary_metrics(metrics))
            f.write("\n\n")

            f.write("Full Metrics (完整指标明细):\n")
            f.write("-----------------\n")
            for k, v in metrics.items():
                # Diagnostics is a deep nested structure with its own rendered
                # section below; dumping it here would bury the scalar metrics.
                if k == "Diagnostics":
                    continue
                display_key = METRIC_NAMES.get(k, k)
                f.write(f"{display_key:<45}: {_format_metric_value(v)}\n")
            f.write("\n")

            self._write_drawdown_events_section(f, analysis.get("drawdown_events"))
            self._write_trade_quality_section(f, analysis.get("trade_quality"))
            self._write_attribution_section(f, analysis.get("attribution"))
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

    @staticmethod
    def _write_trade_quality_section(f, quality: Optional[Dict[str, Any]]) -> None:
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
        f.write(f"Win Rate (胜率)                   : {_format_metric_value(quality['win_rate'])}\n")
        f.write(f"Expectancy (期望值)               : {_format_metric_value(quality['expectancy'])}\n")
        f.write(
            f"Profit Factor (盈亏比)            : "
            f"{_format_metric_value(quality['profit_factor'])} "
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
                f"profit_factor={_format_metric_value(stats['profit_factor'])}\n"
            )
        f.write("\nBy Strategy (按策略):\n")
        for strategy, stats in (quality.get("by_strategy") or {}).items():
            f.write(
                f"  {strategy:<15} trades={stats['sample_size']:<6} "
                f"win_rate={stats['win_rate']:.2%} net_pnl={stats['net_pnl']:.4f} "
                f"profit_factor={_format_metric_value(stats['profit_factor'])}\n"
            )
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

    def _plot_equity(
        self, equity_curve: pd.DataFrame, benchmark_curve: pd.Series = None
    ):
        """
        生成 equity.png 四联图：
        1) 策略净值与基准净值
        2) 回撤曲线
        3) 日收益柱状图
        4) 现金/持仓市值堆叠图（若 equity_curve 含 cash 列）
        """
        try:
            # Create a figure with 4 subplots
            fig = plt.figure(figsize=(16, 12))
            gs = fig.add_gridspec(4, 1, height_ratios=[2, 1, 1, 1])

            ax1 = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1], sharex=ax1)
            ax3 = fig.add_subplot(gs[2], sharex=ax1)
            ax4 = fig.add_subplot(gs[3], sharex=ax1)

            # Ensure font supports basic text
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            # Plot 1: Equity Curve
            ax1.plot(
                equity_curve.index,
                equity_curve["equity"],
                label="Strategy Equity (策略净值)",
                color="blue",
                linewidth=1.5,
            )

            if benchmark_curve is not None:
                # Align benchmark to equity curve (ensure same index range if possible)
                # But usually plotting handles date index fine.
                ax1.plot(
                    benchmark_curve.index,
                    benchmark_curve,
                    label="Benchmark (Buy & Hold)",
                    color="gray",
                    linewidth=1.0,
                    linestyle="--",
                )

            ax1.set_title("Equity Curve (净值曲线)", fontsize=12, fontweight="bold")
            ax1.set_ylabel("Value (USDT)")
            ax1.legend(loc="upper left")
            ax1.grid(True, which="both", linestyle="--", alpha=0.6)

            # Plot 2: Drawdown
            rolling_max = equity_curve["equity"].cummax()
            drawdown = (equity_curve["equity"] - rolling_max) / rolling_max

            ax2.fill_between(
                drawdown.index, drawdown, 0, color="red", alpha=0.3, label="Drawdown"
            )
            ax2.plot(drawdown.index, drawdown, color="red", linewidth=1)
            ax2.set_title("Drawdown % (回撤率)")
            ax2.set_ylabel("Percentage")
            ax2.axhline(0, color="black", linewidth=0.5)
            ax2.grid(True, which="both", linestyle="--", alpha=0.6)

            # Plot 3: Daily Returns
            returns = equity_curve["equity"].pct_change().fillna(0)
            colors = ["green" if x >= 0 else "red" for x in returns]
            ax3.bar(
                returns.index, returns, color=colors, alpha=0.7, label="Daily Return"
            )
            ax3.set_title("Daily Returns (日收益率)")
            ax3.set_ylabel("Return %")
            ax3.grid(True, axis="y", linestyle="--", alpha=0.6)

            # Plot 4: Cash vs Position (Asset Allocation)
            # Assuming 'cash' column exists, otherwise infer from equity
            if "cash" in equity_curve.columns:
                cash = equity_curve["cash"]
                # Position value = Equity - Cash
                position_val = equity_curve["equity"] - cash

                ax4.stackplot(
                    equity_curve.index,
                    [cash, position_val],
                    labels=["Cash (现金)", "Position Value (持仓市值)"],
                    colors=["lightgray", "orange"],
                    alpha=0.6,
                )
                ax4.set_title("Asset Allocation (资产分布)")
                ax4.set_ylabel("Value (USDT)")
                ax4.legend(loc="upper left")
                ax4.grid(True, which="both", linestyle="--", alpha=0.6)

            ax4.set_xlabel("Date")

            # Save through the figure object, not the pyplot global "current
            # figure": a figure leaked by an earlier call would otherwise
            # receive the write, producing a wrong or empty image.
            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "equity.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving plot: %s", e)

    def _plot_monthly_heatmap(self, equity_curve: pd.DataFrame) -> None:
        """月度收益热力图（年 x 月）。数据不足（不足一个完整月）时跳过，不画空图。"""
        try:
            returns = monthly_returns(equity_curve["equity"])
            if returns.empty:
                logger.info("Monthly heatmap skipped: insufficient monthly samples")
                return

            table = returns.to_frame("ret")
            table["year"] = table.index.year
            table["month"] = table.index.month
            pivot = table.pivot(index="year", columns="month", values="ret")
            pivot = pivot.reindex(columns=range(1, 13))

            fig, ax = plt.subplots(
                figsize=(12, max(2.0, 0.6 * len(pivot) + 1.5))
            )
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            data = pivot.to_numpy(dtype=float)
            vmax = np.nanmax(np.abs(data)) if np.isfinite(data).any() else 1.0
            vmax = vmax if vmax > 0 else 1.0
            im = ax.imshow(data, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")

            ax.set_xticks(range(12))
            ax.set_xticklabels([f"{m:02d}" for m in range(1, 13)])
            ax.set_yticks(range(len(pivot)))
            ax.set_yticklabels(pivot.index.astype(str))
            ax.set_title("Monthly Returns (月度收益热力图)", fontsize=12, fontweight="bold")

            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    value = data[i, j]
                    if np.isnan(value):
                        continue
                    ax.text(
                        j, i, f"{value * 100:.1f}%",
                        ha="center", va="center", fontsize=8, color="black",
                    )

            fig.colorbar(im, ax=ax, label="Return")
            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "monthly_returns_heatmap.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving monthly heatmap: %s", e)

    def _plot_rolling_metrics(
        self, equity_curve: pd.DataFrame, window: int = 30
    ) -> None:
        """滚动 Sharpe 与滚动最大回撤（trailing window，无前视偏差）。

        样本不足 2*window 时跳过：滚动窗口指标在样本太短时噪声过大，容易被
        误读为有信息量的信号。
        """
        try:
            equity = equity_curve["equity"].dropna()
            if len(equity) < window * 2:
                logger.info(
                    "Rolling metrics skipped: %d samples < 2x window (%d)",
                    len(equity), window,
                )
                return

            periods_per_year = infer_periods_per_year(equity.index) or 252.0
            returns = equity.pct_change().dropna()

            rolling_mean = returns.rolling(window).mean()
            rolling_std = returns.rolling(window).std()
            rolling_sharpe = (
                rolling_mean / rolling_std * np.sqrt(periods_per_year)
            ).replace([np.inf, -np.inf], np.nan).dropna()

            rolling_max = equity.cummax()
            rolling_drawdown = (equity - rolling_max) / rolling_max
            rolling_max_dd = rolling_drawdown.rolling(window).min().dropna()

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            ax1.plot(rolling_sharpe.index, rolling_sharpe, color="steelblue")
            ax1.axhline(0, color="black", linewidth=0.5)
            ax1.set_title(
                f"Rolling Sharpe ({window}-period window) (滚动夏普)",
                fontsize=12, fontweight="bold",
            )
            ax1.grid(True, linestyle="--", alpha=0.6)

            ax2.fill_between(
                rolling_max_dd.index, rolling_max_dd, 0, color="red", alpha=0.3
            )
            ax2.plot(rolling_max_dd.index, rolling_max_dd, color="red", linewidth=1)
            ax2.set_title(
                f"Rolling Max Drawdown ({window}-period window) (滚动最大回撤)",
                fontsize=12, fontweight="bold",
            )
            ax2.set_xlabel("Date")
            ax2.grid(True, linestyle="--", alpha=0.6)

            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "rolling_metrics.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving rolling metrics plot: %s", e)

    def _plot_pnl_distribution(self, closed_trades: List[Dict[str, Any]]) -> None:
        """已平仓交易净盈亏分布直方图（区分盈利/亏损配色）。"""
        try:
            if not closed_trades:
                logger.info("PnL distribution skipped: no closed trades")
                return

            net_pnls = [t["net_pnl"] for t in closed_trades if t.get("net_pnl") is not None]
            if not net_pnls:
                return

            fig, ax = plt.subplots(figsize=(10, 6))
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            wins = [p for p in net_pnls if p > 0]
            losses = [p for p in net_pnls if p <= 0]
            bins = min(30, max(5, len(net_pnls) // 2)) or 5

            ax.hist(wins, bins=bins, color="green", alpha=0.6, label=f"Wins (n={len(wins)})")
            ax.hist(losses, bins=bins, color="red", alpha=0.6, label=f"Losses (n={len(losses)})")
            ax.axvline(0, color="black", linewidth=0.8)
            ax.set_title("Closed Trade Net PnL Distribution (已平仓交易净盈亏分布)",
                         fontsize=12, fontweight="bold")
            ax.set_xlabel("Net PnL (USDT)")
            ax.set_ylabel("Count")
            ax.legend(loc="upper right")
            ax.grid(True, axis="y", linestyle="--", alpha=0.6)

            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "pnl_distribution.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving PnL distribution plot: %s", e)
