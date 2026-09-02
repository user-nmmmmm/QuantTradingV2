"""Metric calculation mixin for :class:`ReportGenerator`."""

import math
from typing import Any, Dict, List

import pandas as pd

from core.metric_result import MetricResult
from core.metrics import (
    calculate_attribution,
    calculate_cost_sensitivity,
    calculate_equity_metrics,
    calculate_r_multiple_stats,
    calculate_trade_quality,
)


class ReportMetricsMixin:
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

        calculate_exposure is not called here because it is not a trade-level
        statistic: BacktestEngine samples the book on every bar and joins the
        exposure columns onto the equity curve itself, and generate() derives
        ExtendedAnalytics["exposure"] from those columns
        (backtest/reporting/risk_metrics.py:summarize_exposure).

        calculate_signal_funnel is still unwired: it needs the event
        pipeline's correlation-id stream, which is returned by the engine as
        ``event_log`` but not passed to ReportGenerator.
        """
        return {
            "trade_quality": calculate_trade_quality(closed_trades),
            "attribution": calculate_attribution(closed_trades),
            "r_multiple": calculate_r_multiple_stats(closed_trades),
            "cost_sensitivity": calculate_cost_sensitivity(closed_trades),
        }

