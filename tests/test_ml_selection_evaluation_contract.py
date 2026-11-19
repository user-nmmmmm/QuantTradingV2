"""Protocol/orchestration checks with no estimator fitting or policy learning."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.ml_selection import pipeline, protocol
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.next_round import formal_budget_summary


def settings(*, independent=True, account="spot"):
    value = {"schema": "ml-selection-research/v1", "timeframe": "1d", "account_mode": account,
        "evaluation_kind": "retrospective", "data_registration": "data.json", "baseline_registration": "baseline.json",
        "start": "2020-01-01", "end": "2020-06-30",
        "splits": {"train_start": "2020-01-15", "train_end": "2020-03-01", "validation_end": "2020-04-01"},
        "models": ["ridge"], "rl": {"enabled": True, "episodes": 2, "seeds": [42, 43],
            "evaluation_threshold": .5, "evaluation_thresholds": [.49, .5, .51], "validation_patience": 5}}
    if independent:
        value["splits"]["calibration_end"] = "2020-05-01"
        value["evaluation_protocol"] = {"deployment_account_modes": [account],
            "maximum_validation_trials": 4, "maximum_calibration_trials": 3, "minimum_event_groups": 6}
    return value


def dataset():
    return pd.DataFrame([{"symbol": symbol, "as_of": day, "eligible": True,
        "label_net_return": .01, "label_available_at": day + pd.Timedelta(days=2),
        **dict.fromkeys(FEATURE_COLUMNS, 1.)}
        for day in pd.date_range("2020-01-01", "2020-06-30", tz="UTC") for symbol in ("A/USDT", "B/USDT")])


class FakePolicy:
    """Bookkeeping double: no fitted coefficients and no parameter updates."""
    reference_inputs = []
    def __init__(self, features, *, seed=42):
        self.features, self.seed, self.metadata, self.update_count = tuple(features), seed, {}, 0

    def fit_scaler(self, frame):
        self.reference_inputs.append(frame.copy())
        self.scaler = SimpleNamespace(features=self.features)
        return self

    def predict(self, frame):
        return np.full(len(frame), .5)

    def update(self, *args, **kwargs):
        self.update_count += 1
        return {"update_count": self.update_count}

    @property
    def model_id(self):
        return hashlib.sha256(json.dumps([self.seed, self.update_count, self.metadata], sort_keys=True).encode()).hexdigest()

    def save(self, path):
        protocol.save_json(path, {"seed": self.seed, "updates": self.update_count,
                                 "features": self.features, "metadata": self.metadata})

    @classmethod
    def load(cls, path):
        row = json.loads(path.read_text())
        value = cls(row["features"], seed=row["seed"])
        value.metadata, value.update_count = row["metadata"], row["updates"]
        value.scaler = SimpleNamespace(features=value.features)
        return value


@pytest.fixture
def mocks(monkeypatch):
    calls = []
    FakePolicy.reference_inputs = []
    monkeypatch.setattr(pipeline, "fit_model", lambda *a, **kw: pytest.fail("actual fitting is forbidden"))
    monkeypatch.setattr(pipeline, "BernoulliPolicy", FakePolicy)
    monkeypatch.setattr(pipeline, "progress", lambda *a, **kw: None)

    def selector(rows, configured, kind, models, *, policy, deterministic):
        features = {**dict.fromkeys(FEATURE_COLUMNS, 1.),
                    **dict.fromkeys(pipeline.ACCOUNT_FEATURES, 0.),
                    **dict.fromkeys(configured.get("rl", {}).get("context_features", []), 2.)}
        trajectory = [] if deterministic else [{"features": features, "bar_time": pd.Timestamp("2020-01-20", tz="UTC"), "action": True}]
        return SimpleNamespace(trajectory=trajectory, audit=[], seed=policy.seed,
                               threshold=policy.metadata["evaluation_threshold"], deterministic=deterministic)

    def environment(frames, frozen, *, start, end):
        configured = frozen["settings"]
        assert (start, end) in {(configured["splits"]["train_start"], configured["splits"]["train_end"]),
            (configured["splits"]["train_end"], configured["splits"]["validation_end"]),
            (configured["splits"]["validation_end"], configured["splits"].get("calibration_end"))}

        def run(selected):
            calls.append((selected.seed, selected.threshold, start, end))
            reward = (.2 if selected.seed == 43 else .1) if str(pd.Timestamp(end).date()) != "2020-05-01" else (.3 if selected.threshold == .49 else -.1)
            return SimpleNamespace(rewards=pd.DataFrame({"reward": [reward]}, index=[pd.Timestamp("2020-01-21", tz="UTC")]))
        return SimpleNamespace(run_episode=run)

    monkeypatch.setattr(pipeline, "selection", selector)
    monkeypatch.setattr(pipeline, "make_environment", environment)
    monkeypatch.setattr(pipeline, "persist_episode", lambda folder, name, episode, selected:
        {"reward_sum": float(episode.rewards.reward.sum()), "net_return": float(episode.rewards.reward.sum()),
         "max_drawdown": 0., "accounting_ok": True})
    return calls


def test_legacy_contract_is_explicitly_reused_and_does_not_mutate_settings():
    configured = settings(independent=False)
    before = deepcopy(configured)
    protocol.validate_settings(configured)
    scope = protocol.evaluation_contract(configured)
    assert scope["validation_reused_for_thresholds"] is True
    assert scope["independent_holdout"] is False
    assert scope["account_contract"]["compatibility_verified"] is False
    assert configured == before


def test_calibration_and_test_have_distinct_purged_maturity_cohorts():
    configured = settings()
    rows = dataset()
    day = pd.Timestamp("2020-04-15", tz="UTC")
    rows.loc[(rows.as_of == day) & (rows.symbol == "B/USDT"), "label_available_at"] = pd.Timestamp("2020-05-01", tz="UTC")
    parts = pipeline.splits(rows, configured)
    assert parts["train"].as_of.min() >= pd.Timestamp("2020-01-15", tz="UTC")
    assert day not in set(parts["calibration"].as_of)
    assert parts["calibration"].as_of.min() >= pd.Timestamp("2020-04-01", tz="UTC")
    assert parts["test"].as_of.min() >= pd.Timestamp("2020-05-01", tz="UTC")
    for name, end in (("train", "2020-03-01"), ("validation", "2020-04-01"), ("calibration", "2020-05-01")):
        assert (parts[name].label_available_at < pd.Timestamp(end, tz="UTC")).all()


@pytest.mark.parametrize("change", ["overlap", "account", "budget", "final", "window"])
def test_new_contract_rejects_unusable_boundaries_accounts_or_attempt_budgets(change):
    configured = settings()
    if change == "overlap":
        configured["splits"]["calibration_end"] = "2020-03-15"
    elif change == "account":
        configured["evaluation_protocol"]["deployment_account_modes"] = ["spot_margin"]
    elif change == "budget":
        configured["evaluation_protocol"]["maximum_validation_trials"] = 3
    elif change == "final":
        configured["evaluation_protocol"]["final_sample"] = {"start": "2020-05-01", "end": "2020-08-01", "opened": False, "maximum_frozen_candidates": 1}
    else:
        configured["walk_forward"] = [{"train_end": "2020-02-01", "validation_end": "2020-03-01", "test_end": "2020-04-01"}]
    with pytest.raises(ValueError):
        protocol.validate_settings(configured)


def test_margin_training_requires_an_explicit_matching_account_contract():
    configured = settings(account="spot_margin")
    protocol.validate_settings(configured)
    assert protocol.evaluation_contract(configured)["account_contract"]["deployment_account_modes"] == ["spot_margin"]
    del configured["evaluation_protocol"]
    with pytest.raises(ValueError, match="explicit"):
        protocol.validate_settings(configured)


def test_new_freeze_cannot_register_an_elapsed_final_period(tmp_path):
    configured = settings()
    configured["evaluation_protocol"]["final_sample"] = {"start": "2021-01-01", "end": "2021-04-01", "opened": False, "maximum_frozen_candidates": 1}
    with pytest.raises(ValueError, match="before its real start"):
        protocol.freeze_protocol(tmp_path / "run", configured, {"account": {"mode": "spot"}}, {"account_mode": "spot"}, {})
    assert not (tmp_path / "run").exists()


def test_checkpoint_and_seed_choice_never_use_calibration_or_development_test(tmp_path, mocks):
    configured = settings()
    protocol.validate_settings(configured)
    models = {"primary": SimpleNamespace(model_id="mock-parent")}
    policy = pipeline.train_policies(tmp_path, {"settings": configured}, {}, dataset(), models)
    validation_calls = [row for row in mocks if row[3] == "2020-04-01"]
    calibration_calls = [row for row in mocks if row[3] == "2020-05-01"]
    assert len(validation_calls) == 4 and {row[1] for row in validation_calls} == {.5}
    assert len(calibration_calls) == 3 and {row[0] for row in calibration_calls} == {43}
    assert policy.seed == 43 and policy.metadata["evaluation_threshold"] == .49
    receipt = json.loads((tmp_path / "threshold_calibration.json").read_text())
    assert receipt["weights_updated"] is False
    assert receipt["checkpoint_or_seed_selected_using_calibration"] is False
    assert receipt["independent_final_evidence"] is False
    budget = json.loads((tmp_path / "rl_budget_receipt.json").read_text())
    assert budget["trial_counts"] == {"checkpoint_validation_accounts": 4, "threshold_calibration_accounts": 3}


def test_calibration_resume_uses_frozen_receipt_without_repeating_evaluation(tmp_path, mocks):
    configured = settings()
    frozen = {"settings": configured}
    models = {"primary": SimpleNamespace(model_id="mock-parent")}
    pipeline.train_policies(tmp_path, frozen, {}, dataset(), models)
    checkpoint = FakePolicy.load(tmp_path / "rl_seeds/43/models/policy_best.json")
    before = len(mocks)
    restored = pipeline.calibrate_policy(tmp_path, frozen, {}, dataset(), models, checkpoint)
    assert restored.metadata["evaluation_threshold"] == .49 and len(mocks) == before
    configured["splits"]["calibration_end"] = "2020-05-02"
    with pytest.raises(ValueError, match="contract changed"):
        pipeline.calibrate_policy(tmp_path, frozen, {}, dataset(), models, checkpoint)


def test_budget_summary_does_not_claim_missing_legacy_trial_counts(tmp_path):
    configured = settings()
    configured["next_research"] = {"rl_windows": []}
    for seed in (42, 43):
        protocol.save_json(tmp_path / "rl_seeds" / str(seed) / "rl_budget_receipt.json",
                           {"actual_updates": 20, "minimum_budget_met": True})
    summary = formal_budget_summary(tmp_path, configured)
    assert summary["trial_counts"]["legacy_counts_missing"] is True
    assert summary["evaluation_scope"]["independent_holdout"] is False


def test_optional_real_candidate_context_uses_declared_unit_reference_not_fabricated_history(tmp_path, mocks):
    configured = settings()
    configured["rl"]["context_features"] = ["native_candidate_score", "stop_distance_fraction"]
    configured["selection"] = {"policy_gate_mode": "policy_and_return"}
    protocol.validate_settings(configured)
    policy = pipeline.train_policies(tmp_path, {"settings": configured}, {}, dataset(),
                                     {"primary": SimpleNamespace(model_id="mock-parent")})
    assert policy.features[-2:] == ("native_candidate_score", "stop_distance_fraction")
    assert FakePolicy.reference_inputs[0]["native_candidate_score"].eq(0.).all()
    assert policy.metadata["context_feature_scaling"] == "fixed_zero_reference_unit_scale_not_observed_training_state"
    assert policy.metadata["policy_gate_mode"] == "policy_and_return"
    assert "not_individual_candidate_pnl" in policy.metadata["credit_assignment"]
    configured["rl"]["context_features"] = ["invented_win_probability"]
    with pytest.raises(ValueError, match="actual-candidate"):
        protocol.validate_settings(configured)


def test_candidate_training_adapter_keeps_serving_table_separate_and_records_target_identity(tmp_path, mocks, monkeypatch):
    from research.ml_selection import candidate_dataset
    configured = settings()
    configured["training_data"] = {"candidate_dataset_directory": str(tmp_path), "target_type": "actual_exit", "data_identity": "registered-market"}
    source = dataset()
    candidates = source.loc[source.as_of.dt.day.eq(15)].copy()
    candidates.loc[:, list(FEATURE_COLUMNS)] = 2.
    receipt = {"source_protocol_verified": True, "contract_id": "fixed-candidate-contract", "target_type": "actual_exit"}
    loads = []

    def load(directory, **kwargs):
        loads.append(kwargs)
        return candidates.copy(), receipt

    monkeypatch.setattr(candidate_dataset, "load_candidate_training_rows", load)
    fitted = []

    class ScoreModel:
        model_id = "mock-candidate-model"
        metadata = {}

        def predict(self, frame):
            return np.full(len(frame), .01)

        def predict_net_return(self, frame):
            return self.predict(frame)

        def save(self, path):
            protocol.save_json(path, {"metadata": self.metadata})

    def fit(train, validation, **kwargs):
        fitted.append((train.copy(), validation.copy()))
        return ScoreModel()

    monkeypatch.setattr(pipeline, "fit_model", fit)
    models = pipeline.supervised(tmp_path, {"settings": configured}, source)
    assert len(fitted) == 1
    assert fitted[0][0].as_of.dt.day.eq(15).all() and fitted[0][1].as_of.dt.day.eq(15).all()
    assert fitted[0][0][FEATURE_COLUMNS[0]].eq(2.).all()
    assert source[FEATURE_COLUMNS[0]].eq(1.).all()
    assert loads[0]["account_mode"] == "spot" and loads[0]["data_identity"] == "registered-market"
    assert models["ridge"].metadata["training_data"]["receipt"] == receipt
    assert models["ridge"].metadata["training_data"]["serving_table"] == "separate_full_causal_market_table"


def test_candidate_artifact_evidence_is_bound_at_freeze_and_cannot_change_on_resume(tmp_path, monkeypatch):
    from research.ml_selection import candidate_dataset
    directory = tmp_path / "candidate"
    directory.mkdir()
    protocol.save_json(directory / "contract.json", {"contract_id": "frozen-target"})
    configured = settings()
    configured["training_data"] = {"candidate_dataset_directory": str(directory), "target_type": "proxy", "data_identity": "fixed-market"}
    receipt = {"contract_id": "frozen-target", "source_protocol_verified": True}
    monkeypatch.setattr(candidate_dataset, "load_candidate_training_rows", lambda *a, **kw: (dataset(), dict(receipt)))
    monkeypatch.setattr(protocol, "source_identity", lambda: {})
    folder = tmp_path / "run"
    frozen = protocol.freeze_protocol(folder, configured, {"account": {"mode": "spot"}}, {"account_mode": "spot"}, {})
    assert frozen["training_label"] == "registered_original_candidate_proxy_not_portfolio_marginal"
    assert protocol.validate_run(folder)["training_data_evidence"]["receipt"]["contract_id"] == "frozen-target"
    receipt["contract_id"] = "changed-target"
    with pytest.raises(ValueError, match="training evidence changed"):
        protocol.validate_run(folder)


def test_date_typed_settings_produce_json_safe_metadata_and_stable_calibration_receipts(tmp_path, mocks):
    configured = settings()
    for key in ("start", "end"):
        configured[key] = pd.Timestamp(configured[key]).date()
    configured["splits"] = {key: pd.Timestamp(value).date() for key, value in configured["splits"].items()}
    before = deepcopy(configured)
    protocol.validate_settings(configured)
    scope = protocol.evaluation_contract(configured)
    assert scope["train"]["end"] == "2020-03-01T00:00:00+00:00"
    json.dumps(scope, allow_nan=False)
    models = {"primary": SimpleNamespace(model_id="mock-parent")}
    policy = pipeline.train_policies(tmp_path, {"settings": configured}, {}, dataset(), models)
    checkpoint = FakePolicy.load(tmp_path / "rl_seeds/43/models/policy_best.json")
    count = len(mocks)
    restored = pipeline.calibrate_policy(tmp_path, {"settings": configured}, {}, dataset(), models, checkpoint)
    assert restored.model_id == policy.model_id and len(mocks) == count
    assert configured == before
