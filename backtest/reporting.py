import math
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
from collections import deque
from typing import Deque, List, Dict, Any, Optional, Tuple

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
        metrics["ExtendedAnalytics"] = extended
        metrics["MetricResults"] = self._headline_metric_results(metrics)

        if not metrics_only:
            # 3. Save Report Text（report.txt 的分析型分节直接读 ExtendedAnalytics，
            # 不重新计算一遍——避免 trade_quality/attribution 等函数在同一次
            # generate() 调用里跑两次）。
            self._save_report_text(metrics, metadata, metrics["ExtendedAnalytics"])

            # 4. Generate Plots
            self._plot_equity(equity_curve, benchmark_curve)

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
                display_key = METRIC_NAMES.get(k, k)
                f.write(f"{display_key:<45}: {_format_metric_value(v)}\n")
            f.write("\n")

            self._write_drawdown_events_section(f, analysis.get("drawdown_events"))
            self._write_trade_quality_section(f, analysis.get("trade_quality"))
            self._write_attribution_section(f, analysis.get("attribution"))
            self._write_benchmark_section(f, analysis.get("benchmark_comparison"))

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

            plt.tight_layout()
            output_path = os.path.join(self.output_dir, "equity.png")
            plt.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close()
        except Exception as e:
            logger.error("Error saving plot: %s", e)
