"""Research boundaries and orchestration on small synthetic daily histories."""
from copy import deepcopy
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
import yaml

from core.allocation import EntryCandidate
from core.reproducibility import canonical_frame_csv, sha256_file, sha256_frame
from core.runtime import EventProcessor, MarketDataSlice
from research.ml_selection import pipeline, protocol
from research.ml_selection.dataset import FEATURE_COLUMNS, build_dataset
from research.ml_selection.selector import ResearchSelector
from scripts import train_selector


def market(n=190):
    index = pd.date_range("2020-01-01", periods=n, freq="D")
    close = 100 + np.arange(n) * .25
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                          "close": close, "volume": 100000.0}, index=index)
    return {"A/USDT": frame, "B/USDT": frame.copy()}


def settings():
    index = market()["A/USDT"].index
    return {"schema": "ml-selection-research/v1", "timeframe": "1d", "account_mode": "spot",
            "evaluation_kind": "retrospective", "data_registration": "data.json",
            "baseline_registration": "baseline.json", "start": str(index[0].date()),
            "end": str(index[-1].date()), "splits": {"train_end": str(index[100].date()),
            "validation_end": str(index[145].date())}, "models": ["ridge"],
            "dataset": {"horizon_bars": 3}, "rl": {"enabled": False}}


def candidate(symbol="A/USDT", *, position=80):
    frame = market()["A/USDT"]
    return EntryCandidate(symbol=symbol, strategy=SimpleNamespace(name="OriginalStrategy"),
                          bar_index=position, frame=frame, state="trend",
                          signal={"action": "buy", "stop_loss": 90., "requested_qty": 25.},
                          score=1.5, capital_allocation={"approved_qty": 3.})


def context(position=80):
    frames = market()
    point = frames["A/USDT"].index[position]
    event = MarketDataSlice(point, {s: f.iloc[position] for s, f in frames.items()},
                            frames, timeframe="1d", positions={s: position for s in frames})
    portfolio = SimpleNamespace(cash=10000., positions={}, get_total_value=lambda prices: 10000.)
    broker = SimpleNamespace(orders={}, submit_order=Mock(side_effect=AssertionError("selector submitted order")))
    risk = SimpleNamespace(max_leverage=1., approve=Mock(side_effect=AssertionError("selector sized order")))
    return {"event": event, "portfolio": portfolio, "broker": broker, "risk_manager": risk,
            "current_prices": {s: float(f.close.iloc[position]) for s, f in frames.items()}}


class ScoreModel:
    kind = "ridge"
    model_id = "synthetic-model"
    metadata = {"train_latest_label_available_at": "2020-04-09T00:00:00Z"}

    def __init__(self, value=.03):
        self.value = value
        self.inputs = []

    def predict(self, frame):
        self.inputs.append(frame.copy())
        return np.full(len(frame), self.value)


def test_selector_serving_excludes_labels_and_future_mutation_keeps_decisions():
    dataset = build_dataset(market(), horizon_bars=3)
    mutated = dataset.copy()
    mutated["label_net_return"] = np.arange(len(mutated)) * -10000.
    mutated["label_mae"] = 999999.
    mutated["label_available_at"] = pd.Timestamp("2099-01-01", tz="UTC")
    models = [ScoreModel(), ScoreModel()]
    selectors = [ResearchSelector(dataset, model=models[0]), ResearchSelector(mutated, model=models[1])]
    originals = [candidate(), candidate("B/USDT")]
    outputs = [selector.select(originals, **context()) for selector in selectors]
    assert [(c.symbol, c.score) for c in outputs[0]] == [(c.symbol, c.score) for c in outputs[1]]
    assert selectors[0].audit == selectors[1].audit
    for selector, model in zip(selectors, models):
        assert not any(column.startswith("label_") for column in selector.table.columns)
        assert not any(column.startswith("label_") for column in model.inputs[0].columns)
        assert set(FEATURE_COLUMNS) <= set(model.inputs[0].columns)


def test_missing_or_ineligible_snapshot_is_rejected_before_prediction():
    dataset = build_dataset(market(), horizon_bars=3)
    stamp = context()["event"].timestamp.tz_localize("UTC") + pd.Timedelta(days=1)
    target = (dataset.symbol == "A/USDT") & (dataset.as_of == stamp)
    dataset.loc[target, "eligible"] = False
    dataset.loc[target, "exclusion_reason"] = "history_unavailable"
    model = ScoreModel()
    selector = ResearchSelector(dataset, model=model)
    assert selector.select([candidate(), candidate("UNKNOWN/USDT")], **context()) == []
    assert model.inputs == []
    assert {entry["reason"] for entry in selector.audit} == {"history_unavailable", "missing_causal_snapshot"}


def test_model_gate_changes_only_score_and_never_quantity_or_order_state():
    dataset = build_dataset(market(), horizon_bars=3)
    original = candidate()
    ctx = context()
    before = deepcopy(original.signal)
    selected = ResearchSelector(dataset, model=ScoreModel(.04)).select([original], **ctx)
    assert len(selected) == 1
    assert np.isfinite(selected[0].score) and 0 < selected[0].score <= 5
    assert selected[0].score != original.score
    assert selected[0].strategy is original.strategy
    assert selected[0].frame is original.frame
    assert selected[0].signal is original.signal
    assert selected[0].capital_allocation is original.capital_allocation
    assert original.signal == before and original.score == 1.5
    assert ctx["portfolio"].cash == 10000. and ctx["portfolio"].positions == {}
    ctx["broker"].submit_order.assert_not_called()
    ctx["risk_manager"].approve.assert_not_called()
    gated = ResearchSelector(dataset, model=ScoreModel(-.01)).select([original], **context())
    assert gated == []


