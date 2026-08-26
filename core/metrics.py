"""Pure, side-effect-free backtest metric calculations."""
from __future__ import annotations
from typing import Any, Dict, Iterable, Mapping, Optional
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


def calculate_trade_quality(
    trades: Iterable[Mapping[str, Any]],
    minimum_samples: int = 30,
    confidence: float = 0.95,
) -> Dict[str, Any]:
    """Per-closed-trade win/loss, holding-duration, and PF statistics (BM2).

    Each record must provide ``net_pnl``. ``entry_time``/``exit_time`` (any
    value ``pd.Timestamp`` can parse) enable holding-duration stats;
    ``strategy``/``symbol`` enable the grouped breakdowns. Missing optional
    fields degrade that specific section to "insufficient" rather than
    raising, so this tolerates the partial records different execution
    venues happen to have on hand.
    """
    records = list(trades)
    if not records:
        return {
            "sample_size": 0, "status": "insufficient", "win_count": 0,
            "loss_count": 0, "breakeven_count": 0, "win_rate": None,
            "avg_win": None, "avg_loss": None, "expectancy": None,
            "profit_factor": None, "profit_factor_status": "insufficient",
            "holding_duration_hours": _EMPTY_DURATION, "by_strategy": {}, "by_symbol": {},
        }

    pnls = np.array([float(t["net_pnl"]) for t in records], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    win_rate = float(len(wins) / len(pnls))
    loss_rate = float(len(losses) / len(pnls))
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    expectancy = win_rate * (avg_win or 0.0) + loss_rate * (avg_loss or 0.0)
    pf = calculate_profit_factor(pnls, minimum_samples=minimum_samples, confidence=confidence)

    return {
        "sample_size": len(records),
        "status": "ok" if len(records) >= minimum_samples else "insufficient",
        "win_count": int(len(wins)), "loss_count": int(len(losses)),
        "breakeven_count": int(len(pnls) - len(wins) - len(losses)),
        "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": float(expectancy),
        "profit_factor": pf["value"], "profit_factor_status": pf["status"],
        "holding_duration_hours": _holding_duration_hours(records),
        "by_strategy": _trade_quality_breakdown(records, "strategy", minimum_samples, confidence),
        "by_symbol": _trade_quality_breakdown(records, "symbol", minimum_samples, confidence),
    }


_EMPTY_DURATION: Dict[str, Any] = {
    "status": "insufficient", "sample_size": 0,
    "mean": None, "median": None, "min": None, "max": None,
}


def _holding_duration_hours(records: list[Mapping[str, Any]]) -> Dict[str, Any]:
    hours = []
    for trade in records:
        entry, exit_ = trade.get("entry_time"), trade.get("exit_time")
        if entry is None or exit_ is None:
            continue
        entry_ts, exit_ts = pd.Timestamp(entry), pd.Timestamp(exit_)
        if pd.isna(entry_ts) or pd.isna(exit_ts):
            continue
        hours.append((exit_ts - entry_ts).total_seconds() / 3600.0)
    if not hours:
        return dict(_EMPTY_DURATION)
    values = np.array(hours, dtype=float)
    return {"status": "ok", "sample_size": int(len(values)),
            "mean": float(values.mean()), "median": float(np.median(values)),
            "min": float(values.min()), "max": float(values.max())}


def _trade_quality_breakdown(
    records: list[Mapping[str, Any]], key: str, minimum_samples: int, confidence: float,
) -> Dict[str, Any]:
    groups: Dict[Any, list] = {}
    for trade in records:
        label = trade.get(key)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(trade["net_pnl"]))
    breakdown = {}
    for label, pnls in groups.items():
        wins = [pnl for pnl in pnls if pnl > 0]
        pf = calculate_profit_factor(pnls, minimum_samples=minimum_samples, confidence=confidence)
        breakdown[str(label)] = {
            "sample_size": len(pnls),
            "win_rate": float(len(wins) / len(pnls)),
            "net_pnl": float(sum(pnls)),
            "profit_factor": pf["value"], "profit_factor_status": pf["status"],
        }
    return breakdown


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


