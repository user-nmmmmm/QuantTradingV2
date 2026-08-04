"""Pure, side-effect-free backtest metric calculations."""
from __future__ import annotations
from typing import Any, Dict, Iterable, Optional
import numpy as np
import pandas as pd

SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60
METRICS_FORMULA_VERSION = "1.0"


def infer_periods_per_year(index: pd.Index) -> Optional[float]:
    """Infer observations/year from median positive spacing (robust to missing bars)."""
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 2:
        return None
    timestamps = pd.DatetimeIndex(index).dropna().unique().sort_values()
    if len(timestamps) < 2:
        return None
    deltas = np.diff(timestamps.asi8) / 1_000_000_000.0
    positive = deltas[deltas > 0]
    if len(positive) == 0:
        return None
    seconds = float(np.median(positive))
    return SECONDS_PER_YEAR / seconds if np.isfinite(seconds) and seconds > 0 else None


def monthly_returns(equity: pd.Series) -> pd.Series:
    """Changes between consecutive calendar month-end observations."""
    clean = _clean_equity(equity)
    if clean.empty or not isinstance(clean.index, pd.DatetimeIndex):
        return pd.Series(dtype=float, name="monthly_return")
    result = clean.resample("ME").last().dropna().pct_change(fill_method=None).dropna()
    result.name = "monthly_return"
    return result


def calculate_sharpe(returns: pd.Series, periods_per_year: Optional[float]) -> Dict[str, Any]:
    clean = pd.Series(returns, copy=True).replace([np.inf, -np.inf], np.nan).dropna()
    n = int(len(clean))
    if n < 2:
        return _result(None, "insufficient", n)
    if periods_per_year is None or not np.isfinite(periods_per_year) or periods_per_year <= 0:
        return _result(None, "undefined", n)
    volatility = float(clean.std(ddof=1))
    if not np.isfinite(volatility) or np.isclose(volatility, 0.0):
        return _result(None, "undefined", n)
    return _result(float(clean.mean() / volatility * np.sqrt(periods_per_year)), "ok", n)


def calculate_drawdown(equity: pd.Series) -> Dict[str, Any]:
    clean = _clean_equity(equity)
    empty = {"status": "insufficient", "sample_size": 0, "max_pct": None,
             "max_amount": None, "current_pct": None, "peak": None, "trough": None,
             "recovery": None, "duration_periods": None, "duration_days": None,
             "recovery_periods": None, "recovery_days": None,
             "underwater_ratio": None, "is_open": None}
    if clean.empty:
        return empty
    rolling_peak = clean.cummax()
    amount = clean - rolling_peak
    valid = (amount / rolling_peak.replace(0.0, np.nan)).dropna()
    if valid.empty:
        return {**empty, "status": "undefined", "sample_size": int(len(clean))}
    trough = valid.idxmin()
    through_trough = clean.loc[:trough]
    peak = through_trough.idxmax()
    peak_value = float(through_trough.loc[peak])
    recovered = clean.loc[trough:][clean.loc[trough:] >= peak_value]
    recovery = recovered.index[0] if not recovered.empty else None
    end = recovery if recovery is not None else clean.index[-1]
    peak_pos, trough_pos, end_pos = (int(clean.index.get_indexer([x])[0]) for x in (peak, trough, end))
    return {"status": "ok", "sample_size": int(len(clean)), "max_pct": float(valid.min()),
            "max_amount": float(amount.loc[trough]), "current_pct": float(valid.iloc[-1]),
            "peak": peak, "trough": trough, "recovery": recovery,
            "duration_periods": end_pos - peak_pos, "duration_days": _elapsed_days(peak, end),
            "recovery_periods": end_pos - trough_pos if recovery is not None else None,
            "recovery_days": _elapsed_days(trough, recovery) if recovery is not None else None,
            "underwater_ratio": float((valid < 0).mean()),
            "is_open": recovery is None and float(valid.loc[trough]) < 0}