def test_runtime_single_symbol_retains_selector_and_allocator_risk_context():
    ctx = context()
    allocator = SimpleNamespace(allocate=Mock())
    selector = ResearchSelector(build_dataset(market(), horizon_bars=3), model=ScoreModel(.04))
    processor = EventProcessor(portfolio=ctx["portfolio"], execution=ctx["broker"],
                               risk_manager=ctx["risk_manager"], state_machine=object(),
                               router=object(), allocator=allocator, candidate_selector=selector)
    processor.last_prices = ctx["current_prices"]
    original = candidate()
    processor._collect_symbol_candidate = Mock(return_value=(original, True))
    assert processor.process_symbol(ctx["event"], original.symbol)
    allocator.allocate.assert_called_once()
    args, kwargs = allocator.allocate.call_args
    assert np.isfinite(args[0][0].score) and args[0][0].score != original.score
    assert args[0][0].signal is original.signal
    assert kwargs["risk_manager"] is ctx["risk_manager"]
    assert kwargs["broker"] is ctx["broker"]
    assert kwargs["portfolio"] is ctx["portfolio"]
    # A risk-blocked collection yields no candidate, so the selector cannot
    # manufacture an entry on the per-symbol runtime path.
    processor._collect_symbol_candidate = Mock(return_value=(None, True))
    allocator.allocate.reset_mock()
    assert processor.process_symbol(ctx["event"], original.symbol, allow_new_entries=False)
    assert processor._collect_symbol_candidate.call_args.kwargs["allow_new_entries"] is False
    allocator.allocate.assert_not_called()


