"""Return attribution, benchmark comparison, cost sensitivity, and signal funnel.

Split out of core/metrics.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations
from typing import Any, Dict, Iterable, Mapping
import numpy as np
import pandas as pd

from core.strategy_health import (
    CONTROLLER_ACCOUNT_RISK,
    CONTROLLER_STRATEGY,
    classify_exit_controller,
)

from core.metrics.performance import _clean_equity

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

    # SR3-3 (STR-P1-08): 76.6% of the frozen baseline's net profit came out of
    # DailyLossLimit exits, so a headline PF cannot be read as evidence about
    # the Donchian alpha. Splitting the same trades by the controller that
    # actually closed them gives the three views the roadmap requires -
    # alpha-only, risk-overlay, combined - and the split is a partition, so it
    # still sums to total_net_pnl exactly.
    by_controller = _group_by(
        lambda t: classify_exit_controller(t.get("exit_reason"))
    )
    alpha_only = by_controller.get(CONTROLLER_STRATEGY, 0.0)
    risk_overlay = by_controller.get(CONTROLLER_ACCOUNT_RISK, 0.0)
    other = total - alpha_only - risk_overlay
    return {
        "sample_size": len(records),
        "total_net_pnl": total,
        "by_strategy": _group_by(lambda t: str(t.get("strategy") or "UNKNOWN")),
        "by_symbol": _group_by(lambda t: str(t.get("symbol") or "UNKNOWN")),
        "by_month": _group_by(_month_key),
        "by_exit_controller": by_controller,
        "control_attribution": {
            "alpha_only": alpha_only,
            "risk_overlay": risk_overlay,
            "router_and_system": other,
            "combined": total,
            "risk_overlay_share": (
                risk_overlay / total if total else None
            ),
            "reconciles": abs(
                (alpha_only + risk_overlay + other) - total
            ) < 1e-6,
        },
        "trade_count_by_exit_controller": {
            controller: sum(
                1 for trade in records
                if classify_exit_controller(trade.get("exit_reason")) == controller
            )
            for controller in by_controller
        },
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
