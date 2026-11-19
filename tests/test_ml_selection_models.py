"""Model correctness on hand-computable data, not trading effectiveness evidence."""

import json

import numpy as np
import pandas as pd
import pytest

from research.ml_selection.models import (
    BernoulliPolicy,
    FeatureScaler,
    LightGBMModel,
    fit_model,
    load_model,
    ranking_metrics,
)


def samples():
    train = pd.DataFrame({
        "as_of": pd.date_range("2024-01-01", periods=4, tz="UTC"),
        "label_available_at": pd.date_range("2024-01-02", periods=4, tz="UTC"),
        "trend": [1.0, 2.0, np.nan, 4.0],
        "constant": [7.0, 7.0, 7.0, 7.0],
        "label_net_return": [0.1, 0.2, 0.2, 0.4],
    })
    validation = pd.DataFrame({
        "as_of": pd.date_range("2024-01-06", periods=2, tz="UTC"),
        "label_available_at": pd.date_range("2024-01-07", periods=2, tz="UTC"),
        "trend": [1000.0, 2000.0], "constant": [7.0, 7.0],
        "label_net_return": [10.0, 20.0],
    })
    return train, validation


def test_training_only_normalization_and_unpenalized_intercept():
    train, validation = samples()
    model = fit_model(train, validation, features=["trend", "constant"], params={"alpha": 0})
    assert model.scaler.median == pytest.approx([2.0, 7.0])
    assert model.scaler.mean == pytest.approx([2.25, 7.0])
    assert model.scaler.scale == pytest.approx([np.std([1.0, 2.0, 2.0, 4.0]), 1.0])
    assert model.predict(train) == pytest.approx(train["label_net_return"])
    changed_validation = validation.assign(trend=-1e9, label_net_return=-1e9)
    other = fit_model(train, changed_validation, features=model.features, params={"alpha": 0})
    assert other.coefficients == pytest.approx(model.coefficients)
    assert other.scaler.mean == pytest.approx(model.scaler.mean)
    constant_labels = train.assign(label_net_return=0.123)
    penalized = fit_model(constant_labels, validation, features=model.features, params={"alpha": 1e6})
    assert penalized.intercept == pytest.approx(0.123)
    assert penalized.predict(train) == pytest.approx([0.123] * len(train))


def test_invalid_labels_dropped_before_fitting_preprocessing():
    train, validation = samples()
    train.loc[3, "label_net_return"] = np.inf
    model = fit_model(train, validation, features=["trend", "constant"])
    assert model.metadata["train_rows"] == 3
    assert model.metadata["train_dropped_invalid_labels"] == 1
    assert model.scaler.median[0] == pytest.approx(1.5)
    assert model.metadata["train_max_as_of"] == "2024-01-03T00:00:00+00:00"
    assert model.metadata["train_latest_label_available_at"] == "2024-01-04T00:00:00+00:00"


def test_future_training_labels_and_overlapping_decisions_rejected():
    train, validation = samples()
    future = train.copy()
    future.loc[3, "label_available_at"] = pd.Timestamp("2024-01-07", tz="UTC")
    with pytest.raises(ValueError, match="not available"):
        fit_model(future, validation, features=["trend"])
    overlap = validation.copy()
    overlap.loc[0, "as_of"] = pd.Timestamp("2024-01-04", tz="UTC")
    with pytest.raises(ValueError, match="overlap"):
        fit_model(train, overlap, features=["trend"])
    # A label becoming available exactly at validation decision time is allowed.
    train.loc[3, "label_available_at"] = validation.loc[0, "as_of"]
    fit_model(train, validation, features=["trend"])


def test_missing_availability_and_empty_labels_are_rejected():
    train, validation = samples()
    train.loc[0, "label_available_at"] = pd.NaT
    with pytest.raises(ValueError, match="invalid label_available_at"):
        fit_model(train, validation, features=["trend"])
    with pytest.raises(ValueError, match="no finite labels"):
        fit_model(train.assign(label_net_return=np.nan), validation, features=["trend"])


