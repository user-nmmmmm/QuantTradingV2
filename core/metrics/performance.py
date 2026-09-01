"""Pure equity-curve performance metrics: Sharpe, drawdown, CAGR, exposure.

Split out of core/metrics.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations
from typing import Any, Dict, Mapping, Optional
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


def calculate_drawdown_events(
    equity: pd.Series, min_depth_pct: float = 0.0
) -> list[Dict[str, Any]]:
    """Enumerate every peak-to-recovery drawdown episode (BM1).

    ``calculate_drawdown`` reports only the single worst episode; this
    enumerates all of them so each can be located and reasoned about
    independently. An episode starts the instant equity first falls below a
    prior peak and ends when equity recovers back to that peak (or remains
    open at the series end). ``min_depth_pct`` filters out episodes shallower
    than the given fraction (e.g. 0.01 drops sub-1% noise); it compares
    against the unsigned depth.
    """
    clean = _clean_equity(equity)
    if len(clean) < 2:
        return []
    values = clean.to_numpy()
    rolling_peak = clean.cummax().to_numpy()
    underwater = values < rolling_peak
    index = clean.index
    n = len(clean)
    events: list[Dict[str, Any]] = []
    i = 0
    while i < n:
        if not underwater[i]:
            i += 1
            continue
        peak_pos = i - 1  # underwater[0] is always False (nothing precedes it).
        peak_value = float(values[peak_pos])
        trough_pos = i
        j = i
        while j < n and values[j] < peak_value:
            if values[j] < values[trough_pos]:
                trough_pos = j
            j += 1
        recovery_pos = j if j < n else None
        end_pos = recovery_pos if recovery_pos is not None else n - 1
        depth_amount = float(values[trough_pos] - peak_value)
        depth_pct = depth_amount / peak_value if peak_value else None
        events.append({
            "peak": index[peak_pos], "peak_value": peak_value,
            "trough": index[trough_pos], "trough_value": float(values[trough_pos]),
            "recovery": index[recovery_pos] if recovery_pos is not None else None,
            "depth_pct": depth_pct, "depth_amount": depth_amount,
            "duration_periods": end_pos - peak_pos,
            "duration_days": _elapsed_days(index[peak_pos], index[end_pos]),
            "recovery_periods": (
                recovery_pos - trough_pos if recovery_pos is not None else None
            ),
            "recovery_days": (
                _elapsed_days(index[trough_pos], index[recovery_pos])
                if recovery_pos is not None else None
            ),
            "is_open": recovery_pos is None,
        })
        i = recovery_pos if recovery_pos is not None else n
    if min_depth_pct > 0:
        events = [
            event for event in events
            if event["depth_pct"] is not None and abs(event["depth_pct"]) >= min_depth_pct
        ]
    return events


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


def calculate_exposure(
    positions_by_time: Mapping[Any, Mapping[str, float]],
    prices_by_time: Mapping[Any, Mapping[str, float]],
    equity_by_time: Optional[Mapping[Any, float]] = None,
) -> pd.DataFrame:
    """Gross/net notional exposure per timestamp across symbols (BM3).

    ``positions_by_time`` and ``prices_by_time`` are aligned by timestamp key
    (e.g. a portfolio's position snapshot and the matching close prices at
    each bar). Flat positions (qty == 0) never contribute. A symbol with a
    position but no matching price at that timestamp is excluded from that
    timestamp's totals rather than raising or being treated as zero-value —
    a stale/missing price is common in live data and must not silently
    understate exposure or crash the whole calculation; ``priced_symbols``
    reports how many symbols actually contributed so a caller can tell an
    empty book apart from an unpriced one.
    """
    rows = []
    for timestamp, positions in positions_by_time.items():
        prices = prices_by_time.get(timestamp, {})
        gross = 0.0
        net = 0.0
        priced_symbols = 0
        for symbol, qty in positions.items():
            if not qty:
                continue
            price = prices.get(symbol)
            if price is None:
                continue
            notional = float(qty) * float(price)
            gross += abs(notional)
            net += notional
            priced_symbols += 1
        equity = None if equity_by_time is None else equity_by_time.get(timestamp)
        rows.append({
            "timestamp": timestamp, "gross_exposure": gross, "net_exposure": net,
            "priced_symbols": priced_symbols,
            "gross_exposure_pct_equity": gross / equity if equity else None,
            "net_exposure_pct_equity": net / equity if equity else None,
        })
    frame = pd.DataFrame(
        rows, columns=["timestamp", "gross_exposure", "net_exposure",
                       "priced_symbols", "gross_exposure_pct_equity", "net_exposure_pct_equity"],
    )
    if not frame.empty:
        frame = frame.set_index("timestamp").sort_index()
    return frame


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
