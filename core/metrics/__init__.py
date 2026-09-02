"""Pure, side-effect-free backtest metric calculations.

Facade over the split metrics modules (A4) — see docs/architecture_review.md.
Every name that used to live here is re-exported unchanged so existing
``from core.metrics import ...`` call sites do not need to move:

- performance.py   — equity curve: Sharpe, drawdown, CAGR, exposure
- trade_quality.py — per-trade win/loss, profit factor, R-multiple
- attribution.py   — attribution, benchmark, cost sensitivity, funnel
- validation.py    — train/test split, walk-forward, bootstrap, FDR
"""
from __future__ import annotations

from core.metrics.performance import (
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
from core.metrics.trade_quality import (
    calculate_profit_factor,
    calculate_trade_quality,
    calculate_r_multiple_stats,
)
from core.metrics.attribution import (
    calculate_signal_funnel,
    calculate_cost_sensitivity,
    calculate_attribution,
    calculate_benchmark_comparison,
    calculate_rolling_returns,
    calculate_segment_returns,
)
from core.metrics.validation import (
    train_test_split_returns,
    walk_forward_windows,
    bootstrap_return_distribution,
    monte_carlo_trade_sequence,
    one_sided_bootstrap_p_value,
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
    "bootstrap_return_distribution", "monte_carlo_trade_sequence",
    "one_sided_bootstrap_p_value", "benjamini_hochberg",
]