def frozen_run(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("frozen_source = 1\n", encoding="utf-8")
    monkeypatch.setattr(protocol, "ROOT", tmp_path)
    monkeypatch.setattr(protocol, "source_identity", lambda: {"source.py": sha256_file(source)})
    folder = tmp_path / "run"
    frozen = protocol.freeze_protocol(folder, settings(), {}, {}, {"symbols": ["A/USDT"]})
    return folder, frozen, source


def test_frozen_protocol_roundtrip_rejects_configuration_tampering(tmp_path, monkeypatch):
    folder, frozen, _ = frozen_run(tmp_path, monkeypatch)
    assert protocol.validate_run(folder)["protocol_id"] == frozen["protocol_id"]
    assert not frozen["live_orders"] and not frozen["formal_admission"]
    assert not frozen["final_holdout_opened"]
    altered = deepcopy(frozen)
    altered["settings"]["dataset"]["horizon_bars"] = 999
    protocol.save_json(folder / "protocol.json", altered)
    with pytest.raises(ValueError, match="protocol was changed"):
        protocol.validate_run(folder)


def test_frozen_protocol_rejects_source_and_artifact_tampering(tmp_path, monkeypatch):
    folder, _, source = frozen_run(tmp_path, monkeypatch)
    source.write_text("frozen_source = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        protocol.validate_run(folder)
    source.write_text("frozen_source = 1\n", encoding="utf-8")
    artifact = folder / "dataset.csv"
    artifact.write_text("frozen data\n", encoding="utf-8")
    pipeline.artifact_manifest(folder)
    protocol.validate_run(folder)
    artifact.write_text("altered data\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        protocol.validate_run(folder)


def test_artifact_manifest_cannot_redirect_outside_run_folder(tmp_path, monkeypatch):
    folder, _, source = frozen_run(tmp_path, monkeypatch)
    protocol.save_json(folder / "artifacts.json", {"../source.py": sha256_file(source)})
    with pytest.raises(ValueError, match="artifact changed"):
        protocol.validate_run(folder)


def registrations(tmp_path):
    frames = market()
    sources, identities = {}, {}
    for symbol, frame in frames.items():
        relative = "input/engine/" + symbol.replace("/", "_") + ".csv"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_frame_csv(frame), encoding="utf-8")
        sources[relative], identities[symbol] = sha256_file(path), sha256_frame(frame)
    registration = {"symbols": list(frames), "input_files": sources, "engine_frame_hashes": identities}
    protocol.save_json(tmp_path / "data.json", registration)
    parameters = {"account": {"mode": "perpetual"}, "execution": {"fee_schedule": {"market_type": "perpetual"}}}
    baseline = {"arms": {"smart": {"parameters": parameters, "engine_options": {"initial_capital": 1000}}}}
    protocol.save_json(tmp_path / "baseline.json", baseline)
    configured = settings()
    configured.update(data_registration=str(tmp_path / "data.json"),
                      baseline_registration=str(tmp_path / "baseline.json"))
    return configured, frames


def test_registered_input_loading_checks_bytes_frame_identity_and_spot_adaptation(tmp_path):
    configured, expected = registrations(tmp_path)
    frames, parameters, options, evidence = protocol.load_inputs(configured)
    pd.testing.assert_frame_equal(frames["A/USDT"].sort_index(axis=1), expected["A/USDT"].sort_index(axis=1), check_freq=False,
                                 check_names=False, check_dtype=False)
    assert parameters["account"]["mode"] == "spot"
    assert parameters["execution"]["fee_schedule"]["market_type"] == "spot"
    assert options["terminal_policy"] == "forced_liquidation"
    assert evidence["membership_basis"] == "observed_history_only"
    assert not evidence["historical_universe_verified"] and not evidence["independent_holdout"]
    path = tmp_path / "input/engine/A_USDT.csv"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen input hash mismatch"):
        protocol.load_inputs(configured)


def test_engine_frame_identity_must_match_even_when_csv_digest_is_valid(tmp_path):
    configured, _ = registrations(tmp_path)
    path = tmp_path / "data.json"
    registration = json.loads(path.read_text(encoding="utf-8"))
    registration["engine_frame_hashes"]["A/USDT"] = "0" * 64
    protocol.save_json(path, registration)
    with pytest.raises(ValueError, match="engine frame identity mismatch"):
        protocol.load_inputs(configured)


def test_prepare_roundtrip_and_supervised_training_only_mature_partitions(tmp_path, monkeypatch):
    configured, _ = registrations(tmp_path)
    monkeypatch.setattr(protocol, "source_identity", lambda: {})
    folder = tmp_path / "research"
    frozen, _, dataset = pipeline.prepare(configured, folder)
    loaded = pipeline.load_dataset(folder)
    assert len(loaded) == len(dataset)
    partitions = pipeline.splits(loaded, configured)
    train_end = pd.Timestamp(configured["splits"]["train_end"], tz="UTC")
    valid_end = pd.Timestamp(configured["splits"]["validation_end"], tz="UTC")
    assert (partitions["train"].label_available_at < train_end).all()
    assert (partitions["validation"].label_available_at < valid_end).all()
    captured = {}

    class Trained(ScoreModel):
        metadata = {"train_latest_label_available_at": partitions["train"].label_available_at.max().isoformat()}

        def save(self, path):
            protocol.save_json(path, {"synthetic": True})

    def fit(train, validation, **kwargs):
        captured["train"], captured["validation"] = train.copy(), validation.copy()
        assert tuple(kwargs["features"]) == FEATURE_COLUMNS
        return Trained()

    monkeypatch.setattr(pipeline, "fit_model", fit)
    models = pipeline.supervised(folder, frozen, loaded)
    assert list(models) == ["ridge"]
    pd.testing.assert_frame_equal(captured["train"], partitions["train"])
    pd.testing.assert_frame_equal(captured["validation"], partitions["validation"])
    assert not set(captured["train"].as_of) & set(partitions["test"].as_of)
    assert (folder / "supervised_validation.json").exists()


def test_matrix_rejects_model_fitted_at_or_after_evaluation_boundary(tmp_path):
    configured = settings()
    model = ScoreModel()
    model.metadata = {"train_latest_label_available_at": configured["splits"]["train_end"]}
    with pytest.raises(ValueError, match="labels matured"):
        pipeline.matrix(tmp_path, {"settings": configured}, market(),
                        build_dataset(market(), horizon_bars=3), {"ridge": model}, segment="validation")


def test_candidate_is_frozen_from_validation_before_policy_and_test(tmp_path, monkeypatch):
    configured = settings()
    configured["models"] = ["ridge", "lightgbm"]
    models = {"ridge": ScoreModel(), "lightgbm": ScoreModel()}
    models["ridge"].model_id = "ridge-id"
    models["lightgbm"].kind = "lightgbm"
    models["lightgbm"].model_id = "lightgbm-id"
    monkeypatch.setattr(pipeline, "prepare", lambda *args: ({"settings": configured}, {}, pd.DataFrame()))
    monkeypatch.setattr(pipeline, "supervised", lambda *args: models)
    segments = []

    def matrix(*args, segment, **kwargs):
        segments.append(segment)
        return {"native": validation_result(.001, .001),
                "ridge": validation_result(.01, .01),
                "lightgbm": validation_result(.02, .02)}

    def inspect_parent(*args):
        choice = json.loads((tmp_path / "parent_model.json").read_text(encoding="utf-8"))
        assert choice["primary_model"] == "lightgbm"
        assert choice["test_used_for_selection"] is False
        assert models["primary"] is models["lightgbm"]
        assert not (tmp_path / "candidate.json").exists()
        return None

    def inspect_candidate(*args):
        choice = json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8"))
        assert choice["primary_model"] == "lightgbm"
        assert choice["selected_candidate"] == "lightgbm"
        assert choice["model_id"] == "lightgbm-id"
        assert choice["parent_model_id"] == "lightgbm-id"
        assert choice["test_used_for_selection"] is False
        assert choice["validation_qualification_passed"] is True
        assert models["primary"] is models["lightgbm"]
        return None

    monkeypatch.setattr(pipeline, "matrix", matrix)
    monkeypatch.setattr(pipeline, "train_policies", inspect_parent)
    monkeypatch.setattr(pipeline, "evaluate", lambda *args: inspect_candidate(*args) or {"status": "research"})
    monkeypatch.setattr(pipeline, "walk_forward", lambda *args: [])
    monkeypatch.setattr(pipeline, "artifact_manifest", lambda *args: None)
    monkeypatch.setattr(pipeline, "progress", lambda *args, **kwargs: None)
    old_disable = logging.root.manager.disable
    try:
        assert pipeline.full_run(configured, tmp_path) == {"status": "research"}
        assert logging.root.manager.disable == old_disable
    finally:
        logging.disable(old_disable)
    assert segments == ["validation"]


def validation_result(reward, net_return, *, max_drawdown=.02, accounting_ok=True):
    return {"reward_sum": reward, "net_return": net_return,
            "max_drawdown": max_drawdown, "accounting_ok": accounting_ok}


@pytest.mark.parametrize("rl_reward,rl_return", [(0.005, .01), (0.5, 0.)])
def test_final_candidate_rejects_worse_rl_or_cash_under_positive_return_gate(tmp_path, rl_reward, rl_return):
    configured = settings()
    configured["gates"] = {"require_positive_net_return": True, "max_drawdown": .15}
    parent = ScoreModel()
    parent.model_id = "ridge-id"
    models = {"ridge": parent, "primary": parent}
    validation = {"native": validation_result(-.01, -.01), "ridge": validation_result(.02, .025)}
    policy = SimpleNamespace(model_id="rl-id")
    protocol.save_json(tmp_path / "rl_training.json", [{"episode": 1, "trajectory_actions": 20,
                      "validation": validation_result(rl_reward, rl_return)}])
    frozen = pipeline.freeze_candidate(tmp_path, {"settings": configured}, models, validation, policy)
    assert frozen["selected_candidate"] == "ridge"
    assert frozen["validation_qualification_passed"] is True
    assert frozen["model_id"] == "ridge-id"
    assert frozen["test_used_for_selection"] is False
    assert pipeline.freeze_candidate(tmp_path, {"settings": configured}, models, validation, policy) == frozen
    protocol.save_json(tmp_path / "rl_training.json", [{"episode": 2, "trajectory_actions": 20,
                      "validation": validation_result(.1, .1)}])
    with pytest.raises(ValueError, match="already frozen"):
        pipeline.freeze_candidate(tmp_path, {"settings": configured}, models, validation, policy)


def test_cash_gate_reaches_real_engine_and_empty_concentration_without_crash(tmp_path):
    from tests.test_ml_selection_environment import OneUnitHold, parameters
    frames = {"A/USDT": market(170)["A/USDT"]}
    selector = ResearchSelector(build_dataset(frames, horizon_bars=3), model=ScoreModel(-.01))
    env = pipeline.FullEngineEnvironment(frames, parameters=parameters(),
        engine_options={"initial_capital": 1000., "warmup_period": 0, "slippage": 0.,
                        "terminal_policy": "forced_liquidation"},
        strategies={"OneUnitHold": OneUnitHold(entry_index=80)})
    episode = env.run_episode(selector)
    assert any(row["reason"] == "model_gate" for row in selector.audit)
    assert episode.result["trades"] == []
    assert episode.summary["net_return"] == 0
    np.testing.assert_array_equal(episode.result["equity_curve"].equity, 1000.)
    summary = pipeline.persist_episode(tmp_path, "cash", episode, selector)
    assert summary["selected_count"] == 0
    trades = pd.read_csv(tmp_path / "episodes/cash/trades.csv")
    assert trades.empty and "qty" in trades
    assert pipeline.concentration_check(tmp_path, "cash") == {
        "status": "insufficient", "reason": "no_realized_roundtrip_records"}
    empty = tmp_path / "episodes/empty"
    empty.mkdir()
    (empty / "closed_positions.csv").write_text("", encoding="utf-8")
    assert pipeline.concentration_check(tmp_path, "empty")["status"] == "insufficient"
    assert pipeline.concentration_check(tmp_path, "missing")["status"] == "insufficient"


def committed_policy_fixture(tmp_path, *, episodes=2, committed_count=1, legacy=False):
    configured = settings()
    configured["rl"] = {"enabled": True, "episodes": episodes, "seed": 42,
                        "validation_patience": 10, "learning_rate": .02}
    dataset = build_dataset(market(), horizon_bars=3)
    training = pipeline.splits(dataset, configured)["train"].copy()
    for name in pipeline.ACCOUNT_FEATURES:
        training[name] = 1.0 if name == "cash_fraction" else 0.0
    parent = ScoreModel()
    saved, history = [], []
    for number in range(1, committed_count + 1):
        policy = pipeline.BernoulliPolicy((*FEATURE_COLUMNS, *pipeline.ACCOUNT_FEATURES), seed=42)
        policy.fit_scaler(training)
        policy.metadata["parent_model_id"] = parent.model_id
        policy.bias, policy.update_count = .1 * number, number
        policy.rng.random(number + 3)
        policy.save(tmp_path / "models" / f"policy_{number:03d}.json")
        saved.append(policy)
        row = {"episode": number, "baseline": .4, "validation": validation_result(.1, .1),
               "train": {}, "trajectory_actions": 1, "update": {}}
        if not legacy:
            row["checkpoint_model_id"] = policy.model_id
        history.append(row)
    protocol.save_json(tmp_path / "rl_training.json", history)
    # Simulate cancellation after next policy was written but before history
    # committed it. Its parent is valid, so a parent-only check cannot detect it.
    ahead = pipeline.BernoulliPolicy((*FEATURE_COLUMNS, *pipeline.ACCOUNT_FEATURES), seed=42)
    ahead.fit_scaler(training)
    ahead.metadata["parent_model_id"] = parent.model_id
    ahead.bias, ahead.update_count = 9., 99
    ahead.rng.random(99)
    ahead.save(tmp_path / "models/policy_latest.json")
    ahead.save(tmp_path / "models/policy_best.json")
    return {"settings": configured}, dataset, {"primary": parent}, saved, training


def test_policy_resume_ignores_ahead_aliases_and_commits_only_next_completed_episode(tmp_path, monkeypatch):
    frozen, dataset, models, saved, training = committed_policy_fixture(tmp_path)
    captured = []
    initial_rng = deepcopy(saved[-1].rng.bit_generator.state)
    index = pd.date_range("2020-01-01", periods=3, tz="UTC")

    def select(dataset, settings, kind, models, *, policy, deterministic):
        if not deterministic:
            captured.append((policy.model_id, policy.update_count, deepcopy(policy.rng.bit_generator.state)))
            trajectory = [{"bar_time": index[0], "features": training.iloc[0].to_dict(), "action": True}]
        else:
            trajectory = []
        return SimpleNamespace(trajectory=trajectory)

    episode = SimpleNamespace(rewards=pd.DataFrame({"reward": [.01, .01, .01]}, index=index))
    monkeypatch.setattr(pipeline, "selection", select)
    monkeypatch.setattr(pipeline, "make_environment", lambda *args, **kwargs:
                        SimpleNamespace(run_episode=lambda selector: episode))
    monkeypatch.setattr(pipeline, "persist_episode", lambda *args:
                        validation_result(.03, .03))
    monkeypatch.setattr(pipeline, "progress", lambda *args, **kwargs: None)
    result = pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert captured == [(saved[-1].model_id, 1, initial_rng)]
    assert result.model_id == saved[0].model_id
    history = json.loads((tmp_path / "rl_training.json").read_text(encoding="utf-8"))
    assert [row["episode"] for row in history] == [1, 2]
    completed = pipeline.BernoulliPolicy.load(tmp_path / "models/policy_002.json")
    assert completed.update_count == 2
    assert history[-1]["checkpoint_model_id"] == completed.model_id
    assert pipeline.BernoulliPolicy.load(tmp_path / "models/policy_latest.json").model_id == completed.model_id
    assert pipeline.BernoulliPolicy.load(tmp_path / "models/policy_best.json").model_id == saved[0].model_id


@pytest.mark.parametrize("legacy", [False, True])
def test_exhausted_policy_budget_repairs_aliases_from_committed_numbered_models(tmp_path, monkeypatch, legacy):
    frozen, dataset, models, saved, _ = committed_policy_fixture(tmp_path, committed_count=2, legacy=legacy)
    run = Mock(side_effect=AssertionError("exhausted budget must not rerun environment"))
    monkeypatch.setattr(pipeline, "make_environment", run)
    original_checkpoints = [(tmp_path / "models" / f"policy_{i:03d}.json").read_bytes() for i in (1, 2)]
    result = pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert result.model_id == saved[0].model_id  # Equal rewards keep first best.
    latest = pipeline.BernoulliPolicy.load(tmp_path / "models/policy_latest.json")
    assert latest.model_id == saved[-1].model_id
    assert latest.rng.bit_generator.state == saved[-1].rng.bit_generator.state
    assert pipeline.BernoulliPolicy.load(tmp_path / "models/policy_best.json").model_id == saved[0].model_id
    assert original_checkpoints == [(tmp_path / "models" / f"policy_{i:03d}.json").read_bytes() for i in (1, 2)]
    run.assert_not_called()


@pytest.mark.parametrize("damage,match", [
    ("missing", "checkpoint is missing"), ("identity", "checkpoint identity mismatch"),
    ("parent", "parent model changed"), ("sequence", "contiguous"),
])
def test_policy_resume_rejects_invalid_committed_checkpoint_without_using_alias(tmp_path, monkeypatch, damage, match):
    frozen, dataset, models, saved, _ = committed_policy_fixture(tmp_path)
    path = tmp_path / "models/policy_001.json"
    history = json.loads((tmp_path / "rl_training.json").read_text(encoding="utf-8"))
    if damage == "missing":
        path.unlink()
    elif damage == "identity":
        history[0]["checkpoint_model_id"] = "0" * 64
    elif damage == "parent":
        saved[0].metadata["parent_model_id"] = "different-parent"
        saved[0].save(path)
    else:
        history[0]["episode"] = 2
    protocol.save_json(tmp_path / "rl_training.json", history)
    run = Mock(side_effect=AssertionError("invalid checkpoint must not train"))
    monkeypatch.setattr(pipeline, "make_environment", run)
    with pytest.raises(ValueError, match=match):
        pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    run.assert_not_called()


def test_policy_resume_preserves_early_stop_watermark_when_rewards_tie(tmp_path, monkeypatch):
    frozen, dataset, models, _, _ = committed_policy_fixture(tmp_path, episodes=4, committed_count=2)
    frozen["settings"]["rl"]["validation_patience"] = 1
    run = Mock(side_effect=AssertionError("committed tied reward already reached patience"))
    monkeypatch.setattr(pipeline, "make_environment", run)
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    run.assert_not_called()


def test_concentration_uses_completed_attempt_directory_instead_of_partial_original(tmp_path):
    original = tmp_path / "episodes/test_ridge"
    original.mkdir(parents=True)
    (original / "closed_positions.csv").write_text("", encoding="utf-8")
    actual = tmp_path / "episodes/test_ridge_attempt_1"
    actual.mkdir()
    pd.DataFrame({"entry_time": pd.date_range("2020-01-01", periods=6, freq="10D"),
                  "net_pnl": [1., 2., 3., 4., 5., 6.]}).to_csv(actual / "closed_positions.csv", index=False)
    summary = {"artifact_directory": "episodes/test_ridge_attempt_1"}
    assert pipeline.concentration_check(tmp_path, "test_ridge")["status"] == "insufficient"
    result = pipeline.concentration_check(tmp_path, "test_ridge", summary)
    assert result["status"] == "ok" and result["cohort_count"] == 6
    assert result["net_pnl"] == 21. and result["remaining_after_top_5"] == 1.
    assert pipeline.concentration_check(tmp_path, "test_ridge", artifact_directory=summary["artifact_directory"]) == result


@pytest.mark.parametrize("source", ["summary", "explicit", "legacy_arm"])
def test_concentration_rejects_artifact_path_outside_run_before_read(tmp_path, monkeypatch, source):
    read = Mock(side_effect=AssertionError("outside path must not be read"))
    monkeypatch.setattr(pipeline.pd, "read_csv", read)
    with pytest.raises(ValueError, match="within the research run"):
        if source == "summary":
            pipeline.concentration_check(tmp_path, "test_ridge", {"artifact_directory": "../outside"})
        elif source == "explicit":
            pipeline.concentration_check(tmp_path, "test_ridge", artifact_directory=tmp_path.parent)
        else:
            pipeline.concentration_check(tmp_path, "../../outside")
    read.assert_not_called()


@pytest.mark.parametrize("change,match", [
    ({"evaluation_kind": "final"}, "independent final"),
    ({"timeframe": "1h"}, "daily spot"),
    ({"account_mode": "perpetual"}, "daily spot"),
    ({"splits": {"train_end": "2020-05-25", "validation_end": "2020-04-10"}}, "time ordered"),
    ({"models": ["ridge", "ridge"]}, "distinct"),
])
def test_configuration_rejects_invalid_scope_final_claim_and_boundary_order(tmp_path, change, match):
    configured = settings()
    configured.update(change)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(configured), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        protocol.load_settings(path)


def test_cli_validates_before_preparing_and_rejects_final_or_frozen_overrides(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    configured = settings()
    configured["evaluation_kind"] = "final"
    path.write_text(yaml.safe_dump(configured), encoding="utf-8")
    prepare = Mock()
    monkeypatch.setattr(pipeline, "prepare", prepare)
    with pytest.raises(ValueError, match="independent final"):
        train_selector.main(["--stage", "prepare", "--config", str(path), "--run-dir", str(tmp_path / "run")])
    prepare.assert_not_called()
    with pytest.raises(SystemExit) as final:
        train_selector.main(["--stage", "final"])
    assert final.value.code == 2
    with pytest.raises(SystemExit) as frozen:
        train_selector.main(["--stage", "supervised", "--run-dir", str(tmp_path / "run"), "--max-symbols", "1"])
    assert frozen.value.code == 2


def test_cli_prepare_applies_explicit_small_run_options_before_freeze(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings()), encoding="utf-8")
    prepare, manifest = Mock(), Mock()
    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(pipeline, "artifact_manifest", manifest)
    folder = tmp_path / "small"
    assert train_selector.main(["--stage", "prepare", "--config", str(path), "--run-dir", str(folder),
                                "--max-symbols", "1", "--rl-episodes", "2"]) == 0
    prepared, target = prepare.call_args.args
    assert prepared["max_symbols"] == 1 and prepared["rl"]["episodes"] == 2
    assert target == folder
    manifest.assert_called_once_with(folder)


def test_stale_shadow_never_scores_or_claims_mature_outcome_and_cannot_rewrite(tmp_path, monkeypatch):
    configured = settings()
    frozen = {"settings": configured, "protocol_id": "frozen-id", "data_evidence": {"symbols": ["A/USDT", "B/USDT"]}}
    protocol.save_json(tmp_path / "candidate.json", {"primary_model": "ridge"})
    model = ScoreModel()
    monkeypatch.setattr(pipeline, "load_model", lambda *args: model)
    monkeypatch.setattr(pipeline, "load_inputs", lambda *args: (market(), {}, {}, {}))
    now = "2021-01-01T12:00:00Z"
    result = pipeline.shadow(tmp_path, frozen, as_of=now)
    assert result["status"] == "awaiting_fresh_market_data"
    assert result["source_kind"] == "frozen_historical_data"
    assert not result["real_orders"] and not result["formal_admission"]
    assert model.inputs == []
    for observation in result["observations"]:
        assert not observation["eligible"]
        assert observation["predicted_value"] is None
        assert observation["label_available_at"] is None
        assert observation["outcome_status"] == "pending_future_data"
    files = list((tmp_path / "shadow").glob("*.json"))
    assert len(files) == 1
    original_bytes = files[0].read_bytes()
    second = pipeline.shadow(tmp_path, frozen, as_of=now)
    assert second["information_cutoff"] == result["information_cutoff"]
    assert second["observed_at"] > result["observed_at"]
    assert len(list((tmp_path / "shadow").glob("*.json"))) == 2
    assert files[0].read_bytes() == original_bytes


def test_forward_shadow_filters_open_and_future_candles(tmp_path, monkeypatch):
    frames = market()
    input_dir = tmp_path / "forward"
    input_dir.mkdir()
    for symbol, frame in frames.items():
        frame.to_csv(input_dir / (symbol.replace("/", "_") + ".csv"), index_label="timestamp")
    frozen = {"settings": settings(), "protocol_id": "frozen-id", "data_evidence": {"symbols": list(frames)}}
    protocol.save_json(tmp_path / "candidate.json", {"primary_model": "ridge"})
    model = ScoreModel()
    monkeypatch.setattr(pipeline, "load_model", lambda *args: model)
    now = frames["A/USDT"].index[160].tz_localize("UTC") + pd.Timedelta(hours=12)
    result = pipeline.shadow(tmp_path, frozen, market_data_dir=input_dir, as_of=now)
    assert result["status"] == "retrospective_scoring"
    assert not result["formal_admission"]
    assert pd.Timestamp(result["information_cutoff"]) == now
    assert pd.Timestamp(result["observed_at"]) > now
    assert result["diagnostic_scoring_only"]
    assert all(len(digest) == 64 for digest in result["source_hashes"].values())
    for observation in result["observations"]:
        assert pd.Timestamp(observation["as_of"]) == now.normalize()
        assert observation["label_available_at"] is None
        assert observation["outcome_status"] == "pending_future_data"
    for frame in model.inputs:
        assert not any(name.startswith("label_") for name in frame.columns)
        expected_return = frames["A/USDT"].close.iloc[159] / frames["A/USDT"].close.iloc[139] - 1
        assert frame["return_20d"].iloc[0] == pytest.approx(expected_return)


def test_shadow_cannot_apply_model_before_training_labels_mature(tmp_path, monkeypatch):
    frames = market()
    frozen = {"settings": settings(), "protocol_id": "frozen-id", "data_evidence": {"symbols": list(frames)}}
    protocol.save_json(tmp_path / "candidate.json", {"primary_model": "ridge"})
    model = ScoreModel()
    model.metadata = {"train_latest_label_available_at": frames["A/USDT"].index[170].isoformat()}
    monkeypatch.setattr(pipeline, "load_model", lambda *args: model)
    monkeypatch.setattr(pipeline, "load_inputs", lambda *args: (frames, {}, {}, {}))
    cutoff = frames["A/USDT"].index[160].tz_localize("UTC") + pd.Timedelta(hours=12)
    with pytest.raises(ValueError, match="(?i)(train|matur|model|cutoff)"):
        pipeline.shadow(tmp_path, frozen, as_of=cutoff)
    assert model.inputs == []


def shadow_resolution_fixture(tmp_path, monkeypatch, *, candidate_kind="ridge"):
    frames = market()
    for frame in frames.values():
        frame["available_at"] = frame.index + pd.Timedelta(days=1)
        # Entry bar exists in the supplied file before its explicitly delayed
        # OHLCV availability. The label cannot mature at the nominal horizon.
        frame.loc[frame.index[80], "available_at"] = frame.index[84]
    input_dir = tmp_path / "forward"
    input_dir.mkdir()
    for symbol, frame in frames.items():
        frame.to_csv(input_dir / (symbol.replace("/", "_") + ".csv"), index_label="timestamp")
    frozen = {"settings": settings(), "protocol_id": "frozen-id", "data_evidence": {"symbols": list(frames)}}
    protocol.save_json(tmp_path / "candidate.json", {"primary_model": "ridge", "selected_candidate": candidate_kind})
    model = ScoreModel()
    model.metadata = {"train_latest_label_available_at": frames["A/USDT"].index[65].isoformat()}
    monkeypatch.setattr(pipeline, "load_model", lambda *args: model)
    cutoff = frames["A/USDT"].index[80].tz_localize("UTC") + pd.Timedelta(hours=12)
    observation = pipeline.shadow(tmp_path, frozen, market_data_dir=input_dir, as_of=cutoff)
    source = next((tmp_path / "shadow").glob("observation_*.json"))
    return frames, input_dir, frozen, observation, source


@pytest.mark.parametrize("candidate_kind", ["ridge", "rl"])
def test_shadow_outcomes_wait_for_actual_availability_and_preserve_original_replay(tmp_path, monkeypatch, candidate_kind):
    frames, input_dir, frozen, observation, source = shadow_resolution_fixture(
        tmp_path, monkeypatch, candidate_kind=candidate_kind)
    original = source.read_bytes()
    assert all(item["eligible"] for item in observation["observations"])
    pending = pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir,
        as_of=frames["A/USDT"].index[83].tz_localize("UTC") + pd.Timedelta(hours=12))
    assert pending["resolved"] == [] and pending["pending_labels"] == 2
    assert source.read_bytes() == original
    mature_cutoff = frames["A/USDT"].index[84].tz_localize("UTC")
    resolved = pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir, as_of=mature_cutoff)
    assert resolved["pending_labels"] == 0 and len(resolved["resolved"]) == 2
    assert not resolved["formal_admission"]
    assert source.read_bytes() == original
    assert len(list((tmp_path / "shadow").glob("outcomes_*.json"))) == 2
    truth = build_dataset(frames, **frozen["settings"]["dataset"])
    for item in resolved["resolved"]:
        assert item["observation_id"] == observation["observation_id"]
        assert item["predicted_value"] == .03
        assert item["label_available_at"] == mature_cutoff
        assert item["evidence_kind"] == "retrospective"
        assert item["label_basis"] == "independent_shadow_proxy_not_portfolio"
        expected = truth.loc[(truth.symbol == item["symbol"])
                              & (truth.as_of == pd.Timestamp(item["as_of"]))].iloc[0]
        assert item["label_net_return"] == pytest.approx(expected.label_net_return)


