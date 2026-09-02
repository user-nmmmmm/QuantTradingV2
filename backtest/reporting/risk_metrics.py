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
    "TotalTrades": "Round trips",
    "ClosedTradeLegs": "Fill legs",
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


EXPOSURE_COLUMNS = (
    "gross_exposure", "net_exposure", "priced_symbols",
    "gross_exposure_pct_equity", "net_exposure_pct_equity",
)


def summarize_exposure(equity_curve: pd.DataFrame) -> dict[str, Any]:
    """How invested the book was, per period, behind the equity curve (BM3).

    A return series says nothing about the risk that produced it: a flat
    stretch of equity means one thing at 2x gross and another thing sitting in
    cash, and Sharpe reads identically either way. These come from the
    exposure columns ``BacktestEngine`` joins onto the curve, so they describe
    the same rows the return statistics do.

    ``mean_gross_leverage`` averages every period including flat ones -
    that is the number to compare against a buy-and-hold benchmark's 1.0.
    ``mean_gross_leverage_invested`` averages only the periods actually in the
    market, which is the leverage the strategy runs when it has a view. A wide
    gap between the two means the headline risk statistics are diluted by time
    spent flat.

    ``core.metrics.calculate_exposure`` contributes a symbol only when it has
    a mark, so a held position whose price is missing lands in neither the
    exposure nor ``priced_symbols``; on a backtest every held symbol has a bar
    and therefore a mark, but that is the one case these numbers cannot see.
    """
    if not isinstance(equity_curve, pd.DataFrame) or equity_curve.empty:
        return {"status": "insufficient", "sample_size": 0}
    if any(column not in equity_curve.columns for column in EXPOSURE_COLUMNS):
        return {"status": "not_recorded", "sample_size": int(len(equity_curve))}

    gross = pd.to_numeric(equity_curve["gross_exposure"], errors="coerce").dropna()
    if gross.empty:
        return {"status": "insufficient", "sample_size": 0}
    net_pct = pd.to_numeric(
        equity_curve["net_exposure_pct_equity"], errors="coerce"
    ).reindex(gross.index)
    gross_pct = pd.to_numeric(
        equity_curve["gross_exposure_pct_equity"], errors="coerce"
    ).reindex(gross.index)
    held = pd.to_numeric(equity_curve["priced_symbols"], errors="coerce").reindex(
        gross.index
    ).fillna(0.0)
    invested = gross > 0

    def _mean(series: pd.Series) -> float | None:
        clean = series.dropna()
        return float(clean.mean()) if len(clean) else None

    return {
        "status": "ok",
        "sample_size": int(len(gross)),
        "time_in_market_ratio": float(invested.mean()),
        "invested_periods": int(invested.sum()),
        "mean_gross_leverage": _mean(gross_pct),
        "mean_gross_leverage_invested": _mean(gross_pct[invested]),
        "max_gross_leverage": (
            float(gross_pct.max()) if gross_pct.notna().any() else None
        ),
        "mean_net_leverage": _mean(net_pct),
        "max_net_long_leverage": (
            float(net_pct.max()) if net_pct.notna().any() else None
        ),
        "max_net_short_leverage": (
            float(net_pct.min()) if net_pct.notna().any() else None
        ),
        "long_period_ratio": float((net_pct > 0).mean()),
        "short_period_ratio": float((net_pct < 0).mean()),
        "mean_open_positions": _mean(held),
        "max_open_positions": int(held.max()),
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
    "EXPOSURE_COLUMNS", "METRIC_LABELS", "PERCENT_METRIC_KEYS",
    "calculate_active_risk_metrics", "calculate_portfolio_risk_metrics",
    "summarize_exposure",
]
