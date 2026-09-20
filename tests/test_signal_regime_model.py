"""Hand-checkable geometry, immutable fit and strict chronological contracts."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import math

import numpy as np
import pandas as pd
import pytest

from core.signal_regime_model import (
    AXIS_FEATURES, RegimeModel, quantile_barycenter, soft_memberships,
    wasserstein_squared,
)


def candidate(number, day=None, feature=None):
    day = number if day is None else day
    feature = number / 100 if feature is None else feature
    entry = pd.Timestamp("2020-01-01", tz="UTC") + pd.Timedelta(days=day)
    return {"candidate_id": f"candidate-{number:03d}", "symbol": "BTC/USDT",
            "strategy": "Trend", "signal_version": "v1", "direction": "long",
            "context": {"available_at": entry.isoformat(), "timeframe": "1d",
                        "snapshot_version": "costs-v1", "features": {
                            "return_12": feature - .5,
                            "efficiency_ratio_10": feature,
                            "volatility_ratio_8_48": feature + .5}}}


def observation(number, value=None, feature=None):
    c = candidate(number, feature=feature)
    stamp = pd.Timestamp(c["context"]["available_at"]) + pd.Timedelta(days=1)
    o = {"candidate_id": c["candidate_id"], "symbol": c["symbol"],
         "strategy": c["strategy"], "direction": c["direction"],
         "horizon_bars": 1, "status": "matured", "available_at": stamp.isoformat(),
         "scope": "independent_fixed_notional_signal_diagnostic", "execution_flags": [],
         "net_return_bps": float((-100 if number < 25 else 100) if value is None else value)}
    return {"candidate": c, "outcome": o}


def training():
    return [observation(number) for number in range(50)]


def fit(rows=None, **kwargs):
    return RegimeModel.fit(training() if rows is None else rows, cutoff="2020-03-01",
                           **({"neighbors": 5, "min_samples": 10} | kwargs))


def query(feature=.1):
    return candidate(90, day=90, feature=feature)


def memberships(model, feature=.1):
    c = query(feature)
    return model.memberships(c, as_of=c["context"]["available_at"])


def test_wasserstein_distance_and_barycenter_match_hand_calculation():
    assert wasserstein_squared([0, 2], [2, 4]) == 4
    assert wasserstein_squared([1, 2, 4], [1, 2, 4]) == 0
    assert quantile_barycenter([[0, 2], [2, 4]]) == [1, 3]
    assert quantile_barycenter([[0, 2], [2, 4]], [.25, .75]) == [.5 * 3, 3.5]
    assert quantile_barycenter([[0, 2], [2, 4]], [1e308, 1e308]) == [1, 3]


def test_soft_memberships_are_stable_and_equal_to_exp_negative_distance():
    result = soft_memberships([0, 2], 2)
    assert result == pytest.approx([1 / (1 + math.exp(-1)), 1 / (1 + math.exp(1))])
    assert soft_memberships([1e308, 1e308], 1e-300) == [.5, .5]
    assert soft_memberships([0, 1e308], 1e-300) == [1, 0]


@pytest.mark.parametrize("left,right", [([], []), ([2, 1], [1, 2]),
    ([0, float("nan")], [1, 2]), ([0, float("inf")], [1, 2]),
    ([0], [1, 2]), ([True], [1]), (["0"], [1]), ([1e308], [-1e308])])
def test_invalid_wasserstein_quantiles_fail(left, right):
    with pytest.raises(ValueError):
        wasserstein_squared(left, right)


@pytest.mark.parametrize("kwargs", [{"clusters": True}, {"clusters": 1},
    {"neighbors": 0}, {"quantiles": 1}, {"min_samples": 1}, {"n_init": 0},
    {"max_iter": 0}, {"seed": -1}, {"seed": 3.0}, {"clusters": 20, "min_samples": 10}])
def test_invalid_fit_spec_rejected(kwargs):
    with pytest.raises(ValueError):
        fit(**kwargs)


def test_separated_outcomes_produce_distinct_geometry_and_normalized_memberships():
    model = fit()
    manifest = model.manifest()
    assert manifest["training_sample_count"] == 50
    assert manifest["training_latest_label_at"] == "2020-02-20T00:00:00+00:00"
    for name in AXIS_FEATURES:
        axis = manifest["axes"][name]
        assert axis["status"] == "available"
        assert sum(axis["support"]) == 50
        assert min(axis["support"]) > 0
        assert axis["centroids"] == sorted(axis["centroids"])
        assert axis["temperature_bps2"] > 0
        assert axis["separation_bps2"] > 0
        assert memberships(model, .1)[name]["state_0"] > .9
        assert memberships(model, .45)[name]["state_1"] > .9
        assert sum(memberships(model, .1)[name].values()) == pytest.approx(1)
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest


def test_fit_is_invariant_to_observation_permutation():
    rows = training()
    reverse = fit(list(reversed(rows)))
    shuffle = deepcopy(rows)
    np.random.default_rng(14).shuffle(shuffle)
    assert fit().manifest() == reverse.manifest() == fit(shuffle).manifest()
    assert memberships(fit()) == memberships(reverse)


def test_training_distributions_leave_self_out_and_include_all_feature_boundary_ties():
    # k=1 sees both equidistant neighbours for the middle record; its own
    # enormous outcome cannot enter its training distribution.
    rows = [observation(0, 0, .1), observation(1, 1000, .2), observation(2, 100, .3)]
    for row, feature in zip(rows, [0., 1., 2.]):
        row["candidate"]["context"]["features"]["return_12"] = feature
    model = fit(rows, neighbors=1, min_samples=3, quantiles=3)
    centers = model.manifest()["axes"]["trend"]["centroids"]
    expected_quantiles = np.quantile([0., 100.], [1/6, .5, 5/6]).tolist()
    # Outer records see only 1000, middle record sees {0,100}.
    assert centers[0] == pytest.approx(expected_quantiles)
    assert centers[1] == [1000., 1000., 1000.]
    c = query(.1)
    c["context"]["features"]["return_12"] = .5
    description = model.describe(c, as_of=c["context"]["available_at"])
    # Midpoint query includes the entire tied boundary {0,1000}.
    assert description["axes"]["trend"]["empirical_quantiles_bps"] == pytest.approx(
        np.quantile([0., 1000.], [1/6, .5, 5/6]))


def test_model_and_returned_maps_are_deeply_frozen_against_input_mutation():
    rows = training()
    original = deepcopy(rows)
    model = fit(rows)
    manifest, expected = model.manifest(), memberships(model)
    assert rows == original
    rows[0]["outcome"]["net_return_bps"] = 1e9
    rows[0]["candidate"]["context"]["features"]["return_12"] = 1e9
    first = model.manifest()
    first["axes"]["trend"]["centroids"][0][0] = 1e9
    modified = memberships(model)
    modified["trend"]["state_0"] = 99
    assert model.manifest() == manifest
    assert memberships(model) == expected
    with pytest.raises(FrozenInstanceError):
        model._cutoff = "2099-01-01"


def test_inference_does_not_read_candidate_outcome_or_mutate_candidate():
    model = fit()
    c = query()
    original = deepcopy(c)
    before = model.memberships(c, as_of="2020-04-01")
    c["outcome"] = {"net_return_bps": 1e12}
    c["realized_return"] = -1e12
    after = model.memberships(c, as_of="2021-04-01")
    assert before == after
    assert {key: value for key, value in c.items() if key in original} == original


@pytest.mark.parametrize("stamp", ["2020-03-01", "2021-03-01"])
def test_training_labels_at_or_after_cutoff_are_rejected(stamp):
    rows = training()
    rows[-1]["outcome"]["available_at"] = stamp
    with pytest.raises(ValueError, match="strictly before cutoff"):
        fit(rows)


def test_future_entry_and_premature_label_are_rejected():
    future = observation(70)
    with pytest.raises(ValueError, match="entry must be strictly"):
        fit([future])
    premature = observation(0)
    premature["outcome"]["available_at"] = premature["candidate"]["context"]["available_at"]
    with pytest.raises(ValueError, match="horizon maturity"):
        fit([premature])


def test_inference_cannot_relabel_training_history_or_run_before_entry():
    model = fit()
    with pytest.raises(ValueError, match="cutoff must not be later"):
        model.memberships(candidate(3), as_of="2021-01-01")
    with pytest.raises(ValueError, match="as_of must not precede"):
        model.memberships(query(), as_of="2020-03-15")
    # Exact cutoff entry is eligible; labels were strictly before this time.
    c = candidate(90, day=60)
    assert set(model.memberships(c, as_of="2020-03-01")) == set(AXIS_FEATURES)


def test_frozen_prefix_outputs_do_not_change_when_later_data_or_models_exist():
    model = fit()
    original = memberships(model), model.manifest()
    extended = training() + [observation(number, value=10000.) for number in range(50, 80)]
    later = RegimeModel.fit(extended, cutoff="2020-04-01", neighbors=5, min_samples=10)
    assert later.manifest()["model_id"] != model.manifest()["model_id"]
    assert (memberships(model), model.manifest()) == original
    with pytest.raises(ValueError, match="strictly before cutoff"):
        fit(extended)


@pytest.mark.parametrize("field,value", [("strategy", "Other"), ("direction", "short"),
    ("signal_version", "v2"), ("snapshot_version", "v2"), ("timeframe", "12h")])
def test_model_book_isolation_at_fit_and_inference(field, value):
    rows = training()
    c = rows[-1]["candidate"]
    owner = c["context"] if field in {"snapshot_version", "timeframe"} else c
    owner[field] = value
    if field in {"strategy", "direction"}:
        rows[-1]["outcome"][field] = value
    with pytest.raises(ValueError, match="mixed training books"):
        fit(rows)
    c = query()
    owner = c["context"] if field in {"snapshot_version", "timeframe"} else c
    owner[field] = value
    with pytest.raises(ValueError, match="does not match fitted book"):
        fit().memberships(c, as_of="2020-04-01")


def test_duplicate_candidates_and_mixed_horizons_are_rejected():
    rows = training()
    with pytest.raises(ValueError, match="duplicate"):
        fit(rows + [deepcopy(rows[0])])
    rows[-1]["outcome"]["horizon_bars"] = 3
    with pytest.raises(ValueError, match="mixed training books"):
        fit(rows)


def test_constant_features_cannot_create_regimes_by_excluding_own_label():
    rows = [observation(number, feature=.5) for number in range(50)]
    model = fit(rows)
    for name, details in model.manifest()["axes"].items():
        assert details["status"] == "unavailable"
        assert details["reason"] == "insufficient_distinct_features"
        assert memberships(model)[name] == {"unknown": 1}


def test_identical_outcome_distributions_do_not_claim_separation():
    model = fit([observation(number, value=10) for number in range(50)])
    for details in model.manifest()["axes"].values():
        assert details["reason"] == "insufficient_distinct_distributions"
        assert details["centroids"] == []
        assert details["temperature_bps2"] is None


def test_insufficient_axis_data_and_missing_query_features_are_explicit_unknown():
    rows = training()
    for row in rows[:-3]:
        row["candidate"]["context"]["features"]["return_12"] = None
    model = fit(rows)
    manifest = model.manifest()
    assert manifest["axes"]["trend"]["sample_count"] == 3
    assert manifest["axes"]["trend"]["reason"] == "insufficient_feature_samples"
    assert manifest["axes"]["efficiency"]["status"] == "available"
    c = query()
    c["context"]["features"].pop("efficiency_ratio_10")
    result = model.memberships(c, as_of="2020-04-01")
    assert result["efficiency"] == result["trend"] == {"unknown": 1}
    assert "unknown" not in result["volatility"]


def test_empty_training_model_is_auditable_and_abstains():
    model = fit([])
    assert model.manifest()["training_sample_count"] == 0
    assert model.manifest()["training_latest_label_at"] is None
    assert memberships(model) == {name: {"unknown": 1.} for name in AXIS_FEATURES}


def test_censored_and_flagged_labels_never_fit_but_are_counted():
    rows = training()
    rows[0]["outcome"].update(status="censored_tail", available_at=None, net_return_bps=None)
    rows[1]["outcome"]["execution_flags"] = ["margin_breach"]
    model = fit(rows)
    assert model.manifest()["training_sample_count"] == 48
    assert model.manifest()["ignored"] == {"not_matured": 1, "execution_flags": 1}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_invalid_feature_and_return_numbers_rejected(value):
    rows = training()
    rows[0]["candidate"]["context"]["features"]["return_12"] = value
    with pytest.raises(ValueError):
        fit(rows)
    rows = training()
    rows[0]["outcome"]["net_return_bps"] = value
    with pytest.raises(ValueError):
        fit(rows)
    c = query()
    c["context"]["features"]["return_12"] = value
    with pytest.raises(ValueError):
        fit().memberships(c, as_of="2020-04-01")


@pytest.mark.parametrize("value", [None, "NaT", 123])
def test_invalid_cutoff_rejected(value):
    with pytest.raises(ValueError):
        RegimeModel.fit([], cutoff=value)


@pytest.mark.parametrize("rows", [None, {}, "rows"])
def test_invalid_training_container_rejected(rows):
    with pytest.raises(ValueError, match="observations must be a list"):
        RegimeModel.fit(rows, cutoff="2020-03-01")


def test_malformed_candidate_and_feature_containers_rejected():
    model = fit()
    for c in (None, {}, {"context": []}):
        with pytest.raises(ValueError, match="requires context"):
            model.memberships(c, as_of="2020-04-01")
    c = query()
    c["context"]["features"] = []
    with pytest.raises(ValueError, match="features must be a mapping"):
        model.memberships(c, as_of="2020-04-01")


@pytest.mark.parametrize("weights", [[0, 0], [-1, 1], [True, 1], [float("nan"), 1], [1]])
def test_invalid_barycenter_weights_rejected(weights):
    with pytest.raises(ValueError):
        quantile_barycenter([[0], [1]], weights)


@pytest.mark.parametrize("distances,temperature", [([1], 0), ([-1], 1),
    ([float("nan")], 1), ([], 1), ([0], True)])
def test_invalid_soft_memberships_rejected(distances, temperature):
    with pytest.raises(ValueError):
        soft_memberships(distances, temperature)