def test_shadow_resolver_rejects_revised_recorded_feature_history(tmp_path, monkeypatch):
    frames, input_dir, frozen, _, source = shadow_resolution_fixture(tmp_path, monkeypatch)
    original = source.read_bytes()
    revised = frames["A/USDT"].copy()
    revised.loc[revised.index[79], ["open", "high", "low", "close"]] *= 1.2
    revised.to_csv(input_dir / "A_USDT.csv", index_label="timestamp")
    with pytest.raises(ValueError, match="feature history was revised"):
        pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir,
                                as_of=frames["A/USDT"].index[84])
    assert source.read_bytes() == original
    assert not list((tmp_path / "shadow").glob("outcomes_*.json"))


def test_actual_forward_proxy_starts_after_observation_and_waits_for_availability(tmp_path, monkeypatch):
    frames = market()
    index = frames["A/USDT"].index
    observed_at = index[150].tz_localize("UTC") + pd.Timedelta(hours=12)
    for frame in frames.values():
        frame["available_at"] = frame.index + pd.Timedelta(days=1)
        frame.loc[index[152], "available_at"] = index[155]
    input_dir = tmp_path / "forward"
    input_dir.mkdir()
    for symbol, frame in frames.items():
        frame.to_csv(input_dir / (symbol.replace("/", "_") + ".csv"), index_label="timestamp")
    frozen = {"settings": settings(), "protocol_id": "frozen-id", "created_at": index[140].isoformat(),
              "data_evidence": {"symbols": list(frames)}}
    protocol.save_json(tmp_path / "candidate.json", {"primary_model": "ridge", "selected_candidate": "ridge"})
    monkeypatch.setattr(pipeline, "load_model", lambda *args: ScoreModel())
    monkeypatch.setattr(pipeline, "datetime", SimpleNamespace(now=lambda *args: observed_at.to_pydatetime()))
    observation = pipeline.shadow(tmp_path, frozen, market_data_dir=input_dir)
    assert observation["status"] == "forward_diagnostic_scores_recorded"
    assert observation["retrospective_observation"] is False
    source = next((tmp_path / "shadow").glob("observation_*.json"))
    original = source.read_bytes()
    for item in observation["observations"]:
        assert pd.Timestamp(item["entry_not_before"]) == observed_at
        assert item["reference_close"] == pytest.approx(frames[item["symbol"]].close.iloc[149])
        assert item["decision_atr_absolute"] > 0
    cutoff = index[154].tz_localize("UTC")
    monkeypatch.setattr(pipeline, "datetime", SimpleNamespace(now=lambda *args: cutoff.to_pydatetime()))
    pending = pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir)
    assert not pending["resolved"] and pending["pending_labels"] == 2
    cutoff = index[155].tz_localize("UTC")
    monkeypatch.setattr(pipeline, "datetime", SimpleNamespace(now=lambda *args: cutoff.to_pydatetime()))
    resolved = pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir)
    assert resolved["pending_labels"] == 0 and len(resolved["resolved"]) == 2
    for item in resolved["resolved"]:
        assert item["evidence_kind"] == "forward_diagnostic"
        assert pd.Timestamp(item["entry_time"]) == index[151].tz_localize("UTC")
        assert pd.Timestamp(item["label_available_at"]) == cutoff
        frame = frames[item["symbol"]]
        expected = frame.close.iloc[153] * (1 - .0005) * (1 - .001) / (
            frame.open.iloc[151] * (1 + .0005) * (1 + .001)) - 1
        assert item["label_net_return"] == pytest.approx(expected)
    assert source.read_bytes() == original