_FUNNEL_STAGES = ("risk_evaluated", "risk_approved", "order_created", "order_accepted", "filled")
_ORDER_ACCEPTED_STATUSES = {"accepted", "partially_filled", "filled"}


def calculate_signal_funnel(events: Iterable[Any]) -> Dict[str, Any]:
    """Stage-by-stage conversion counts across the signal-to-fill chain (BM3).

    Groups events by ``correlation_id`` — the deterministic ID every
    downstream event in a signal's chain shares (P1.1.5) — and classifies
    the stages each correlation group reached: risk evaluated -> risk
    approved -> order created -> order accepted by the venue -> filled. Each
    stage is counted independently (a group counts at every stage it
    reached), so this is a true funnel where every count is <= the one
    before it.

    Events are duck-typed (``correlation_id``/``event_type``/``payload``
    attributes), so this accepts ``EventEnvelope`` instances or any
    equivalent lightweight record without importing the events module.

    Note: as of this writing, ``RiskDecision`` is a domain object but is not
    yet published as a ``risk_decision`` event by the live/backtest
    pipelines, so ``risk_evaluated``/``risk_approved`` will read 0 against a
    real run's event log until that publishing is wired up. This function
    only counts what it is given; it does not assume unpublished events.
    """
    groups: Dict[Any, Dict[str, bool]] = {}
    for event in events:
        key = getattr(event, "correlation_id", None)
        if key is None:
            continue
        group = groups.setdefault(key, {stage: False for stage in _FUNNEL_STAGES})
        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", None) or {}
        if event_type == "risk_decision":
            group["risk_evaluated"] = True
            if bool(payload.get("approved")):
                group["risk_approved"] = True
        elif event_type == "order_intent":
            group["order_created"] = True
        elif event_type == "order":
            if str(payload.get("status", "")).lower() in _ORDER_ACCEPTED_STATUSES:
                group["order_accepted"] = True
        elif event_type == "fill":
            group["filled"] = True

    total = len(groups)
    counts = {
        stage: sum(1 for group in groups.values() if group[stage])
        for stage in _FUNNEL_STAGES
    }
    stages: Dict[str, Any] = {}
    prior_count = None
    for stage in _FUNNEL_STAGES:
        stages[stage] = {
            "count": counts[stage],
            "pct_of_total": counts[stage] / total if total else None,
            "pct_of_previous_stage": (
                counts[stage] / prior_count if prior_count else None
            ),
        }
        prior_count = counts[stage]
    return {"total_correlation_chains": total, "stages": stages}