def test_ridge_roundtrip_identity_and_feature_order(tmp_path):
    train, validation = samples()
    model = fit_model(train, validation, features=["trend", "constant"])
    target = tmp_path / "model.json"
    model.save(target)
    restored = load_model(target)
    assert restored.model_id == model.model_id
    assert restored.metadata == model.metadata
    assert restored.predict(validation) == pytest.approx(model.predict(validation))
    # Input column arrangement can vary; the artifact's feature order controls it.
    assert restored.predict(validation[["constant", "trend"]]) == pytest.approx(model.predict(validation))
    with pytest.raises(ValueError, match="feature mismatch"):
        restored.predict(validation.drop(columns="trend"))
    with pytest.raises(ValueError, match="unique"):
        fit_model(train, validation, features=["trend", "trend"])
    payload = json.loads(target.read_text())
    payload["coefficients"][0] += 1
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        load_model(target)


def test_all_missing_feature_uses_zero_and_unit_scale():
    frame = pd.DataFrame({"unknown": [np.nan, np.inf, -np.inf], "flat": [2, 2, 2]})
    scaler = FeatureScaler.fit(frame, ["unknown", "flat"])
    assert scaler.transform(frame) == pytest.approx(np.zeros((3, 2)))
    assert scaler.median == pytest.approx([0, 2])
    assert scaler.scale == pytest.approx([1, 1])


def test_unknown_model_and_invalid_regularization_do_not_fallback():
    train, validation = samples()
    with pytest.raises(ValueError, match="unknown model kind"):
        fit_model(train, validation, features=["trend"], kind="unknown")
    with pytest.raises(ValueError, match="nonnegative"):
        fit_model(train, validation, features=["trend"], params={"alpha": -1})
    with pytest.raises(ValueError, match="unknown ridge parameters"):
        fit_model(train, validation, features=["trend"], params={"bogus": 1})


def test_ranking_metrics_hand_calculation_and_pending_cohort():
    frame = pd.DataFrame({
        "as_of": ["2024-01-01"] * 4,
        "label_available_at": ["2024-01-03"] * 4,
        "label_net_return": [0.1, 0.2, 0.3, 0.4],
    })
    metrics = ranking_metrics(frame, [1, 2, 3, 4], mature_as_of="2024-01-04", quantiles=2)
    assert metrics["mean_spearman"] == pytest.approx(1)
    assert metrics["mean_top_minus_bottom"] == pytest.approx(0.2)
    assert metrics["cohorts"][0]["quantile_returns"] == pytest.approx({"1": 0.15, "2": 0.35})
    with pytest.raises(ValueError, match="mature_as_of"):
        ranking_metrics(frame, [1, 2, 3, 4])
    frame.loc[3, "label_available_at"] = "2024-02-01"
    pending = ranking_metrics(frame, [1, 2, 3, 4], mature_as_of="2024-01-04", quantiles=2)
    assert pending["mature_rows"] == 3
    assert pending["mean_spearman"] is None
    assert pending["cohorts"][0]["status"] == "pending"
    frame["label_net_return"] = frame["label_net_return"].astype(object)
    frame.loc[3, "label_net_return"] = "unreadable_future_label"
    assert ranking_metrics(frame, [1, 2, 3, 4], mature_as_of="2024-01-04", quantiles=2) == pending
    json.dumps(pending, allow_nan=False)


def test_ranking_constant_scores_and_missing_mature_labels():
    frame = pd.DataFrame({"as_of": ["2024-01-01"] * 3,
                          "label_net_return": [0.1, 0.2, 0.3]})
    result = ranking_metrics(frame, [1, 1, 1], quantiles=2)
    assert result["mean_spearman"] is None
    frame.loc[1, "label_net_return"] = np.nan
    assert ranking_metrics(frame, [1, 2, 3])["cohorts"][0]["reason"] == "missing_mature_labels"


@pytest.mark.parametrize("action,advantage,direction", [
    (True, 1, 1), (True, -1, -1), (False, 1, -1), (False, -1, 1),
])
def test_single_observation_policy_gradient_direction(action, advantage, direction):
    train = pd.DataFrame({"trend": [-1.0, 1.0]})
    policy = BernoulliPolicy(["trend"]).fit_scaler(train)
    observation = pd.DataFrame({"trend": [1.0]})
    before = policy.predict(observation)[0]
    stats = policy.update(observation, [action], [advantage], learning_rate=0.1, entropy_coef=0)
    after = policy.predict(observation)[0]
    assert (after - before) * direction > 0
    assert stats["update_count"] == 1
    assert stats["mean_advantage"] == advantage