@pytest.mark.parametrize("field", ["observation_id", "source_hashes", "features"])
def test_shadow_resolver_rejects_record_and_hash_tampering(tmp_path, monkeypatch, field):
    frames, input_dir, frozen, _, source = shadow_resolution_fixture(tmp_path, monkeypatch)
    altered = json.loads(source.read_text(encoding="utf-8"))
    if field == "observation_id":
        altered[field] = "0" * 64
    elif field == "source_hashes":
        altered[field]["A/USDT"] = "0" * 64
    else:
        altered["observations"][0]["features"]["return_20d"] = 999.
    protocol.save_json(source, altered)
    with pytest.raises(ValueError, match="changed after recording"):
        pipeline.resolve_shadow(tmp_path, frozen, market_data_dir=input_dir,
                                as_of=frames["A/USDT"].index[84])
    assert not list((tmp_path / "shadow").glob("outcomes_*.json"))


def test_walk_forward_retrains_each_window_with_its_own_mature_history(tmp_path, monkeypatch):
    frames = market()
    dataset = build_dataset(frames, horizon_bars=3)
    configured = settings()
    index = frames["A/USDT"].index
    configured["walk_forward"] = [
        {"train_end": str(index[100].date()), "validation_end": str(index[125].date()), "test_end": str(index[145].date())},
        {"train_end": str(index[125].date()), "validation_end": str(index[155].date()), "test_end": str(index[180].date())},
    ]
    configured["random_seeds"] = []
    untouched = deepcopy(configured)
    fitted, windows = [], []

    class Trained(ScoreModel):
        def save(self, path):
            protocol.save_json(path, {"synthetic": True})

    def fit(train, validation, **kwargs):
        window = configured["walk_forward"][len(fitted)]
        end = pd.Timestamp(window["train_end"], tz="UTC")
        valid_end = pd.Timestamp(window["validation_end"], tz="UTC")
        assert len(train) and len(validation)
        assert (train.as_of < end).all() and (train.label_available_at < end).all()
        assert (validation.as_of >= end).all() and (validation.label_available_at < valid_end).all()
        fitted.append(train.copy())
        model = Trained()
        model.metadata = {"train_latest_label_available_at": train.label_available_at.max().isoformat()}
        return model

    def environment(frames, current, *, start, end):
        windows.append((pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")))
        return SimpleNamespace(run_episode=lambda selector: SimpleNamespace())

    monkeypatch.setattr(pipeline, "fit_model", fit)
    monkeypatch.setattr(pipeline, "make_environment", environment)
    monkeypatch.setattr(pipeline, "persist_episode", lambda folder, name, *args:
                        validation_result(.01 if "ridge" in name else 0., .01 if "ridge" in name else 0.))
    monkeypatch.setattr(pipeline, "progress", lambda *args, **kwargs: None)
    results = pipeline.walk_forward(tmp_path, {"settings": configured}, frames, dataset)
    assert len(results) == len(fitted) == 2
    assert fitted[0].label_available_at.max() < fitted[1].label_available_at.max()
    assert configured == untouched
    for i, window in enumerate(configured["walk_forward"], 1):
        path = tmp_path / "walk_forward" / f"window_{i:02d}" / "candidate.json"
        frozen = json.loads(path.read_text(encoding="utf-8"))
        assert frozen["candidate"] == "ridge" and not frozen["test_used_for_selection"]
        assert (pd.Timestamp(window["validation_end"], tz="UTC"), pd.Timestamp(window["test_end"], tz="UTC")) in windows
