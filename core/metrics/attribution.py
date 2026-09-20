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
_ORDER_ACCEPTED_STATUSES = {"accepted", "partially_filled", "filled", "partial"}


def _payload_field(payload: Any, name: str) -> Any:
    """Read one field from an event payload, mapping or object alike.

    Real pipeline payloads are dataclasses (``RiskDecision``, ``OrderEvent``,
    ``FillEvent``); serialised ones are plain dicts. Assuming only the mapping
    form made this function raise ``AttributeError`` on every real event log,
    which is part of why it had no production caller.
    """
    if isinstance(payload, Mapping):
        return payload.get(name)
    return getattr(payload, name, None)


def _status_text(value: Any) -> str:
    """Enum members compare by value here; ``str(SomeEnum.X)`` would not."""
    return str(getattr(value, "value", value)).lower()


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
    Payloads may be dataclasses or plain mappings; both are read the same way.

    Only entry chains with opening-risk evidence contribute to the funnel.
    Exit and unlinked chains remain visible as excluded diagnostics.
    """
    groups: Dict[Any, Dict[str, bool]] = {}
    exits = set()
    keyless = 0
    raw_signals = set()
    for event in events:
        key = getattr(event, "correlation_id", None)
        event_type = getattr(event, "event_type", None)
        # Signal envelopes can begin their own transport correlation. The
        # intent explicitly references event_id as signal_id; use that causal
        # identity to join the emitted signal to its downstream entry chain.
        if event_type == "signal" and getattr(event, "event_id", None) is not None:
            key = event.event_id
        if key is None:
            keyless += 1
            continue
        key = str(key)
        if event_type == "signal":
            raw_signals.add(key)
        group = groups.setdefault(key, {stage: False for stage in _FUNNEL_STAGES})
        payload = getattr(event, "payload", None)
        side = _status_text(_payload_field(payload, "action") or _payload_field(payload, "side"))
        if side in {"sell", "cover"} or _payload_field(payload, "reduce_only") is True:
            exits.add(key)
        if event_type == "risk_decision":
            group["risk_evaluated"] = True
            if bool(_payload_field(payload, "approved")):
                group["risk_approved"] = True
        elif event_type == "order_intent":
            group["order_created"] = True
        elif event_type == "order":
            if _status_text(_payload_field(payload, "status")) in _ORDER_ACCEPTED_STATUSES:
                group["order_accepted"] = True
        elif event_type == "fill":
            group["filled"] = True

    entry_groups = {key: group for key, group in groups.items()
                    if group["risk_evaluated"] and key not in exits}
    incomplete = 0
    for group in entry_groups.values():
        prior = True
        for stage in _FUNNEL_STAGES:
            if group[stage] and not prior:
                incomplete += 1
            group[stage] = bool(prior and group[stage])
            prior = group[stage]
    total = len(entry_groups)
    counts = {
        stage: sum(1 for group in entry_groups.values() if group[stage])
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
    return {"schema_version": "entry-funnel/v2", "total_correlation_chains": total,
            "raw_entry_signal_chains": len(raw_signals - exits),
            "stages": stages, "excluded_exit_chains": len(exits),
            "unclassified_chains": len(set(groups) - set(entry_groups) - exits),
            "keyless_events": keyless, "incomplete_entry_stages": incomplete}


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

    def reference_missing(value):
        return value is None or pd.isna(value)

    legacy_count = sum(reference_missing(t.get("gross_pnl_theoretical")) for t in records)
    try:
        for trade in records:
            reference = trade.get("gross_pnl_theoretical")
            gross = trade.get("gross_pnl") if reference_missing(reference) else reference
            costs = [float(trade.get(key, 0.0)) for key in ("commission", "slippage")]
            if gross is None or not np.isfinite(float(gross)) or any(not np.isfinite(v) or v < 0 for v in costs):
                raise ValueError("non-finite or missing gross PnL, or invalid recorded cost")
        commission_multipliers = tuple(float(v) for v in commission_multipliers)
        slippage_multipliers = tuple(float(v) for v in slippage_multipliers)
        if any(not np.isfinite(v) or v < 0 for v in (*commission_multipliers, *slippage_multipliers)):
            raise ValueError("invalid cost multiplier")
    except (TypeError, ValueError) as exc:
        return {"status": "invalid_input", "sample_size": len(records), "grid": [], "reason": str(exc)}
    # Legacy gross PnL already includes execution-price slippage. Only its
    # incremental multiplier is chargeable; this reference is not a claim
    # that a missing theoretical fill price was observed.
    gross_theoretical = float(sum(
        float(t["gross_pnl_theoretical"]) if not reference_missing(t.get("gross_pnl_theoretical"))
        else float(t.get("gross_pnl", 0.0)) + float(t.get("slippage", 0.0))
        for t in records
    ))
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
        "legacy_count": legacy_count,
        "cost_semantics": "mixed_or_legacy_incremental_slippage" if legacy_count else "theoretical_reference",
        "baseline_commission": total_commission, "baseline_slippage": total_slippage,
        "baseline_net_pnl": gross_theoretical - total_commission - total_slippage,
        "grid": grid,
        "unmodeled_note": (
            "commission and slippage only; funding/borrow fees and market "
            "impact beyond recorded slippage are not modeled"
        ),
    }


def calculate_group_drawdown_contribution(
    account_equity: pd.Series | None = None,
    group_equity: pd.DataFrame | None = None,
    group_cashflows: pd.DataFrame | None = None,
    external_cashflows: pd.Series | None = None,
    *,
    cashflow_timing: str = "end_of_interval",
) -> Dict[str, Any]:
    """Attribute one portfolio drawdown to simultaneous marked group paths.

    All amounts use the same account reporting currency. Group equity includes
    allocated cash, marked holdings and liabilities; groups must partition the
    entire account. ``group_cashflows[t]`` are net contributions during (t-1,t],
    including transfers between groups, and must sum to the independently
    recorded account ``external_cashflows[t]``. Opening flows must be zero.
    Explicit zero flows are required when no money moved. Only flows booked at
    the end of each interval are supported; intrainterval flows require finer
    marked observations and must not be approximated silently.

    For interval t, group return contribution is (G[t]-G[t-1]-F[t])/E[t-1].
    Its sum is the account flow-neutral return. Chain these returns into a NAV
    starting at 1. At the account's deepest peak/trough, each group's linked
    contribution is sum(NAV[t-1]/NAV[peak] * contribution[t]). Negative values
    deepen the drawdown; positive values offset it. They sum to NAV[trough] /
    NAV[peak] - 1, unlike the non-additive standalone group drawdowns.

    This is an ex-post report, never an input to a trading decision. Missing
    inputs remain null; clocks, coverage and sums are never repaired or filled.
    """
    base = {"schema_version": "group-drawdown-attribution/v1", "formula_version": "1.0",
            "value": None, "unit": "ratio", "sample_size": 0, "by_group": {}, "paths": [],
            "cashflow_timing": cashflow_timing,
            "policy": "linked marked-equity contributions at common account TWR peak/trough"}
    facts = (account_equity, group_equity, group_cashflows, external_cashflows)
    if any(value is None for value in facts):
        return {**base, "status": "not_modeled", "reason":
                "complete simultaneous account/group marked equity and explicit cashflow facts required"}
    if cashflow_timing != "end_of_interval":
        return {**base, "status": "not_modeled", "reason":
                "only explicit end_of_interval cashflows are supported"}

    def clean(value, expected_type):
        if not isinstance(value, expected_type) or not isinstance(value.index, pd.DatetimeIndex):
            raise ValueError("facts must be Series/DataFrame with a DatetimeIndex")
        result = value.copy(deep=True)
        if result.index.hasnans or not result.index.is_unique or not result.index.is_monotonic_increasing:
            raise ValueError("timestamps must be unique, ordered and nonmissing")
        result.index = (result.index.tz_localize("UTC") if result.index.tz is None
                        else result.index.tz_convert("UTC"))
        result = result.astype(float)
        if not np.isfinite(result.to_numpy()).all():
            raise ValueError("all marked values and cashflows must be finite")
        return result

    try:
        account = clean(account_equity, pd.Series)
        groups = clean(group_equity, pd.DataFrame)
        flows = clean(group_cashflows, pd.DataFrame)
        external = clean(external_cashflows, pd.Series)
        if not groups.columns.is_unique or any(not isinstance(c, str) or not c.strip() for c in groups.columns):
            raise ValueError("group identities must be unique nonempty strings")
        if not groups.columns.equals(flows.columns):
            raise ValueError("group equity and cashflow columns must match exactly")
        if any(not account.index.equals(value.index) for value in (groups, flows, external)):
            raise ValueError("all account and group facts must share the exact observation clock")
        if not np.allclose(groups.sum(axis=1), account, rtol=1e-10, atol=1e-8):
            raise ValueError("group marked equity does not reconcile to account equity")
        if not np.allclose(flows.sum(axis=1), external, rtol=1e-10, atol=1e-8):
            raise ValueError("group cashflows do not reconcile to external account cashflows")
        if len(account) and ((flows.iloc[0] != 0).any() or external.iloc[0] != 0):
            raise ValueError("opening cashflows must be zero; opening equity is the capital anchor")
        if (account <= 0).any() or ((account - external) <= 0).any():
            raise ValueError("account equity before and after interval-end flows must stay positive")
        if len(account) < 2 or not len(groups.columns):
            return {**base, "status": "insufficient_data", "sample_size": len(account),
                    "reason": "at least two complete marked observations and one group required"}
        contributions = (groups.diff() - flows).div(account.shift(), axis=0).iloc[1:]
        returns = contributions.sum(axis=1)
        nav = pd.Series(1.0, index=account.index)
        nav.iloc[1:] = (1 + returns).cumprod().to_numpy()
        if not np.isfinite(nav.to_numpy()).all() or (nav <= 0).any():
            raise ValueError("flow-neutral NAV must be finite and positive")
        drawdown = nav / nav.cummax() - 1
        trough_pos = int(np.argmin(drawdown.to_numpy()))
        peak_pos = int(np.argmax(nav.iloc[:trough_pos + 1].to_numpy()))
        linked = contributions.mul(nav.shift().iloc[1:], axis=0).iloc[peak_pos:trough_pos]
        linked = linked.sum(axis=0) / float(nav.iloc[peak_pos])
        value = float(drawdown.iloc[trough_pos])
        if not np.isfinite(linked.to_numpy()).all() or not np.isclose(linked.sum(), value, rtol=1e-10, atol=1e-10):
            raise ValueError("linked group contributions do not reconcile to portfolio drawdown")
    except (TypeError, ValueError, OverflowError) as exc:
        return {**base, "status": "invalid_input", "reason": str(exc)}
    return {**base, "status": "ok", "reason": None, "value": value,
            "sample_size": len(account), "peak": account.index[peak_pos].isoformat(),
            "trough": account.index[trough_pos].isoformat(), "reconciles": True,
            "by_group": {name: {"contribution": float(linked[name]), "unit": "ratio"}
                         for name in groups.columns},
            "paths": [{"timestamp": stamp.isoformat(), "account_equity": float(account.loc[stamp]),
                       "flow_neutral_nav": float(nav.loc[stamp]),
                       "external_cashflow": float(external.loc[stamp]),
                       "group_equity": {name: float(groups.loc[stamp, name]) for name in groups.columns},
                       "group_cashflows": {name: float(flows.loc[stamp, name]) for name in groups.columns}}
                      for stamp in account.index]}


def calculate_attribution(
    trades: Iterable[Mapping[str, Any]], *, account_equity=None, group_equity=None,
    group_cashflows=None, external_cashflows=None, cashflow_timing="end_of_interval",
) -> Dict[str, Any]:
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
        "by_direction": _group_by(lambda t: str(t.get("side") or t.get("direction") or "UNKNOWN")),
        "by_exit_reason": _group_by(lambda t: str(t.get("exit_reason") or "UNKNOWN")),
        "group_drawdown_contribution": calculate_group_drawdown_contribution(
            account_equity, group_equity, group_cashflows, external_cashflows,
            cashflow_timing=cashflow_timing),
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
    identity = dict(benchmark.attrs.get("benchmark") or {})
    identity_fields = {"benchmark_id": identity.get("benchmark_id"), "benchmark_policy": identity,
                       "identity_status": "ok" if identity.get("benchmark_id") else "not_modeled"}
    strategy = _clean_equity(equity)
    bench = _clean_equity(benchmark)
    common_index = strategy.index.intersection(bench.index).sort_values()
    if len(common_index) < 2:
        return {**identity_fields, "status": "insufficient", "sample_size": int(len(common_index)),
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
        if len(return_index) >= 2 and strategy_returns.std() > 0 and bench_returns.std() > 0 else None
    )
    return {
        **identity_fields, "status": "ok", "sample_size": int(len(common_index)),
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