def calculate_cost_sensitivity(
    trades: Iterable[Mapping[str, Any]],
    commission_multipliers: Iterable[float] = (0.5, 1.0, 1.5, 2.0),
    slippage_multipliers: Iterable[float] = (0.5, 1.0, 1.5, 2.0),
) -> Dict[str, Any]:
    """Net-PnL sensitivity to commission/slippage assumptions (BM4).

    Each trade must provide ``gross_pnl_theoretical`` (the zero-cost PnL
    computed from ``theoretical_price``, i.e. before any slippage was
    applied to the fill — see the T-1.6 cost-field contract on
    ``CostBreakdown``), plus ``commission``/``slippage`` (missing ones
    default to 0.0). Trades recorded before ``gross_pnl_theoretical``
    existed fall back to ``gross_pnl``, which already has slippage baked
    into the fill price (T-1.7 fix for I-25: using ``gross_pnl`` — not
    ``gross_pnl_theoretical`` — as the sensitivity base double-counts
    slippage, since that fill-price-derived PnL already reflects it).

    The realized order flow — fill prices and quantities — is held fixed;
    this rescales the recorded cost components by each multiplier rather
    than re-simulating execution, so it is a first-order sensitivity, not
    a new backtest. Net PnL under a multiplier is
    ``gross_pnl_theoretical - commission*commission_multiplier -
    slippage*slippage_multiplier``: by construction this is monotonically
    non-increasing as either multiplier grows, so a grid point with higher
    net PnL than a lower-multiplier point indicates bad input data, not a
    real cost benefit. At commission_multiplier=1.0/slippage_multiplier=1.0
    ``baseline_net_pnl`` must equal the main report's NetPnL exactly (both
    reduce to ``gross_pnl - commission``), which is the acceptance test for
    the I-25 fix.

    Costs such as funding/borrow fees or market impact beyond the recorded
    slippage are not modeled here — this only scales the two cost fields
    it is given.
    """
    records = list(trades)
    if not records:
        return {"status": "insufficient", "sample_size": 0, "grid": []}

    gross_theoretical = float(
        sum(float(t.get("gross_pnl_theoretical", t.get("gross_pnl", 0.0))) for t in records)
    )
    total_commission = float(sum(float(t.get("commission", 0.0)) for t in records))
    total_slippage = float(sum(float(t.get("slippage", 0.0)) for t in records))

    grid = []
    for c_mult in commission_multipliers:
        for s_mult in slippage_multipliers:
            net = gross_theoretical - total_commission * c_mult - total_slippage * s_mult
            grid.append({
                "commission_multiplier": float(c_mult),
                "slippage_multiplier": float(s_mult),
                "net_pnl": float(net),
            })
    return {
        "status": "ok", "sample_size": len(records), "gross_pnl": gross_theoretical,
        "baseline_commission": total_commission, "baseline_slippage": total_slippage,
        "baseline_net_pnl": gross_theoretical - total_commission - total_slippage,
        "grid": grid,
        "unmodeled_note": (
            "commission and slippage only; funding/borrow fees and market "
            "impact beyond recorded slippage are not modeled"
        ),
    }


