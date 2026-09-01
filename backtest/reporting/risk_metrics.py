"""Shared return and benchmark-risk calculations for report renderers.

Renderers consume these facts but do not own them.  Keeping the calculations
here prevents the workbook and PDF formats from drifting apart.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from core.metrics import infer_periods_per_year, monthly_returns


METRIC_LABELS = {
    "TotalReturn": "Total return",
    "CAGR": "CAGR",
    "MaxDrawdownPct": "Max drawdown",
    "SharpeRatio": "Sharpe",
    "TotalTrades": "Closed trades",
    "WinRate": "Win rate",
    "ProfitFactor": "Profit factor",
    "NetPnL": "Net PnL",
    "EndEquity": "End equity",
    "annualized_return_arithmetic": "Annual return (mean)",
    "annualized_volatility": "Annual volatility",
    "downside_deviation": "Downside deviation",
    "sortino_ratio": "Sortino",
    "calmar_ratio": "Calmar",
    "var_95_period": "Historical VaR 95%",
    "cvar_95_period": "Historical CVaR 95%",
    "return_skewness": "Return skewness",
    "return_excess_kurtosis": "Excess kurtosis",
    "positive_period_ratio": "Positive periods",
    "positive_month_ratio": "Positive months",
    "best_month": "Best month",
    "worst_month": "Worst month",
    "beta": "Beta",
    "annualized_alpha": "Annual alpha",
    "tracking_error": "Tracking error",
    "information_ratio": "Information ratio",
    "return_correlation": "Correlation",
    "up_capture": "Up capture",
    "down_capture": "Down capture",
}

PERCENT_METRIC_KEYS = {
    "TotalReturn", "CAGR", "MaxDrawdownPct", "WinRate",
    "annualized_return_arithmetic", "annualized_volatility",
    "downside_deviation", "var_95_period", "cvar_95_period",
    "positive_period_ratio", "positive_month_ratio", "best_month",
    "worst_month", "annualized_alpha", "tracking_error",
}


def calculate_portfolio_risk_metrics(equity: pd.Series) -> dict[str, Any]:
    """Institutional-style return/risk statistics derived from an equity path."""
    clean = pd.Series(equity, copy=True).replace([np.inf, -np.inf], np.nan).dropna()
    returns = clean.pct_change(fill_method=None).dropna()
    ppy = infer_periods_per_year(clean.index)
    if len(returns) < 2 or not ppy:
        return {"status": "insufficient", "sample_size": int(len(returns))}
    annual_return = float(returns.mean() * ppy)
    annual_vol = float(returns.std(ddof=1) * math.sqrt(ppy))
    downside = returns[returns < 0]
    downside_dev = (
        float(np.sqrt(np.mean(np.square(downside))) * math.sqrt(ppy))
        if len(downside) else None
    )
    running_peak = clean.cummax()
    max_dd = float(((clean - running_peak) / running_peak.replace(0, np.nan)).min())
    elapsed_years = (clean.index[-1] - clean.index[0]).total_seconds() / (365.25 * 86400)
    cagr = (
        float((clean.iloc[-1] / clean.iloc[0]) ** (1 / elapsed_years) - 1)
        if elapsed_years > 0 and clean.iloc[0] > 0 and clean.iloc[-1] >= 0 else None
    )
    monthlies = monthly_returns(clean)
    var95 = float(returns.quantile(0.05))
    tail = returns[returns <= var95]
    return {
        "status": "ok", "sample_size": int(len(returns)),
        "annualized_return_arithmetic": annual_return,
        "annualized_volatility": annual_vol,
        "downside_deviation": downside_dev,
        "sortino_ratio": annual_return / downside_dev if downside_dev else None,
        "calmar_ratio": cagr / abs(max_dd) if cagr is not None and max_dd < 0 else None,
        "var_95_period": var95,
        "cvar_95_period": float(tail.mean()) if len(tail) else None,
        "return_skewness": float(returns.skew()),
        "return_excess_kurtosis": float(returns.kurt()),
        "positive_period_ratio": float((returns > 0).mean()),
        "positive_month_ratio": float((monthlies > 0).mean()) if len(monthlies) else None,
        "best_month": float(monthlies.max()) if len(monthlies) else None,
        "worst_month": float(monthlies.min()) if len(monthlies) else None,
    }


def calculate_active_risk_metrics(
    equity: pd.Series, benchmark: pd.Series | None
) -> dict[str, Any]:
    """Alpha/beta and active-risk statistics on aligned periodic returns."""
    if benchmark is None:
        return {"status": "not_provided"}
    joined = pd.concat(
        [pd.Series(equity).rename("strategy"), pd.Series(benchmark).rename("benchmark")],
        axis=1, join="inner",
    ).replace([np.inf, -np.inf], np.nan).dropna()
    rets = joined.pct_change(fill_method=None).dropna()
    ppy = infer_periods_per_year(joined.index)
    if len(rets) < 3 or not ppy:
        return {"status": "insufficient", "sample_size": int(len(rets))}
    active = rets["strategy"] - rets["benchmark"]
    bench_var = float(rets["benchmark"].var(ddof=1))
    beta = float(rets["strategy"].cov(rets["benchmark"]) / bench_var) if bench_var > 0 else None
    alpha = (
        float((rets["strategy"].mean() - beta * rets["benchmark"].mean()) * ppy)
        if beta is not None else None
    )
    tracking_error = float(active.std(ddof=1) * math.sqrt(ppy))

    def capture(mask: pd.Series) -> float | None:
        if not mask.any():
            return None
        denominator = float(rets.loc[mask, "benchmark"].mean())
        return float(rets.loc[mask, "strategy"].mean() / denominator) if denominator else None

    return {
        "status": "ok", "sample_size": int(len(rets)), "beta": beta,
        "annualized_alpha": alpha, "tracking_error": tracking_error,
        "information_ratio": float(active.mean() * ppy / tracking_error) if tracking_error else None,
        "return_correlation": float(rets["strategy"].corr(rets["benchmark"])),
        "up_capture": capture(rets["benchmark"] > 0),
        "down_capture": capture(rets["benchmark"] < 0),
    }


__all__ = [
    "METRIC_LABELS", "PERCENT_METRIC_KEYS", "calculate_active_risk_metrics",
    "calculate_portfolio_risk_metrics",
]