def calculate_equity_metrics(equity_curve: pd.DataFrame) -> Dict[str, Any]:
    """Calculate P0 equity metrics without changing the input DataFrame."""
    if "equity" not in equity_curve.columns:
        raise ValueError("equity_curve must contain an 'equity' column")
    equity = _clean_equity(equity_curve["equity"])
    if equity.empty:
        return {}
    annualisation = infer_periods_per_year(equity.index)
    sharpe = calculate_sharpe(equity.pct_change(fill_method=None).dropna(), annualisation)
    drawdown = calculate_drawdown(equity)
    monthlies = monthly_returns(equity)
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    elapsed_days = _elapsed_days(equity.index[0], equity.index[-1])
    if start <= 0 or end < 0 or elapsed_days is None or elapsed_days <= 0:
        cagr, cagr_status = None, "undefined" if len(equity) >= 2 else "insufficient"
    else:
        cagr, cagr_status = float((end / start) ** (365.25 / elapsed_days) - 1), "ok"
    return {"CAGR": cagr, "CAGRStatus": cagr_status,
            "MaxDrawdownPct": drawdown["max_pct"], "MaxDrawdownAmount": drawdown["max_amount"],
            "CurrentDrawdownPct": drawdown["current_pct"], "DrawdownStatus": drawdown["status"],
            "MaxDrawdownPeak": drawdown["peak"], "MaxDrawdownTrough": drawdown["trough"],
            "MaxDrawdownRecovery": drawdown["recovery"],
            "MaxDrawdownDurationPeriods": drawdown["duration_periods"],
            "MaxDrawdownDurationDays": drawdown["duration_days"],
            "MaxDrawdownRecoveryPeriods": drawdown["recovery_periods"],
            "MaxDrawdownRecoveryDays": drawdown["recovery_days"],
            "MaxDrawdownOpen": drawdown["is_open"], "UnderwaterRatio": drawdown["underwater_ratio"],
            "AvgMonthlyReturn": float(monthlies.mean()) if not monthlies.empty else None,
            "MonthlyReturnStatus": "ok" if not monthlies.empty else "insufficient",
            "MonthlyReturnSamples": int(len(monthlies)), "SharpeRatio": sharpe["value"],
            "SharpeStatus": sharpe["status"], "SharpeSamples": sharpe["sample_size"],
            "PeriodsPerYear": annualisation, "EndEquity": end,
            "TotalReturn": None if start == 0 else end / start - 1,
            "MetricsFormulaVersion": METRICS_FORMULA_VERSION}


def calculate_profit_factor(pnls: Iterable[float], minimum_samples: int = 30,
                            confidence: float = 0.95) -> Dict[str, Any]:
    values = np.asarray(list(pnls), dtype=float)
    values = values[np.isfinite(values)]
    wins, losses, flat = values[values > 0], values[values < 0], values[values == 0]
    base = {"sample_size": int(len(values)), "win_count": int(len(wins)),
            "loss_count": int(len(losses)), "breakeven_count": int(len(flat)),
            "lower": None, "upper": None}
    if len(values) == 0:
        return {"value": None, "status": "insufficient", **base}
    gross_loss = float(abs(losses.sum()))
    if len(losses) == 0 or np.isclose(gross_loss, 0.0):
        return {"value": None, "status": "undefined", **base}
    lower, upper = _profit_factor_interval(values, confidence)
    return {"value": float(wins.sum() / gross_loss),
            "status": "ok" if len(values) >= minimum_samples else "insufficient",
            **base, "lower": lower, "upper": upper}


class Metrics:
    infer_periods_per_year = staticmethod(infer_periods_per_year)
    monthly_returns = staticmethod(monthly_returns)
    calculate_sharpe = staticmethod(calculate_sharpe)
    calculate_drawdown = staticmethod(calculate_drawdown)
    calculate_equity_metrics = staticmethod(calculate_equity_metrics)
    calculate_profit_factor = staticmethod(calculate_profit_factor)


def _clean_equity(equity: pd.Series) -> pd.Series:
    clean = pd.Series(equity, copy=True).replace([np.inf, -np.inf], np.nan).dropna()
    if isinstance(clean.index, pd.DatetimeIndex):
        clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    return clean.astype(float)


def _result(value: Optional[float], status: str, sample_size: int) -> Dict[str, Any]:
    return {"value": value, "status": status, "sample_size": sample_size}


def _elapsed_days(start: Any, end: Any) -> Optional[float]:
    if not isinstance(start, pd.Timestamp) or not isinstance(end, pd.Timestamp):
        return None
    return float((end - start).total_seconds() / 86_400.0)


def _profit_factor_interval(values: np.ndarray, confidence: float):
    if len(values) < 2 or not 0 < confidence < 1:
        return None, None
    samples = np.random.default_rng(42).choice(values, size=(2000, len(values)), replace=True)
    profits = np.where(samples > 0, samples, 0.0).sum(axis=1)
    losses = np.abs(np.where(samples < 0, samples, 0.0).sum(axis=1))
    finite = profits[losses > 0] / losses[losses > 0]
    if len(finite) == 0:
        return None, None
    alpha = (1 - confidence) / 2
    return float(np.quantile(finite, alpha)), float(np.quantile(finite, 1 - alpha))