def test_policy_roundtrip_restores_rng_and_parameters(tmp_path):
    train, validation = samples()
    ridge = fit_model(train, validation, features=["trend", "constant"])
    policy = BernoulliPolicy(ridge.features, scaler=ridge.scaler, seed=11,
                             metadata={"train_model_id": ridge.model_id})
    policy.act(train)
    policy.update(train, [1, 1, 0, 0], [1, 0.5, -0.3, -0.8])
    target = tmp_path / "policy.json"
    policy.save(target)
    restored = BernoulliPolicy.load(target)
    assert restored.model_id == policy.model_id
    assert restored.predict(train) == pytest.approx(policy.predict(train))
    expected_actions, expected_p = policy.act(train)
    actual_actions, actual_p = restored.act(train)
    assert np.array_equal(expected_actions, actual_actions)
    assert actual_p == pytest.approx(expected_p)
    assert restored.update_count == 1
    assert restored.metadata == policy.metadata


def test_policy_probability_and_gradient_bounds():
    scaler = FeatureScaler.fit(np.asarray([[-1.0], [1.0]]), ["x"])
    policy = BernoulliPolicy(["x"], scaler=scaler, weights=[1000], probability_epsilon=1e-5,
                             max_grad_norm=0.1)
    probabilities = policy.predict(np.asarray([[-1e4], [1e4]]))
    assert probabilities == pytest.approx([1e-5, 1 - 1e-5])
    previous = policy.weights.copy()
    stats = policy.update(np.asarray([[1.0]]), [False], [1e8], learning_rate=0.1, entropy_coef=0)
    assert stats["gradient_clipped"] is True
    assert abs(policy.weights[0] - previous[0]) <= 0.01
    assert np.isfinite(policy.predict(np.asarray([[-1e4], [1e4]]))).all()
    json.dumps(stats, allow_nan=False)


def test_policy_requires_scaler_and_matching_features():
    policy = BernoulliPolicy(["trend"])
    with pytest.raises(ValueError, match="training scaler"):
        policy.act(np.asarray([[1.0]]))
    scaler = FeatureScaler.fit(np.asarray([[1, 2], [2, 3]]), ["trend", "volume"])
    with pytest.raises(ValueError, match="feature order"):
        BernoulliPolicy(["volume", "trend"], scaler=scaler)
    policy.fit_scaler(np.asarray([[-1.0], [1.0]]))
    with pytest.raises(ValueError, match="zero or one"):
        policy.update(np.asarray([[1.0]]), [0.5], [1])
    with pytest.raises(ValueError, match="finite"):
        policy.update(np.asarray([[1.0]]), [1], [np.nan])


def test_optional_lightgbm_model_roundtrip(tmp_path):
    pytest.importorskip("lightgbm")
    train, validation = samples()
    model = fit_model(train, validation, features=["trend", "constant"], kind="lightgbm",
                      params={"num_boost_round": 10, "early_stopping_rounds": 3,
                              "min_data_in_leaf": 1, "min_data_in_bin": 1})
    target = tmp_path / "lightgbm.json"
    model.save(target)
    restored = load_model(target)
    assert restored.predict(validation) == pytest.approx(model.predict(validation))
    assert restored.model_id == model.model_id
    assert model.metadata["params"]["device_type"] == "cpu"


@pytest.mark.parametrize("threads", [1, 2, 16, None])
def test_lightgbm_prediction_explicitly_enforces_frozen_thread_budget(threads):
    class Booster:
        def feature_name(self):
            return ["trend"]

        def predict(self, values, **kwargs):
            self.received = kwargs
            return np.zeros(len(values))

    scaler = FeatureScaler.fit(np.asarray([[-1.0], [1.0]]), ["trend"])
    metadata = {"params": {"num_threads": threads}} if threads is not None else {}
    booster = Booster()
    model = LightGBMModel(scaler, booster, metadata)
    assert model.predict(np.asarray([[0.0]])).tolist() == [0.0]
    assert booster.received == {"num_threads": threads if threads is not None else 2}


@pytest.mark.parametrize("threads", [0, 17, True, float("nan")])
def test_lightgbm_prediction_rejects_invalid_thread_budget(threads):
    class Booster:
        def feature_name(self):
            return ["trend"]

        def predict(self, *args, **kwargs):
            raise AssertionError("invalid budget reached Booster")

    scaler = FeatureScaler.fit(np.asarray([[-1.0], [1.0]]), ["trend"])
    model = LightGBMModel(scaler, Booster(), {"params": {"num_threads": threads}})
    with pytest.raises(ValueError, match="num_threads"):
        model.predict(np.asarray([[0.0]]))
