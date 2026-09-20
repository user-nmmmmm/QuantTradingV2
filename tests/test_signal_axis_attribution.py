"""Frozen predictions and support floors remain binding under adaptive weights."""
from copy import deepcopy
from dataclasses import replace
import math

import pandas as pd
import pytest

from core.signal_adaptive_types import AdaptiveEVPolicy
from core.signal_axis_attribution import combine_axis_score, fit_axis_weights
from core.signal_ev_ledger import EVLedger
from core.signal_ev_types import EVPolicy


AXES = ("trend", "volatility", "efficiency")
START = pd.Timestamp("2020-01-01", tz="UTC")


def stamp(day):
    return (START + pd.Timedelta(days=day)).isoformat()


def policy(**kwargs):
    ev = EVPolicy(block_days=1, min_effective_samples=2, min_effective_blocks=2,
                  min_weight_mass=0.0001)
    return AdaptiveEVPolicy(ev_policy=ev, attribution_min_samples=6,
                            attribution_min_blocks=3, **kwargs)


def records(count=60, *, same_day=False, horizon=1):
    return [{"candidate_id": f"id-{i:03d}", "available_at": stamp(0 if same_day else i),
             "label_available_at": stamp((0 if same_day else i) + horizon),
             "net_return_bps": float(i), "model_version": f"frozen-fold-{i // 20}",
             "axes": {"trend": {"estimate_bps": float(i), "support_reason": None},
                      "volatility": {"estimate_bps": float(-i), "support_reason": None},
                      "efficiency": {"estimate_bps": 7.0, "support_reason": None}}}
            for i in range(count)]


def fit(rows=None, *, day=62, model_policy=None, **kwargs):
    return fit_axis_weights(records() if rows is None else rows, AXES,
                            model_policy or policy(), cutoff=stamp(day), **kwargs)


def supported_score(estimates=(100.0, -40.0, -40.0), *, stderr=10.0):
    ev = EVPolicy(min_effective_samples=2, min_effective_blocks=2, min_weight_mass=.01,
                  confidence_z=1.0)
    stats = {"raw_count": 30, "effective_samples": 30., "effective_blocks": 20.,
             "weight_mass": 25., "mean_bps": 0., "stderr_bps": stderr}
    axes = {name: {"estimate_bps": estimate, "stderr_bps": stderr, "support_reason": None,
                   "states": {"state": {**stats, "probability": 1., "support_reason": None}}}
            for name, estimate in zip(AXES, estimates)}
    mean = sum(estimates) / 3
    return {"axes": axes, "prior": deepcopy(stats), "estimate_bps": mean,
            "stderr_bps": stderr, "lower_bound_bps": mean - stderr,
            "upper_bound_bps": mean + stderr, "status": "allow" if mean > stderr else "veto",
            "would_allow": mean > stderr, "reason": "lower_bound_not_positive",
            "effective_samples": 30., "effective_blocks": 20., "weight_mass": 25.,
            "raw_count": 30}, ev


def attribution(weights=None):
    return {"weights": weights or {"trend": .6, "volatility": .2, "efficiency": .2},
            "status": "fitted", "information_id": "causal-weights-v1"}


def test_known_ranks_negative_and_constant_scores_and_hand_computed_smoothing():
    result = fit()
    assert result["correlations"] == {"efficiency": None, "trend": 1., "volatility": -1.}
    assert result["raw_scores"] == {"efficiency": 0., "trend": 1., "volatility": 0.}
    assert result["clipped_scores"] == {"efficiency": .15, "trend": .6, "volatility": .15}
    assert result["weights"] == pytest.approx({"trend": 49/120, "volatility": 71/240,
                                               "efficiency": 71/240})
    assert result["status"] == "fitted"
    assert result["sample_count"] == 60
    assert result["effective_blocks"] == 60
    assert result["source_model_versions"] == ["frozen-fold-0", "frozen-fold-1", "frozen-fold-2"]
    assert len(result["frozen_prediction_digests"]) == 60