def calculate_attribution(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return-contribution breakdown by strategy, symbol, and month (BM5).

    Requires ``net_pnl`` on every trade; ``strategy``/``symbol`` missing on
    a trade group it under ``"UNKNOWN"`` rather than dropping it, and month
    is derived from ``exit_time`` (``"UNKNOWN"`` if absent/unparseable). A
    partition never loses or double-counts a trade, so each breakdown's
    values always sum back to ``total_net_pnl`` exactly.
    """
    records = list(trades)
    total = float(sum(float(t.get("net_pnl", 0.0)) for t in records))

    def _group_by(key_fn) -> Dict[str, float]:
        groups: Dict[str, float] = {}
        for trade in records:
            key = key_fn(trade)
            groups[key] = groups.get(key, 0.0) + float(trade.get("net_pnl", 0.0))
        return groups

    def _month_key(trade: Mapping[str, Any]) -> str:
        exit_time = trade.get("exit_time")
        if exit_time is None:
            return "UNKNOWN"
        timestamp = pd.Timestamp(exit_time)
        return "UNKNOWN" if pd.isna(timestamp) else timestamp.strftime("%Y-%m")

    return {
        "sample_size": len(records),
        "total_net_pnl": total,
        "by_strategy": _group_by(lambda t: str(t.get("strategy") or "UNKNOWN")),
        "by_symbol": _group_by(lambda t: str(t.get("symbol") or "UNKNOWN")),
        "by_month": _group_by(_month_key),
    }


def calculate_benchmark_comparison(equity: pd.Series, benchmark: pd.Series) -> Dict[str, Any]:
    """Strategy vs. benchmark total return over their overlapping index (BM6).

    Aligns on the intersection of both indices (inner join); periods where
    either series is missing are dropped rather than filled, since filling
    would fabricate a return that never happened.
    """
    strategy = _clean_equity(equity)
    bench = _clean_equity(benchmark)
    common_index = strategy.index.intersection(bench.index).sort_values()
    if len(common_index) < 2:
        return {"status": "insufficient", "sample_size": int(len(common_index)),
                "strategy_return": None, "benchmark_return": None,
                "excess_return": None, "correlation": None}
    strategy = strategy.loc[common_index]
    bench = bench.loc[common_index]
    strategy_returns = strategy.pct_change(fill_method=None).dropna()
    bench_returns = bench.pct_change(fill_method=None).dropna()
    return_index = strategy_returns.index.intersection(bench_returns.index)
    strategy_return = float(strategy.iloc[-1] / strategy.iloc[0] - 1)
    benchmark_return = float(bench.iloc[-1] / bench.iloc[0] - 1)
    correlation = (
        float(strategy_returns.loc[return_index].corr(bench_returns.loc[return_index]))
        if len(return_index) >= 2 else None
    )
    return {
        "status": "ok", "sample_size": int(len(common_index)),
        "strategy_return": strategy_return, "benchmark_return": benchmark_return,
        "excess_return": strategy_return - benchmark_return, "correlation": correlation,
    }


def calculate_rolling_returns(equity: pd.Series, window: int) -> pd.Series:
    """Trailing (never forward-looking) rolling total return over ``window`` periods (BM6)."""
    if window < 1:
        raise ValueError("window must be at least 1")
    clean = _clean_equity(equity)
    if len(clean) <= window:
        return pd.Series(dtype=float, name="rolling_return")
    result = (clean / clean.shift(window) - 1).dropna()
    result.name = "rolling_return"
    return result


def calculate_segment_returns(equity: pd.Series, segments: int) -> list[Dict[str, Any]]:
    """Split the equity curve into ``segments`` contiguous, non-overlapping chunks (BM6).

    Boundaries are index positions, not calendar-aware, so this is a coarse
    "did performance hold up across equal-sized chunks of the sample"
    check, not a calendar-period breakdown (use resampling for that).
    """
    if segments < 1:
        raise ValueError("segments must be at least 1")
    clean = _clean_equity(equity)
    n = len(clean)
    if n < segments + 1:
        return []
    boundaries = np.linspace(0, n - 1, segments + 1).astype(int)
    results = []
    for i in range(segments):
        start_pos, end_pos = int(boundaries[i]), int(boundaries[i + 1])
        if start_pos == end_pos:
            continue
        start_value, end_value = float(clean.iloc[start_pos]), float(clean.iloc[end_pos])
        results.append({
            "segment": i + 1, "start": clean.index[start_pos], "end": clean.index[end_pos],
            "return": (end_value / start_value - 1) if start_value else None,
            "sample_size": end_pos - start_pos + 1,
        })
    return results


def calculate_r_multiple_stats(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """R-Multiple distribution, System Quality Number, and MAE/MFE summary (BM7).

    A trade's R-multiple is ``net_pnl / initial_risk``, where
    ``initial_risk`` is the caller-supplied $ amount risked at entry (e.g.
    qty * |entry_price - stop_price|). Trades missing ``initial_risk`` (or
    with a non-positive one) cannot have an R-multiple and are excluded
    from that statistic — reported via ``excluded_no_initial_risk`` rather
    than silently dropped or coerced to R=0. ``mae``/``mfe`` (maximum
    adverse/favorable excursion) are optional per-trade fields this only
    summarizes; it does not derive them from a price path, since that needs
    bar-by-bar prices during the holding period that closed-trade records
    don't carry here.
    """
    records = list(trades)
    r_multiples = []
    excluded = 0
    for trade in records:
        risk = trade.get("initial_risk")
        if risk is None or float(risk) <= 0:
            excluded += 1
            continue
        r_multiples.append(float(trade["net_pnl"]) / float(risk))
    r_values = np.array(r_multiples, dtype=float)

    if len(r_values) < 2:
        r_stats: Dict[str, Any] = {
            "status": "insufficient", "sample_size": int(len(r_values)),
            "mean_r": float(r_values.mean()) if len(r_values) else None,
            "std_r": None, "sqn": None,
        }
    else:
        std_r = float(r_values.std(ddof=1))
        mean_r = float(r_values.mean())
        sqn = None if np.isclose(std_r, 0.0) else float(np.sqrt(len(r_values)) * mean_r / std_r)
        r_stats = {"status": "ok", "sample_size": int(len(r_values)),
                   "mean_r": mean_r, "std_r": std_r, "sqn": sqn}

    def _summary(values: list[float]) -> Dict[str, Any]:
        if not values:
            return {"status": "insufficient", "sample_size": 0, "mean": None, "median": None}
        arr = np.array(values, dtype=float)
        return {"status": "ok", "sample_size": int(len(arr)),
                "mean": float(arr.mean()), "median": float(np.median(arr))}

    return {
        "sample_size": len(records), "excluded_no_initial_risk": excluded,
        "r_multiple": r_stats,
        "mae": _summary([float(t["mae"]) for t in records if t.get("mae") is not None]),
        "mfe": _summary([float(t["mfe"]) for t in records if t.get("mfe") is not None]),
    }


def train_test_split_returns(returns: pd.Series, train_fraction: float = 0.7) -> Dict[str, pd.Series]:
    """Chronological (never shuffled) split of a return series into train/test (BM8).

    ``train_fraction`` is applied by position, not by calendar date. A
    random/shuffled split would leak future information into "train" and
    defeat the point of an out-of-sample test.
    """
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1 exclusive")
    clean = pd.Series(returns).dropna()
    split_pos = int(len(clean) * train_fraction)
    return {"train": clean.iloc[:split_pos], "test": clean.iloc[split_pos:]}


def walk_forward_windows(
    n_periods: int, train_size: int, test_size: int, step: Optional[int] = None,
) -> list[Dict[str, int]]:
    """Chronological, non-anticipating walk-forward window boundaries (BM8).

    Returns index-position boundaries only (train_start/train_end/
    test_start/test_end); the caller applies them to whatever series it is
    validating. Every test window starts exactly where its train window
    ends — there is no gap and no overlap between a window's train and
    test portions — and ``step`` (default ``test_size``, i.e. non-
    overlapping test windows) controls how far the next window slides.
    """
    if train_size < 1 or test_size < 1:
        raise ValueError("train_size and test_size must be at least 1")
    step = test_size if step is None else step
    if step < 1:
        raise ValueError("step must be at least 1")
    windows = []
    train_start = 0
    while train_start + train_size + test_size <= n_periods:
        train_end = train_start + train_size
        test_end = train_end + test_size
        windows.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": train_end, "test_end": test_end,
        })
        train_start += step
    return windows


def bootstrap_return_distribution(
    returns: Iterable[float], statistic: str = "mean",
    n_samples: int = 2000, confidence: float = 0.95, seed: int = 42,
) -> Dict[str, Any]:
    """Bootstrap confidence interval for a return-series statistic (BM8).

    i.i.d. resampling with replacement — this does not model serial
    correlation, so treat the interval as a lower bound on true uncertainty
    for autocorrelated return series, not an exact one. ``statistic`` is
    ``"mean"`` or ``"sharpe"`` (per-resample mean/std, unannualized —
    annualize the bounds yourself with the correct periods_per_year if
    comparing to an annualized Sharpe elsewhere). ``seed`` is fixed for
    reproducibility, matching this module's existing profit-factor
    bootstrap.
    """
    if statistic not in {"mean", "sharpe"}:
        raise ValueError("statistic must be 'mean' or 'sharpe'")
    values = np.asarray(list(returns), dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2 or not 0 < confidence < 1:
        return {"status": "insufficient", "sample_size": int(len(values)),
                "value": None, "lower": None, "upper": None}
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(n_samples, len(values)), replace=True)
    if statistic == "mean":
        stat_values = samples.mean(axis=1)
        point: Optional[float] = float(values.mean())
    else:
        means = samples.mean(axis=1)
        stds = samples.std(axis=1, ddof=1)
        stat_values = np.divide(
            means, stds, out=np.full_like(means, np.nan), where=stds > 0,
        )
        stat_values = stat_values[np.isfinite(stat_values)]
        point_std = float(values.std(ddof=1))
        point = float(values.mean() / point_std) if point_std > 0 else None
    if len(stat_values) == 0:
        return {"status": "undefined", "sample_size": int(len(values)),
                "value": point, "lower": None, "upper": None}
    alpha = (1 - confidence) / 2
    return {"status": "ok", "sample_size": int(len(values)), "value": point,
            "lower": float(np.quantile(stat_values, alpha)),
            "upper": float(np.quantile(stat_values, 1 - alpha))}


def monte_carlo_trade_sequence(
    trade_pnls: Iterable[float], n_simulations: int = 2000, seed: int = 42,
) -> Dict[str, Any]:
    """Monte Carlo reordering of realized trade PnLs to estimate sequence risk (BM8).

    Shuffles the SAME realized trade outcomes (sampling without
    replacement, i.e. permutation) to build a distribution of cumulative-
    PnL paths, then reports the distribution of final PnL and of maximum
    drawdown across simulated orderings. This tests sequence risk given
    trades that already happened — it invents no new trade outcomes, so it
    says nothing about whether these trades would repeat.
    """
    values = np.asarray(list(trade_pnls), dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return {"status": "insufficient", "sample_size": int(len(values))}
    rng = np.random.default_rng(seed)
    final_pnls = np.empty(n_simulations)
    max_drawdowns = np.empty(n_simulations)
    for i in range(n_simulations):
        cumulative = np.cumsum(rng.permutation(values))
        final_pnls[i] = cumulative[-1]
        running_peak = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))[1:]
        max_drawdowns[i] = (cumulative - running_peak).min()
    return {
        "status": "ok", "sample_size": int(len(values)), "n_simulations": n_simulations,
        "realized_final_pnl": float(values.sum()),
        "final_pnl_mean": float(final_pnls.mean()),
        "final_pnl_p05": float(np.quantile(final_pnls, 0.05)),
        "final_pnl_p95": float(np.quantile(final_pnls, 0.95)),
        "max_drawdown_mean": float(max_drawdowns.mean()),
        "max_drawdown_p05": float(np.quantile(max_drawdowns, 0.05)),
    }


def benjamini_hochberg(p_values: Iterable[float], fdr: float = 0.05) -> Dict[str, Any]:
    """Benjamini-Hochberg FDR correction across multiple hypothesis tests (BM8).

    Given raw p-values from independently tested hypotheses (e.g. one per
    strategy variant tried), returns each hypothesis's FDR-adjusted p-value
    and whether it survives the threshold. This is what makes "we tried N
    variants and picked the best" defensible: without a multiple-testing
    correction, the best of N random strategies looks significant purely
    from search breadth, not genuine edge.
    """
    if not 0 < fdr < 1:
        raise ValueError("fdr must be between 0 and 1 exclusive")
    values = np.asarray(list(p_values), dtype=float)
    n = len(values)
    if n == 0:
        return {"status": "insufficient", "sample_size": 0,
                "adjusted_p_values": [], "rejected": [], "rejected_count": 0}
    order = np.argsort(values)
    ranked = values[order]
    adjusted_ranked = ranked * n / (np.arange(n) + 1)
    # Standard BH step-up: enforce monotonicity from the largest p-value down.
    adjusted_ranked = np.minimum.accumulate(adjusted_ranked[::-1])[::-1]
    adjusted_ranked = np.clip(adjusted_ranked, 0.0, 1.0)
    adjusted = np.empty(n)
    adjusted[order] = adjusted_ranked
    rejected = [bool(p <= fdr) for p in adjusted]
    return {
        "status": "ok", "sample_size": n, "fdr": fdr,
        "adjusted_p_values": [float(p) for p in adjusted],
        "rejected": rejected, "rejected_count": int(sum(rejected)),
    }


class Metrics:
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
