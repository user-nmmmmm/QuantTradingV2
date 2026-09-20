"""After-the-fact S2 diagnostics; no parameter selection or trading dependency.

Forward labels are supplied by an independent, frozen labeling process. This
module checks their observation/availability boundaries, not vendor authenticity.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import pandas as pd

from core.universe import normalize_symbol


def _time(value):
    point = pd.Timestamp(value)
    if pd.isna(point):
        raise ValueError("timestamp is required")
    return point.tz_localize("UTC") if point.tzinfo is None else point.tz_convert("UTC")


def _finite(value, name, *, positive=False):
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return number


def _metric(value=None, *, status="insufficient", reason=None, samples=0, unit="ratio"):
    return {"value": value, "status": status, "reason": reason, "sample_size": samples, "unit": unit}


def _mean(values):
    scale = max(abs(value) for value in values) or 1.
    return math.fsum(value / scale for value in values) / len(values) * scale


@dataclass(frozen=True)
class ResearchObservation:
    symbol: str
    as_of: str
    score_available_at: str
    score: float | None
    eligible: bool
    target_weight: float
    horizon_hours: float
    mature_at: str
    label_available_at: str
    net_forward_return: float | None


@dataclass(frozen=True)
class ResearchFill:
    fill_id: str
    symbol: str
    executed_at: str
    side: str
    quantity: float
    price: float
    fee: float


@dataclass(frozen=True)
class ResearchEquity:
    observed_at: str
    equity: float


def evaluate_selection(*, observations: Iterable[ResearchObservation], split: str,
                       evaluated_at, window_start, window_end, quote_currency: str,
                       fills: Iterable[ResearchFill] = (),
                       equity_observations: Iterable[ResearchEquity] = (),
                       quantiles: int = 5, min_cross_section: int = 3) -> dict:
    """Evaluate fully matured cohorts, never rank/select candidate policies.

    Reporting windows are inclusive UTC intervals and must end by evaluated_at.
    Final samples must use the existing one-time adjudication entrypoint. This
    version rejects final entirely so it cannot become a second sample gate.
    """
    if split not in {"train", "validation", "retrospective", "final"}:
        raise ValueError("research split must be explicit")
    if split == "final":
        raise ValueError("final samples require the existing one-time adjudication entrypoint; this diagnostic rejects final")
    if type(quantiles) is not int or quantiles < 2:
        raise ValueError("quantiles must be at least 2")
    if type(min_cross_section) is not int or min_cross_section < 3:
        raise ValueError("min_cross_section must be at least 3")
    if not isinstance(quote_currency, str) or not quote_currency.strip():
        raise ValueError("quote_currency is required")
    evaluated, start, end = _time(evaluated_at), _time(window_start), _time(window_end)
    if not start <= end <= evaluated:
        raise ValueError("reporting window must be ordered and end by evaluated_at")
    cohorts, target_snapshots, identities = {}, {}, set()
    for item in observations:
        symbol, as_of = normalize_symbol(item.symbol), _time(item.as_of)
        score_available = _time(item.score_available_at)
        mature, label_available = _time(item.mature_at), _time(item.label_available_at)
        horizon = _finite(item.horizon_hours, "horizon_hours", positive=True)
        if score_available > as_of:
            raise ValueError("score was not available as of its decision")
        if mature < as_of + pd.Timedelta(hours=horizon) or label_available < mature:
            raise ValueError("label maturity/availability precedes its declared horizon")
        if as_of > evaluated:
            raise ValueError("cannot diagnose future decisions")
        if type(item.eligible) is not bool:
            raise ValueError("eligible must be boolean")
        weight = _finite(item.target_weight, "target_weight")
        if not 0 <= weight <= 1:
            raise ValueError("target weights must be long-only fractions")
        score = None if item.score is None else _finite(item.score, "score")
        if weight > 0 and (not item.eligible or score is None):
            raise ValueError("ineligible or unscored observations cannot carry target weight")
        identity = (as_of, horizon, symbol)
        if identity in identities:
            raise ValueError("duplicate normalized cohort member")
        identities.add(identity)
        if not start <= as_of <= end:
            continue
        snapshot = target_snapshots.setdefault(as_of, {})
        state = (weight, score, item.eligible)
        if symbol in snapshot and snapshot[symbol] != state:
            raise ValueError("horizon copies disagree on contemporaneous score/target")
        snapshot[symbol] = state
        ready = mature <= evaluated and label_available <= evaluated
        # Do not inspect a future label value, even if a caller supplied one.
        label = _finite(item.net_forward_return, "net_forward_return") if ready and item.net_forward_return is not None else None
        if label is not None and label < -1:
            raise ValueError("long-only simple net return cannot be less than -1")
        cohorts.setdefault((as_of, horizon), []).append(
            {"symbol": symbol, "score": score, "eligible": item.eligible,
             "ready": ready, "return": label})
    diagnostics, horizons = [], {}
    for (as_of, horizon), rows in sorted(cohorts.items()):
        if {row["symbol"] for row in rows} != set(target_snapshots[as_of]):
            raise ValueError("horizon cohorts must cover the same contemporaneous universe")
        eligible = [r for r in rows if r["eligible"] and r["score"] is not None]
        paired = [r for r in eligible if r["ready"] and r["return"] is not None]
        pending = any(not r["ready"] for r in eligible)
        if not eligible:
            reason = "no_eligible_scores"
        elif pending:
            reason = "cohort_labels_not_yet_mature_or_available"
        elif len(paired) != len(eligible):
            reason = "cohort_has_missing_mature_labels"
        elif len(paired) < min_cross_section:
            reason = "cross_section_too_small"
        else:
            reason = None
        metric_status = "pending" if pending else "insufficient"
        ic = _metric(status=metric_status, reason=reason, samples=len(paired), unit="spearman_correlation")
        bucket_returns = {str(q): _metric(reason=reason, samples=0, unit="simple_net_return")
                          for q in range(1, quantiles + 1)}
        if reason is None:
            scores = pd.Series([r["score"] for r in paired], dtype=float)
            returns = pd.Series([r["return"] for r in paired], dtype=float)
            score_ranks, return_ranks = scores.rank(method="average"), returns.rank(method="average")
            if scores.nunique() < 2 or returns.nunique() < 2:
                ic = _metric(reason="constant_scores_or_returns", samples=len(paired), unit="spearman_correlation")
            else:
                ic = _metric(float(score_ranks.corr(return_ranks)), status="ok", samples=len(paired),
                             unit="spearman_correlation")
            if len(paired) < quantiles:
                bucket_returns = {str(q): _metric(reason="fewer_members_than_quantiles", unit="simple_net_return")
                                  for q in range(1, quantiles + 1)}
            else:
                # Tied scores stay together; never manufacture a symbol-based alpha split.
                buckets = (scores.rank(method="average", pct=True) * quantiles).apply(math.ceil)
                for q in range(1, quantiles + 1):
                    values = returns[buckets == q]
                    bucket_returns[str(q)] = (_metric(_mean(values.tolist()), status="ok", samples=len(values),
                                                       unit="simple_net_return") if len(values) else
                                              _metric(reason="empty_quantile_due_to_ties", unit="simple_net_return"))
        top, bottom = bucket_returns[str(quantiles)], bucket_returns["1"]
        spread = (_metric(_finite(top["value"] - bottom["value"], "quantile spread"), status="ok", samples=min(top["sample_size"], bottom["sample_size"]),
                          unit="simple_net_return_difference") if top["status"] == bottom["status"] == "ok" else
                  _metric(status=metric_status, reason="top_or_bottom_unavailable", unit="simple_net_return_difference"))
        diagnostic = {"as_of": as_of.isoformat(), "horizon_hours": horizon,
                      "universe_members": len(rows), "eligible_scored_members": len(eligible),
                      "mature_paired_members": len(paired),
                      "coverage": _metric(len(eligible) / len(rows), status="ok", samples=len(rows), unit="fraction"),
                      "rank_ic": ic, "quantile_returns": bucket_returns,
                      "top_minus_bottom": spread}
        diagnostics.append(diagnostic)
        horizons.setdefault(horizon, []).append(diagnostic)
    decay = []
    for horizon, groups in sorted(horizons.items()):
        values = pd.Series([g["rank_ic"]["value"] for g in groups if g["rank_ic"]["status"] == "ok"], dtype=float)
        mean_ic = (_metric(float(values.mean()), status="ok", samples=len(values), unit="spearman_correlation")
                   if len(values) else _metric(reason="no_valid_cohort_ic", unit="spearman_correlation"))
        std = float(values.std(ddof=1)) if len(values) >= 2 else 0.
        ir = (_metric(float(values.mean() / std), status="ok", samples=len(values), unit="unannualized_ic_mean_over_sample_std")
              if len(values) >= 2 and std > 0 else
              _metric(reason="at_least_two_nonconstant_ic_observations_required", samples=len(values),
                      unit="unannualized_ic_mean_over_sample_std"))
        spreads = [g["top_minus_bottom"]["value"] for g in groups if g["top_minus_bottom"]["status"] == "ok"]
        decay.append({"horizon_hours": horizon, "cohort_count": len(groups), "mean_rank_ic": mean_ic, "ic_ir": ir,
                      "mean_top_minus_bottom": (_metric(_mean(spreads), status="ok", samples=len(spreads),
                                                         unit="simple_net_return_difference") if spreads else
                                                 _metric(reason="no_valid_quantile_spreads", unit="simple_net_return_difference"))})
    target_turnover, previous = 0., {}
    for _, snapshot in sorted(target_snapshots.items()):
        weights = {s: state[0] for s, state in snapshot.items()}
        if sum(weights.values()) > 1. + 1e-12:
            raise ValueError("target snapshot gross weight exceeds 1")
        target_turnover += sum(abs(weights.get(s, 0.) - previous.get(s, 0.)) for s in set(weights) | set(previous))
        previous = weights
    executed, fees, fill_ids, fill_count = 0., 0., set(), 0
    for fill in fills:
        if not fill.fill_id or fill.fill_id in fill_ids:
            raise ValueError("fill identities must be unique and nonempty")
        fill_ids.add(fill.fill_id)
        normalize_symbol(fill.symbol)
        if fill.side not in {"buy", "sell"}:
            raise ValueError("fill side must be buy or sell")
        point = _time(fill.executed_at)
        qty, price = _finite(fill.quantity, "fill quantity", positive=True), _finite(fill.price, "fill price", positive=True)
        fee = _finite(fill.fee, "fill fee")
        if start <= point <= end:
            executed = _finite(executed + qty * price, "executed notional")
            fees = _finite(fees + fee, "actual fees")
            fill_count += 1
    equities, equity_times = [], set()
    for item in equity_observations:
        point = _time(item.observed_at)
        if point in equity_times:
            raise ValueError("duplicate equity observation")
        equity_times.add(point)
        equity = _finite(item.equity, "equity", positive=True)
        if start <= point <= end:
            equities.append(equity)
    turnover = (_metric(_finite(executed / _mean(equities), "executed turnover"), status="ok", samples=fill_count,
                        unit="gross_two_way_executed_notional_over_mean_observed_equity") if equities else
                _metric(reason="no_equity_observations_in_window", samples=fill_count,
                        unit="gross_two_way_executed_notional_over_mean_observed_equity"))
    return {"schema_version": "s2-research-diagnostics/v1", "split": split,
            "evaluated_at": evaluated.isoformat(),
            "window": {"start_inclusive": start.isoformat(), "end_inclusive": end.isoformat()},
            "quote_currency": quote_currency.upper(), "cohorts": diagnostics, "decay": decay,
            "executed_turnover": turnover, "executed_notional": executed, "actual_fees": fees,
            "target_weight_turnover": _metric(target_turnover, status="ok" if target_snapshots else "insufficient",
                                               reason=None if target_snapshots else "no_target_snapshots", samples=len(target_snapshots),
                                               unit="sum_absolute_weight_changes_from_initial_cash_excluding_cash"),
            "model_selection_allowed": False, "research_effectiveness": "not_adjudicated",
            "formal_routing_enabled": False,
            "limitations": ["Upstream factors and net labels require independent PIT and source verification.",
                            "Window equity mean is observation-weighted, not time-weighted or annualized.",
                            "Incomplete label cohorts are excluded in full; no selective maturity ranking.",
                            "Final samples are rejected; use the existing one-time adjudication entrypoint."]}