def test_ties_use_average_ranks():
    rows = records(6)
    for row, estimate in zip(rows, [1, 1, 2, 2, 3, 3]):
        row["axes"]["trend"]["estimate_bps"] = estimate
    result = fit(rows, day=8)
    assert result["correlations"]["trend"] == pytest.approx(math.sqrt(32/35))


def test_same_clock_label_excluded_and_future_edits_cannot_change_information_identity():
    rows = records(60)
    first = fit(rows, day=59)
    assert first["sample_count"] == 58
    assert first["max_label_available_at"] == stamp(58)
    changed = deepcopy(rows)
    changed[58]["net_return_bps"] = 1e9
    changed[58]["model_version"] = "future-version"
    changed[59]["axes"] = {"invalid-future": None}
    second = fit(changed, day=59)
    assert first == second
    assert "future-version" not in first["source_model_versions"]


def test_source_prediction_changes_are_audited_but_unrelated_recomputed_fields_are_not_used():
    rows = records()
    first = fit(rows)
    changed = deepcopy(rows)
    changed[10]["axes"]["trend"]["estimate_bps"] = -1e4
    assert fit(changed)["information_id"] != first["information_id"]
    unrelated = deepcopy(rows)
    for row in unrelated:
        row["recomputed_prediction"] = {"trend": -row["net_return_bps"]}
    assert fit(unrelated) == first
    version = deepcopy(rows)
    version[10]["model_version"] = "different-frozen-source"
    assert fit(version)["information_id"] != first["information_id"]


def test_permutation_and_exact_duplicate_do_not_change_evidence_or_weights():
    rows = records()
    first = fit(rows)
    second = fit(list(reversed(rows)) + [deepcopy(rows[3])])
    assert first["weights"] == second["weights"]
    assert first["information_id"] == second["information_id"]
    assert first["frozen_prediction_digests"] == second["frozen_prediction_digests"]
    assert second["sample_count"] == 60
    assert second["duplicate_count"] == 1
    duplicate = deepcopy(rows[3])
    duplicate["net_return_bps"] += 1
    with pytest.raises(ValueError, match="conflicting frozen"):
        fit(rows + [duplicate])


def test_one_hundred_same_day_symbols_do_not_supply_independent_time_blocks():
    result = fit(records(100, same_day=True), day=5)
    assert result["sample_count"] == 100
    assert result["effective_blocks"] == 1
    assert result["reason"] == "insufficient_blocks"
    assert result["weights"] == dict.fromkeys(AXES, 1/3)


def test_horizon_sets_minimum_block_width_and_labels_must_have_matured():
    result = fit(records(60, horizon=20), day=82, horizon_bars=20)
    assert result["block_days"] == 20
    assert result["block_count"] <= 4
    assert result["effective_blocks"] <= 4
    premature = fit(records(60, horizon=1), day=82, horizon_bars=20)
    assert premature["sample_count"] == 0
    assert premature["excluded_counts"] == {"premature_label": 60}


def test_common_sample_requirement_does_not_impute_missing_or_unsupported_predictions():
    rows = records()
    rows[0]["axes"]["trend"]["support_reason"] = "cold_start"
    rows[1]["axes"]["efficiency"]["estimate_bps"] = math.nan
    del rows[2]["axes"]["volatility"]
    rows[3]["net_return_bps"] = math.inf
    del rows[4]["model_version"]
    del rows[5]["axes"]["trend"]["support_reason"]
    result = fit(rows)
    assert result["sample_count"] == 54
    assert result["excluded_counts"] == {"missing_source_model_version": 1,
                                            "nonfinite_or_missing_snapshot": 1,
                                            "unsupported_or_nonfinite_axis": 4}


