"""Nested fitting and frozen forecast timing across complete research folds."""
from copy import deepcopy
from dataclasses import replace
import math

import pandas as pd
import pytest

from core.signal_adaptive import build_adaptive_signal_meta
from core.signal_adaptive_types import AdaptiveEVPolicy, MetaReplayPolicy
from core.signal_ev_types import EVPolicy
from core.signal_observation_types import fingerprint
from tests.test_signal_meta_layer import facts


def adaptive_policy(**kwargs):
    return AdaptiveEVPolicy(**({"enabled": True, "regime_fit_days": 20, "regime_clusters": 2,
        "regime_neighbors": 3, "regime_quantiles": 7, "min_regime_samples": 6, "regime_restarts": 1,
        "attribution_window_days": 30, "attribution_min_samples": 3, "attribution_min_blocks": 2.,
        "ev_policy": EVPolicy(enabled=True, train_days=60, test_days=20, embargo_days=3,
            block_days=1, min_effective_samples=2., min_effective_blocks=2., min_weight_mass=.01,
            min_std_bps=1., prior_strength=2.)} | kwargs))


def adaptive_facts(days=range(110)):
    payload = facts(days=days, horizons=(1, 3))
    for i, c in enumerate(payload["candidates"]):
        c["context"]["features"] = {"return_12": math.sin(i*.71),
            "efficiency_ratio_10": .5+.4*math.cos(i*.43), "volatility_ratio_8_48": 1.+.5*math.sin(i*.23)}
    for i, o in enumerate(payload["outcomes"]):
        o["net_return_bps"] = 200+50*math.sin((i//2)*.71)+20*math.cos((i//2)*.43)
    return payload


@pytest.fixture(scope="module")
def original():
    return build_adaptive_signal_meta(adaptive_facts(), adaptive_policy())


def test_disabled_policy_and_complete_nested_pipeline(original):
    assert build_adaptive_signal_meta(adaptive_facts()) is None
    assert original["status"] == "complete", original.get("errors")
    assert all(original["validation"].values())
    assert len(original["predictions"]) == 220
    assert len(original["folds"]) == 3
    assert original["regime_models"]
    assert any(a["status"] == "available" for m in original["regime_models"] for a in m["axes"].values())
    assert any(p["status"] == "allow" for p in original["predictions"])
    assert any(a["status"] == "fitted" for a in original["attribution"])
    assert any(len(s) == 2 for p in original["predictions"] for s in p["memberships"].values())
    for prediction in original["predictions"]:
        for axis, states in prediction["memberships"].items():
            entropy = prediction["posterior_entropy"][axis]
            if "unknown" in states:
                assert entropy is None
            else:
                assert math.isfinite(entropy) and entropy >= 0


def test_regime_population_and_test_boundaries_are_disjoint(original):
    first = original["folds"][0]
    assert first["regime_fit_cutoff"] == "2020-01-21T00:00:00+00:00"
    assert first["training_cutoff"] == "2020-03-01T00:00:00+00:00"
    assert first["start"] == "2020-03-04T00:00:00+00:00"
    for record in original["calibration_predictions"]:
        if record["fold_id"] != first["fold_id"]:
            continue
        assert first["regime_fit_cutoff"] <= record["available_at"] < first["training_cutoff"]
        assert record["max_training_label_at"] is None or record["max_training_label_at"] < record["available_at"]
    for row in original["attribution"]:
        assert row["max_label_available_at"] is None or row["max_label_available_at"] < row["cutoff"]


def test_future_labels_never_change_existing_fold_predictions(original):
    payload = adaptive_facts()
    cutoff = pd.Timestamp(original["folds"][0]["training_cutoff"])
    for o in payload["outcomes"]:
        if pd.Timestamp(o["available_at"]) >= cutoff:
            o["net_return_bps"] = -1e5
    changed = build_adaptive_signal_meta(payload, adaptive_policy())
    assert changed["status"] == "complete"
    historical = lambda r: [p for p in r["predictions"] if p["fold_id"] in (None, "fold_0000")]
    assert historical(changed) == historical(original)
    assert changed["folds"][0] == original["folds"][0]


def test_future_candidate_book_and_features_do_not_rewrite_history(original):
    payload = adaptive_facts()
    cid = payload["candidates"][75]["candidate_id"]
    payload["candidates"][75]["direction"] = "short"
    payload["candidates"][75]["context"]["features"]["return_12"] = -50.
    for o in payload["outcomes"]:
        if o["candidate_id"] == cid:
            o["direction"] = "short"
    changed = build_adaptive_signal_meta(payload, adaptive_policy())
    assert changed["status"] == "complete", changed.get("errors")
    earlier = lambda r: [p for p in r["predictions"] if pd.Timestamp(p["available_at"]) < pd.Timestamp("2020-03-16", tz="UTC")]
    assert earlier(changed) == earlier(original)
    assert changed["folds"][0] == original["folds"][0]


def test_entry_forecast_does_not_know_its_own_outcome(original):
    payload = adaptive_facts()
    cid = "c045"
    for o in payload["outcomes"]:
        if o["candidate_id"] == cid:
            o["net_return_bps"] = -1e4
    changed = build_adaptive_signal_meta(payload, adaptive_policy())
    frozen = lambda r: [{k: v for k, v in p.items() if k not in ("net_return_bps",)}
        for p in r["calibration_predictions"] if p["fold_id"] == "fold_0000" and p["candidate_id"] == cid]
    assert frozen(changed) == frozen(original)
    assert changed["folds"][0]["training_digest"] != original["folds"][0]["training_digest"]


def test_prefix_and_input_permutation_are_stable(original):
    payload = adaptive_facts()
    for name in ("candidates", "decisions", "outcomes"):
        payload[name].reverse()
    saved = deepcopy(payload)
    reordered = build_adaptive_signal_meta(payload, adaptive_policy())
    assert payload == saved
    assert fingerprint(reordered) == fingerprint(original)
    prefix = adaptive_facts(range(74))
    for o in prefix["outcomes"]:
        if pd.Timestamp(o["available_at"]) >= pd.Timestamp("2020-03-15", tz="UTC"):
            o.update(status="censored_end_of_data", net_return_bps=None)
            o.pop("execution_flags")
    truncated = build_adaptive_signal_meta(prefix, adaptive_policy())
    assert truncated["predictions"] == original["predictions"][:148]
    assert truncated["folds"] == original["folds"][:1]


def test_empty_and_default_sparse_data_are_valid_abstentions():
    result = build_adaptive_signal_meta(adaptive_facts([]), adaptive_policy())
    assert result["status"] == "complete" and not result["predictions"]
    result = build_adaptive_signal_meta(adaptive_facts(), AdaptiveEVPolicy(enabled=True))
    assert result["status"] == "complete"
    assert all(p["status"] == "abstain" for p in result["predictions"])
    assert not result["folds"]


def test_bad_input_produces_incomplete_research():
    payload = adaptive_facts()
    payload["outcomes"].pop()
    result = build_adaptive_signal_meta(payload, adaptive_policy())
    assert result["status"] == "incomplete"
    assert result["errors"] and not result["predictions"]


@pytest.mark.parametrize("changes", [{"regime_clusters": 1}, {"seed": -1}, {"regime_quantiles": 1},
    {"axis_weight_floor": .4}, {"axis_weight_cap": .3}, {"attribution_ema_rate": 2},
    {"attribution_min_blocks": 1}, {"regime_fit_days": 365}, {"regime_neighbors": 40},
    {"attribution_min_samples": 2}, {"enabled": "true"}])
def test_invalid_adaptive_policies_rejected(changes):
    with pytest.raises(ValueError):
        replace(AdaptiveEVPolicy(), **changes)


@pytest.mark.parametrize("changes", [{"max_size_multiplier": 1.5}, {"min_size_multiplier": 0},
    {"max_gross_fraction": 2}, {"horizon_bars": True}, {"reference_notional": float("inf")}])
def test_replay_risk_bounds_are_explicit(changes):
    with pytest.raises(ValueError):
        replace(MetaReplayPolicy(), **changes)
