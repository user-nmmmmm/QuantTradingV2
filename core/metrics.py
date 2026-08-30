"""Pure, side-effect-free backtest metric calculations.

Facade over the split metrics modules (A4) — see docs/architecture_review.md.
Every name that used to live here is re-exported unchanged so existing
``from core.metrics import ...`` call sites do not need to move:

- core/metrics_performance.py  — equity curve: Sharpe, drawdown, CAGR, exposure
- core/metrics_trade_quality.py — per-trade win/loss, profit factor, R-multiple
- core/metrics_attribution.py   — attribution, benchmark, cost sensitivity, funnel
- core/metrics_validation.py    — train/test split, walk-forward, bootstrap, FDR
"""
from __future__ import annotations

from core.metrics_performance import (
    SECONDS_PER_YEAR,
    METRICS_FORMULA_VERSION,
    infer_periods_per_year,
    monthly_returns,
    calculate_sharpe,
    calculate_drawdown,
    calculate_drawdown_events,
    calculate_equity_metrics,
    calculate_exposure,
)
from core.metrics_trade_quality import (
    calculate_profit_factor,
    calculate_trade_quality,
    calculate_r_multiple_stats,
)
from core.metrics_attribution import (
    calculate_signal_funnel,
    calculate_cost_sensitivity,
    calculate_attribution,
    calculate_benchmark_comparison,
    calculate_rolling_returns,
    calculate_segment_returns,
)
from core.metrics_validation import (
    train_test_split_returns,
    walk_forward_windows,
    bootstrap_return_distribution,
    monte_carlo_trade_sequence,
    benjamini_hochberg,
)

__all__ = [
    "SECONDS_PER_YEAR", "METRICS_FORMULA_VERSION",
    "infer_periods_per_year", "monthly_returns", "calculate_sharpe",
    "calculate_drawdown", "calculate_drawdown_events", "calculate_equity_metrics",
    "calculate_exposure", "calculate_profit_factor", "calculate_trade_quality",
    "calculate_r_multiple_stats", "calculate_signal_funnel", "calculate_cost_sensitivity",
    "calculate_attribution", "calculate_benchmark_comparison", "calculate_rolling_returns",
    "calculate_segment_returns", "train_test_split_returns", "walk_forward_windows",
    "bootstrap_return_distribution", "monte_carlo_trade_sequence", "benjamini_hochberg",
    "Metrics",
]


class Metrics:
    """Unused staticmethod bundle kept for API compatibility.

    No call site in this repository uses ``Metrics.xxx`` — every consumer
    imports the module-level functions above directly. Kept only in case an
    external caller depends on it; safe to delete once that is confirmed.
    """

    infer_periods_per_year = staticmethod(infer_periods_per_year)
    monthly_returns = staticmethod(monthly_returns)
    calculate_sharpe = staticmethod(calculate_sharpe)
    calculate_drawdown = staticmethod(calculate_drawdown)
    calculate_drawdown_events = staticmethod(calculate_drawdown_events)
    calculate_equity_metrics = staticmethod(calculate_equity_metrics)
    calculate_exposure = staticmethod(calculate_exposure)
    calculate_signal_funnel = staticmethod(calculate_signal_funnel)
    calculate_profit_factor = staticmethod(calculate_profit_factor)
    calculate_trade_quality = staticmethod(calculate_trade_quality)
    calculate_cost_sensitivity = staticmethod(calculate_cost_sensitivity)
    calculate_attribution = staticmethod(calculate_attribution)
    calculate_benchmark_comparison = staticmethod(calculate_benchmark_comparison)
    calculate_rolling_returns = staticmethod(calculate_rolling_returns)
    calculate_segment_returns = staticmethod(calculate_segment_returns)
    calculate_r_multiple_stats = staticmethod(calculate_r_multiple_stats)
    train_test_split_returns = staticmethod(train_test_split_returns)
    walk_forward_windows = staticmethod(walk_forward_windows)
    bootstrap_return_distribution = staticmethod(bootstrap_return_distribution)
    monte_carlo_trade_sequence = staticmethod(monte_carlo_trade_sequence)
    benjamini_hochberg = staticmethod(benjamini_hochberg)