def test_window_start_is_inclusive_and_earlier_facts_do_not_change_identity():
    rows = records()
    model_policy = policy(attribution_window_days=30)
    first = fit(rows, day=62, model_policy=model_policy)
    assert first["window_start"] == stamp(32)
    assert first["sample_count"] == 28
    rows[31]["net_return_bps"] = -9999
    assert fit(rows, day=62, model_policy=model_policy) == first


def test_sparse_and_no_positive_evidence_fall_back_to_uniform_even_with_previous_weights():
    previous = {"trend": .6, "volatility": .2, "efficiency": .2}
    sparse = fit(records(3), previous=previous)
    assert sparse["reason"] == "insufficient_samples"
    assert sparse["weights"] == dict.fromkeys(AXES, 1/3)
    rows = records()
    for row in rows:
        row["axes"]["trend"]["estimate_bps"] = -row["net_return_bps"]
    no_signal = fit(rows, previous=previous)
    assert no_signal["reason"] == "no_positive_attribution"
    assert no_signal["weights"] == dict.fromkeys(AXES, 1/3)


@pytest.mark.parametrize("floor,cap", [(.15, .60), (.3, .4), (1/3, 1/3)])
def test_projection_preserves_bounds_and_unit_mass_instead_of_clip_then_normalise(floor, cap):
    result = fit(model_policy=policy(axis_weight_floor=floor, axis_weight_cap=cap,
                                     attribution_ema_rate=1.))
    assert math.fsum(result["weights"].values()) == pytest.approx(1., abs=1e-15)
    assert all(floor <= weight <= cap for weight in result["weights"].values())
    assert result["weights"]["trend"] == pytest.approx(cap)


@pytest.mark.parametrize("previous", [{"trend": 1.}, dict.fromkeys(AXES, 0.),
                                       {"trend": .7, "volatility": .15, "efficiency": .15},
                                       {"trend": math.nan, "volatility": .2, "efficiency": .2}])
def test_invalid_previous_weights_are_rejected(previous):
    with pytest.raises(ValueError, match="weights"):
        fit(previous=previous)


def test_fitting_does_not_mutate_records_previous_or_policy():
    rows = records()
    previous = {"trend": .5, "volatility": .3, "efficiency": .2}
    original = deepcopy((rows, previous))
    result = fit(rows, previous=previous)
    assert (rows, previous) == original
    rows[0]["axes"]["trend"]["estimate_bps"] = 100000
    previous["trend"] = 0
    assert result["previous_weights"]["trend"] == .5


@pytest.mark.parametrize("field", ["available_at", "label_available_at"])
def test_missing_record_time_cannot_be_interpreted_as_current_wall_clock(field):
    rows = records()
    rows[0][field] = None
    with pytest.raises(ValueError, match="finite timestamp"):
        fit(rows)


def test_weighted_score_can_change_decision_and_uses_linear_not_independent_axis_error():
    base, ev = supported_score()
    assert base["status"] == "veto"
    result = combine_axis_score(base, attribution(), ev)
    assert result["estimate_bps"] == 44
    assert result["stderr_bps"] == 10
    assert result["lower_bound_bps"] == 34
    assert result["status"] == "allow"
    assert result["would_allow"] is True
    assert result["uniform_estimate_bps"] == pytest.approx(20/3)
    assert result["weighted_contributions"]["trend"]["estimate_bps"] == 60
    other = combine_axis_score(base, attribution({"trend": .15, "volatility": .6, "efficiency": .25}), ev)
    assert other["status"] == "veto"
    assert other["would_allow"] is False


@pytest.mark.parametrize("location,reason", [("base", "training_window"), ("axis", "unknown_context"),
                                              ("cell", "insufficient_blocks"),
                                              ("prior", "stale_weight_mass")])
