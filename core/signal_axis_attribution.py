"""Causal attribution from entry-time predictions, with bounded research weights.

Only previously frozen predictions are compared with subsequently available net
outcomes. Positive Spearman correlations are squared; nonpositive correlations
receive zero score, an intentional conservative departure from unconditional
rho-squared attribution. Weights are clipped, smoothed, then projected onto a
bounded simplex. These are research diagnostics and cannot authorise orders.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from numbers import Real
from typing import Mapping

import pandas as pd

from core.signal_observation_types import fingerprint
from core.timeframes import as_utc_timestamp, timeframe_delta


def _number(value):
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def _time(value, name):
    try:
        if value is None:
            raise ValueError(name)
        result = as_utc_timestamp(value)
        if pd.isna(result) or not math.isfinite(result.timestamp()):
            raise ValueError(name)
        return result
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite timestamp") from exc


def _weights(value, axes, *, floor=0.0, cap=1.0):
    if not isinstance(value, Mapping) or set(value) != set(axes):
        raise ValueError("weights must specify exactly the requested axes")
    result = {axis: _number(value[axis]) for axis in axes}
    if any(number is None or number <= 0 or number < floor - 1e-12 or number > cap + 1e-12
           for number in result.values()):
        raise ValueError("weights must be finite, positive, and within bounds")
    if not math.isclose(math.fsum(result.values()), 1.0, rel_tol=0, abs_tol=1e-12):
        raise ValueError("weights must sum to one")
    return result


def _project(values, floor, cap):
    """Euclidean projection, unlike clip/renormalise, preserves both bounds."""
    left = min(value - cap for value in values.values())
    right = max(value - floor for value in values.values())
    for _ in range(100):
        shift = (left + right) / 2
        mass = math.fsum(min(cap, max(floor, value - shift)) for value in values.values())
        if mass > 1:
            left = shift
        else:
            right = shift
    shift = (left + right) / 2
    result = {axis: min(cap, max(floor, value - shift)) for axis, value in values.items()}
    residual = 1.0 - math.fsum(result.values())
    for axis in result:
        adjustment = min(cap - result[axis], max(floor - result[axis], residual))
        result[axis] += adjustment
        residual -= adjustment
    return result


def _spearman(predictions, outcomes):
    x = pd.Series(predictions, dtype=float).rank(method="average").tolist()
    y = pd.Series(outcomes, dtype=float).rank(method="average").tolist()
    x_mean, y_mean = math.fsum(x) / len(x), math.fsum(y) / len(y)
    x = [value - x_mean for value in x]
    y = [value - y_mean for value in y]
    denominator = math.sqrt(math.fsum(value * value for value in x)
                            * math.fsum(value * value for value in y))
    if denominator == 0:
        return None
    return min(1.0, max(-1.0, math.fsum(a * b for a, b in zip(x, y)) / denominator))


def fit_axis_weights(records, axes, policy, *, cutoff, previous=None,
                     horizon_bars=1, timeframe="1d"):
    """Fit once at a cutoff using common, supported, matured frozen predictions.

    The caller partitions records by strategy, direction, horizon and snapshot
    identity. Source model versions may differ across older frozen folds, and
    remain visible in the audit. No prediction is reconstructed with today's
    ledger or regime model. Unknown/sparse predictions are not zero-imputed.
    """
    if isinstance(axes, str):
        raise ValueError("axes must be a sequence of unique nonempty names")
    axes = tuple(axes)
    if (not axes or any(not isinstance(axis, str) or not axis.strip() for axis in axes)
            or len(set(axes)) != len(axes)):
        raise ValueError("axes must be a sequence of unique nonempty names")
    axes = tuple(sorted(axes))
    floor, cap = policy.axis_weight_floor, policy.axis_weight_cap
    if len(axes) * floor > 1 + 1e-12 or len(axes) * cap < 1 - 1e-12:
        raise ValueError("axis count is incompatible with weight bounds")
    if type(horizon_bars) is not int or horizon_bars < 1:
        raise ValueError("horizon_bars must be a positive integer")
    horizon = horizon_bars * timeframe_delta(timeframe)
    cutoff = _time(cutoff, "cutoff")
    window_start = cutoff - pd.Timedelta(days=policy.attribution_window_days)
    uniform = dict.fromkeys(axes, 1.0 / len(axes))
    previous_weights = (_weights(previous, axes, floor=floor, cap=cap)
                        if previous is not None else uniform.copy())
    snapshots = {}
    skipped = Counter()
    duplicates = 0
    for row in records:
        if not isinstance(row, Mapping):
            raise ValueError("attribution records must be mappings")
        entry = _time(row.get("available_at"), "prediction available_at")
        # Filter before hashing or inspecting outcome/prediction values. Future
        # facts, including future versions, cannot change the fitted identity.
        if entry < window_start or entry >= cutoff:
            skipped["outside_entry_window"] += 1
            continue
        label = _time(row.get("label_available_at"), "label_available_at")
        if label >= cutoff:
            skipped["label_not_yet_available"] += 1
            continue
        if label < entry + horizon:
            skipped["premature_label"] += 1
            continue
        if (("horizon_bars" in row and row["horizon_bars"] != horizon_bars)
                or ("timeframe" in row and row["timeframe"] != timeframe)):
            raise ValueError("attribution record has a different horizon or timeframe")
        candidate_id = row.get("candidate_id")
        version = row.get("model_version")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("attribution candidate_id must be nonempty")
        if not isinstance(version, str) or not version:
            skipped["missing_source_model_version"] += 1
            continue
        outcome = _number(row.get("net_return_bps"))
        source_axes = row.get("axes")
        if outcome is None or not isinstance(source_axes, Mapping):
            skipped["nonfinite_or_missing_snapshot"] += 1
            continue
        estimates = {}
        for axis in axes:
            snapshot = source_axes.get(axis)
            if (not isinstance(snapshot, Mapping) or "support_reason" not in snapshot
                    or snapshot["support_reason"] is not None):
                break
            estimate = _number(snapshot.get("estimate_bps"))
            if estimate is None:
                break
            estimates[axis] = estimate
        if len(estimates) != len(axes):
            skipped["unsupported_or_nonfinite_axis"] += 1
            continue
        frozen = {"candidate_id": candidate_id, "available_at": entry.isoformat(),
                  "label_available_at": label.isoformat(), "net_return_bps": outcome,
                  "axes": estimates, "model_version": version}
        digest = fingerprint(frozen)
        if candidate_id in snapshots:
            if snapshots[candidate_id][1] != digest:
                raise ValueError("conflicting frozen attribution record for candidate_id")
            duplicates += 1
            continue
        snapshots[candidate_id] = (frozen, digest)
    ordered = sorted(snapshots.values(), key=lambda item: (item[0]["available_at"],
                                                          item[0]["candidate_id"]))
    eligible = [item[0] for item in ordered]
    sample_count = len(eligible)
    block_seconds = max(policy.ev_policy.block_days * 86400, horizon.total_seconds())
    blocks = Counter(math.floor(_time(row["available_at"], "entry").timestamp() / block_seconds)
                     for row in eligible)
    effective_blocks = (sample_count ** 2 / math.fsum(count ** 2 for count in blocks.values())
                        if blocks else 0.0)
    correlations = dict.fromkeys(axes)
    if sample_count >= 2:
        outcomes = [row["net_return_bps"] for row in eligible]
        correlations = {axis: _spearman([row["axes"][axis] for row in eligible], outcomes)
                        for axis in axes}
    raw_scores = {axis: rho ** 2 if rho is not None and rho > 0 else 0.0
                  for axis, rho in correlations.items()}
    clipped_scores = {axis: min(cap, max(floor, score)) for axis, score in raw_scores.items()}
    reason = None
    if sample_count < policy.attribution_min_samples:
        reason = "insufficient_samples"
    elif effective_blocks < policy.attribution_min_blocks:
        reason = "insufficient_blocks"
    elif not any(raw_scores.values()):
        reason = "no_positive_attribution"
    smoothed = {axis: ((1 - policy.attribution_ema_rate) * previous_weights[axis]
                       + policy.attribution_ema_rate * clipped_scores[axis]) for axis in axes}
    weights = uniform.copy() if reason else _project(smoothed, floor, cap)
    evidence = {"cutoff": cutoff.isoformat(), "window_start": window_start.isoformat(),
                "axes": list(axes), "horizon_bars": horizon_bars, "timeframe": timeframe,
                "policy": policy.to_dict(), "previous_weights": previous_weights,
                "frozen_records": eligible}
    return {
        "weights": weights, "correlations": correlations, "raw_scores": raw_scores,
        "clipped_scores": clipped_scores, "smoothed_scores": smoothed,
        "status": "uniform_fallback" if reason else "fitted",
        "reason": reason or "positive_rank_correlation", "sample_count": sample_count,
        "effective_blocks": effective_blocks, "block_count": len(blocks),
        "block_days": block_seconds / 86400, "cutoff": cutoff.isoformat(),
        "window_start": window_start.isoformat(), "previous_weights": previous_weights,
        "max_label_available_at": max((row["label_available_at"] for row in eligible), default=None),
        "source_model_versions": sorted({row["model_version"] for row in eligible}),
        "frozen_prediction_digests": [item[1] for item in ordered],
        "information_id": fingerprint(evidence), "duplicate_count": duplicates,
        "excluded_counts": dict(sorted(skipped.items())),
        "method": "positive_spearman_squared_clip_ema_bounded_simplex",
        "prediction_scope": "previously_frozen_entry_predictions_only",
        "uncertainty_scope": "time_block_support_diagnostic_not_independence_or_significance",
    }


def _support(stats, policy):
    if not isinstance(stats, Mapping):
        return "missing_support_statistics"
    for key in ("raw_count", "effective_samples", "effective_blocks", "weight_mass"):
        if _number(stats.get(key)) is None:
            return "missing_support_statistics"
    if stats["raw_count"] <= 0:
        return "cold_start"
    if stats["effective_samples"] < policy.min_effective_samples:
        return "insufficient_samples"
    if stats["effective_blocks"] < policy.min_effective_blocks:
        return "insufficient_blocks"
    if stats["weight_mass"] < policy.min_weight_mass:
        return "stale_weight_mass"
    return None


def combine_axis_score(base_EVLedger_query, attribution_dict, ev_policy):
    """Recompute EV and linear uncertainty without bypassing any ledger support."""
    result = deepcopy(base_EVLedger_query)
    axes = result.get("axes")
    result["uniform_estimate_bps"] = result.get("estimate_bps")
    result["uniform_lower_bound_bps"] = result.get("lower_bound_bps")
    result["axis_weighting"] = "causal_positive_rank_attribution"
    result["attribution_information_id"] = attribution_dict.get("information_id")
    result["attribution_status"] = attribution_dict.get("status")
    result["uncertainty_scope"] = "approximate_linear_axis_error_diagnostic_not_calibrated_ci"
    result["weighted_contributions"] = {}
    result["axis_weights"] = {}
    reason = None
    if result.get("status") == "abstain":
        reason = result.get("reason") or "unsupported_base_score"
    elif result.get("status") not in {"allow", "veto"}:
        reason = "unsupported_base_score"
    if not isinstance(axes, Mapping) or not axes:
        axes = {}
        reason = reason or "unknown_context"
    try:
        weights = _weights(attribution_dict.get("weights"), tuple(sorted(axes)))
    except ValueError:
        weights = {}
        reason = reason or "invalid_axis_weights"
    result["axis_weights"] = weights
    reason = reason or _support(result.get("prior"), ev_policy)
    prior = result.get("prior")
    if isinstance(prior, Mapping):
        prior_mean, prior_stderr = _number(prior.get("mean_bps")), _number(prior.get("stderr_bps"))
        if prior_mean is None or prior_stderr is None or prior_stderr < 0:
            reason = reason or "nonfinite_prior_estimate"
    known = bool(axes) and bool(weights)
    for axis, snapshot in axes.items():
        if not isinstance(snapshot, Mapping):
            reason = reason or "unknown_context"
            known = False
            continue
        if "support_reason" not in snapshot:
            reason = reason or "unknown_context"
        else:
            reason = reason or snapshot["support_reason"]
        estimate, stderr = _number(snapshot.get("estimate_bps")), _number(snapshot.get("stderr_bps"))
        if estimate is None or stderr is None or stderr < 0:
            known = False
            reason = reason or "nonfinite_axis_estimate"
        states = snapshot.get("states")
        if not isinstance(states, Mapping) or not states:
            reason = reason or "missing_support_statistics"
        else:
            total_probability = 0.0
            for state, cell in states.items():
                if not isinstance(cell, Mapping):
                    reason = reason or "missing_support_statistics"
                    continue
                probability = _number(cell.get("probability"))
                if probability is None or probability < 0:
                    reason = reason or "unknown_context"
                else:
                    total_probability += probability
                    if probability > 0:
                        reason = reason or ("unknown_context" if state == "unknown"
                                            else cell.get("support_reason") or _support(cell, ev_policy))
            if not math.isclose(total_probability, 1.0, rel_tol=0, abs_tol=1e-9):
                reason = reason or "unknown_context"
        if estimate is not None and stderr is not None and weights:
            result["weighted_contributions"][axis] = {
                "weight": weights[axis], "estimate_bps": weights[axis] * estimate,
                "stderr_bps": weights[axis] * stderr}
    estimate = (math.fsum(row["estimate_bps"] for row in result["weighted_contributions"].values())
                if known else None)
    stderr = (math.fsum(row["stderr_bps"] for row in result["weighted_contributions"].values())
              if known else None)
    lower = estimate - ev_policy.confidence_z * stderr if known else None
    upper = estimate + ev_policy.confidence_z * stderr if known else None
    if known and any(not math.isfinite(value) for value in (estimate, stderr, lower, upper)):
        estimate = stderr = lower = upper = None
        reason = reason or "nonfinite_combined_estimate"
    allow = None if reason or lower is None else lower > ev_policy.min_ev_bps
    result.update(estimate_bps=estimate, stderr_bps=stderr, lower_bound_bps=lower, upper_bound_bps=upper,
                  would_allow=allow, status="abstain" if allow is None else "allow" if allow else "veto",
                  reason=reason or ("lower_bound_positive" if allow else "lower_bound_not_positive"))
    return result