def test_weighting_cannot_bypass_base_axis_cell_or_prior_support(location, reason):
    base, ev = supported_score()
    if location == "base":
        base.update(status="abstain", reason=reason)
    elif location == "axis":
        base["axes"]["efficiency"]["support_reason"] = reason
    elif location == "cell":
        base["axes"]["efficiency"]["states"]["state"]["effective_blocks"] = 1
    else:
        base["prior"]["weight_mass"] = .000001
    result = combine_axis_score(base, attribution(), ev)
    assert result["status"] == "abstain"
    assert result["reason"] == reason
    assert result["would_allow"] is None


@pytest.mark.parametrize("broken", [None, math.nan, math.inf, "100", True])
def test_nonfinite_axis_is_abstention_not_prior_or_zero_imputation(broken):
    base, ev = supported_score()
    base["axes"]["efficiency"]["estimate_bps"] = broken
    result = combine_axis_score(base, attribution(), ev)
    assert result["status"] == "abstain"
    assert result["estimate_bps"] is None
    assert result["lower_bound_bps"] is None


def test_nonfinite_prior_or_empty_probability_mass_cannot_pass_support_gate():
    base, ev = supported_score()
    base["prior"]["mean_bps"] = None
    assert combine_axis_score(base, attribution(), ev)["reason"] == "nonfinite_prior_estimate"
    base, ev = supported_score()
    base["axes"]["efficiency"]["states"]["state"]["probability"] = 0
    result = combine_axis_score(base, attribution(), ev)
    assert result["status"] == "abstain"
    assert result["reason"] == "unknown_context"


def test_lower_bound_equality_is_veto_and_inputs_remain_unchanged():
    base, ev = supported_score()
    attr = attribution()
    original = deepcopy((base, attr))
    result = combine_axis_score(base, attr, replace(ev, min_ev_bps=34.))
    assert result["status"] == "veto"
    assert (base, attr) == original
    result["axes"]["trend"]["estimate_bps"] = 0
    assert base["axes"]["trend"]["estimate_bps"] == 100


def test_uniform_weight_result_matches_actual_ledger_with_all_cell_statistics():
    ev = EVPolicy(min_effective_samples=2, min_effective_blocks=2, min_weight_mass=.01,
                  block_days=1, prior_strength=0.)
    ledger = EVLedger(ev)
    for i in range(20):
        candidate = {"candidate_id": str(i), "strategy": "Trend", "symbol": "BTC/USDT",
                     "direction": "long", "signal_version": "v1",
                     "context": {"timeframe": "1d", "snapshot_version": "v1",
                                 "available_at": stamp(i)}}
        outcome = {"candidate_id": str(i), "horizon_bars": 1, "status": "matured",
                   "scope": "independent_fixed_notional_signal_diagnostic",
                   "available_at": stamp(i + 1), "execution_flags": [], "net_return_bps": 100 + i}
        ledger.add(candidate, outcome, {axis: {"state": 1.} for axis in AXES}, cutoff=stamp(30))
    query = deepcopy(candidate)
    query["candidate_id"] = "test"
    query["context"]["available_at"] = stamp(31)
    base = ledger.query(query, {axis: {"state": 1.} for axis in AXES}, 1, as_of=stamp(31))
    result = combine_axis_score(base, attribution(dict.fromkeys(AXES, 1/3)), ev)
    for key in ("estimate_bps", "stderr_bps", "lower_bound_bps", "upper_bound_bps"):
        assert result[key] == pytest.approx(base[key])
    assert result["status"] == base["status"]


@pytest.mark.parametrize("weights", [{"trend": 1., "efficiency": 0., "volatility": 0.},
                                     {"trend": .6, "efficiency": .2},
                                     {"trend": .6, "efficiency": .2, "volatility": .3}])
def test_invalid_combination_weights_fail_closed(weights):
    base, ev = supported_score()
    result = combine_axis_score(base, attribution(weights), ev)
    assert result["status"] == "abstain"
    assert result["reason"] == "invalid_axis_weights"
    assert result["estimate_bps"] is None
