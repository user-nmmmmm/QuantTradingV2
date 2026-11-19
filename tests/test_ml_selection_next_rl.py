"""Completed-update budgets and validation-only frozen Bernoulli gates."""
from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.ml_selection import pipeline
from research.ml_selection.models import BernoulliPolicy
from research.ml_selection.selector import ACCOUNT_FEATURES, ResearchSelector
from tests.test_ml_selection_pipeline import market, settings, candidate, context, ScoreModel
from research.ml_selection.dataset import FEATURE_COLUMNS, build_dataset


def harness(monkeypatch, *, options=None, empty_episodes=(), validation_rewards=None):
    configured = settings()
    configured["rl"] = {"enabled": True, "episodes": 6, "seed": 42,
                        "validation_patience": 1, **(options or {})}
    dataset = build_dataset(market(), horizon_bars=3)
    training = pipeline.splits(dataset, configured)["train"].copy()
    for name in ACCOUNT_FEATURES:
        training[name] = 1. if name == "cash_fraction" else 0.
    index = pd.date_range("2020-01-01", periods=3, tz="UTC")
    calls = []
    train_count = 0

    def select(dataset, settings, kind, models, *, policy, deterministic):
        nonlocal train_count
        if not deterministic:
            train_count += 1
            rows = ([] if train_count in empty_episodes else
                    [{"bar_time": index[0], "features": training.iloc[0].to_dict(), "action": True}])
            return SimpleNamespace(trajectory=rows, audit=rows, phase="train", episode=train_count)
        return SimpleNamespace(trajectory=[], audit=[], phase="validation", episode=train_count,
                               threshold=policy.metadata.get("evaluation_threshold", .5))

    def environment(*args, start, end):
        # Any access to test data during checkpoint or threshold choice fails.
        assert end in {configured["splits"]["train_end"], configured["splits"]["validation_end"]}

        def run(selector):
            calls.append((selector.phase, selector.episode, getattr(selector, "threshold", None)))
            value = .1
            if selector.phase == "validation" and validation_rewards:
                value = validation_rewards(selector.episode, selector.threshold)
            return SimpleNamespace(rewards=pd.DataFrame({"reward": [value / 3] * 3}, index=index))

        return SimpleNamespace(run_episode=run)

    monkeypatch.setattr(pipeline, "selection", select)
    monkeypatch.setattr(pipeline, "make_environment", environment)
    monkeypatch.setattr(pipeline, "persist_episode", lambda folder, name, episode, selector:
                        {"reward_sum": float(episode.rewards.reward.sum()), "net_return": 0.,
                         "max_drawdown": 0., "accounting_ok": True})
    monkeypatch.setattr(pipeline, "progress", lambda *args, **kwargs: None)
    return {"settings": configured}, dataset, {"primary": ScoreModel()}, calls


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_minimum_and_delayed_early_stop_count_actual_parameter_steps(tmp_path, monkeypatch):
    frozen, dataset, models, calls = harness(monkeypatch,
        options={"min_updates": 3, "early_stopping_start_updates": 4},
        validation_rewards=lambda episode, threshold: .1 if episode == 1 else -.1)
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    history = read(tmp_path / "rl_training.json")
    receipt = read(tmp_path / "rl_budget_receipt.json")
    assert [row["actual_updates"] for row in history] == [1, 2, 3, 4]
    assert receipt["actual_updates"] == 4 and receipt["minimum_budget_met"]
    assert receipt["stop_reason"] == "validation_early_stop"
    assert receipt["completed_checkpoint"] == "models/policy_004.json"
    assert len([call for call in calls if call[0] == "train"]) == 4
    assert history[-1]["update"]["gradient_norm"] >= 0
    assert history[-1]["probability_after"]["mean_entropy"] > 0
    assert history[-1]["resources"]["wall_seconds"] >= 0
    assert history[-1]["resources"]["ram_peak_scope"] == "process_lifetime"


def test_empty_episodes_never_spend_minimum_updates_or_enable_early_stop(tmp_path, monkeypatch):
    frozen, dataset, models, _ = harness(monkeypatch, options={"episodes": 4, "min_updates": 2},
                                       empty_episodes={1, 2})
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    history = read(tmp_path / "rl_training.json")
    assert [row["actual_updates"] for row in history] == [0, 0, 1, 2]
    receipt = read(tmp_path / "rl_budget_receipt.json")
    assert receipt["actual_updates"] == 2 and receipt["no_action_episode_count"] == 2
    assert receipt["minimum_budget_met"] and receipt["stop_reason"] == "episode_limit_reached"


def test_no_candidates_reports_insufficient_budget_and_legal_cash(tmp_path, monkeypatch):
    frozen, dataset, models, _ = harness(monkeypatch, options={"episodes": 3, "min_updates": 2},
                                       empty_episodes={1, 2, 3})
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    receipt = read(tmp_path / "rl_budget_receipt.json")
    assert receipt["actual_updates"] == 0 and not receipt["minimum_budget_met"]
    assert receipt["budget_status"] == "insufficient_updates"
    assert receipt["stop_reason"] == "no_actionable_candidates"
    assert receipt["cash_is_legal"] and not receipt["forced_trade_reward"]


def test_checkpoint_resume_keeps_completed_update_watermark_and_rejects_corruption(tmp_path, monkeypatch):
    frozen, dataset, models, calls = harness(monkeypatch,
        options={"episodes": 4, "min_updates": 3}, empty_episodes={1})
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    initial_calls = list(calls)
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert calls == initial_calls
    receipt = read(tmp_path / "rl_budget_receipt.json")
    assert receipt["completed_checkpoint_update_count"] == 3
    history = read(tmp_path / "rl_training.json")
    history[2]["actual_updates"] = 99
    pipeline.save_json(tmp_path / "rl_training.json", history)
    with pytest.raises(ValueError, match="history update count mismatch"):
        pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert calls == initial_calls


def test_resume_continues_below_update_minimum_after_tied_rewards(tmp_path, monkeypatch):
    frozen, dataset, models, calls = harness(monkeypatch, options={"episodes": 4, "min_updates": 3})
    # Stop after first committed episode without changing the frozen budget.
    original = pipeline._rl_budget_receipt
    interrupted = False

    def interrupt(folder, history, policy, limits, stale, *, wall_seconds):
        nonlocal interrupted
        result = original(folder, history, policy, limits, stale, wall_seconds=wall_seconds)
        if len(history) == 1 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic cancellation after completed history")
        return result

    monkeypatch.setattr(pipeline, "_rl_budget_receipt", interrupt)
    with pytest.raises(KeyboardInterrupt):
        pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert read(tmp_path / "rl_budget_receipt.json")["actual_updates"] == 3
    assert len([call for call in calls if call[0] == "train"]) == 3


def test_validation_threshold_selection_is_frozen_and_training_sampling_is_unaffected(tmp_path, monkeypatch):
    frozen, dataset, models, calls = harness(monkeypatch,
        options={"episodes": 1, "evaluation_thresholds": [.48, .49, .5, .51]},
        validation_rewards=lambda episode, threshold: .2 if threshold == .49 else 0.)
    result = pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert result.metadata["evaluation_threshold"] == .49
    history = read(tmp_path / "rl_training.json")
    assert set(history[0]["validation_thresholds"]) == {"0.48", "0.49", "0.5", "0.51"}
    assert history[0]["validation"]["evaluation_threshold"] == .49
    assert len([call for call in calls if call[0] == "validation"]) == 4
    assert read(tmp_path / "rl_budget_receipt.json")["test_used_for_selection"] is False
    # Equal seeds and probabilities produce equal stochastic gates regardless of
    # their separately preregistered evaluation thresholds.
    frame = pd.DataFrame({"x": [0., 1., 2.]})
    left = BernoulliPolicy(["x"], seed=8).fit_scaler(frame)
    right = BernoulliPolicy(["x"], seed=8).fit_scaler(frame)
    np.testing.assert_array_equal(left.act(frame, threshold=.01)[0], right.act(frame, threshold=.99)[0])
    assert left.act(frame, deterministic=True, threshold=.49)[0].all()
    assert not right.act(frame, deterministic=True, threshold=.51)[0].any()


def test_selector_uses_frozen_policy_threshold_from_metadata():
    dataset = build_dataset(market(), horizon_bars=3)
    frame = dataset.loc[dataset.eligible].copy()
    for name in ACCOUNT_FEATURES:
        frame[name] = 1. if name == "cash_fraction" else 0.
    policy = BernoulliPolicy((*FEATURE_COLUMNS, *ACCOUNT_FEATURES),
                             metadata={"evaluation_threshold": .49}).fit_scaler(frame)
    # A bias slightly below zero gives p < .5 while preserving p >= .49.
    policy.bias = -.02
    selector = ResearchSelector(dataset, mode="policy", model=ScoreModel(), policy=policy)
    assert len(selector.select([candidate()], **context())) == 1
    policy.metadata["evaluation_threshold"] = .5
    selector = ResearchSelector(dataset, mode="policy", model=ScoreModel(), policy=policy)
    assert selector.select([candidate()], **context()) == []


def test_reward_pilot_registers_all_cells_without_spending_formal_budget_or_opening_test(tmp_path, monkeypatch):
    from research.ml_selection.rl_experiments import run_reward_pilot
    frozen, dataset, models, calls = harness(monkeypatch,
        options={"episodes": 30, "min_updates": 20, "early_stopping_start_updates": 20,
                 "evaluation_thresholds": [.49, .5, .51]})
    before = deepcopy(frozen)
    report = run_reward_pilot(tmp_path, frozen, {}, dataset, models, episodes=2)
    assert frozen == before
    assert report["registered_cells"] == report["completed_cells"] == 9
    assert report["maximum_pilot_updates"] == report["total_actual_updates"] == 18
    assert report["formal_budget_contribution"] == 0 and not report["test_used_for_selection"]
    assert {row["drawdown_penalty"] for row in report["cells"]} == {0., .25, .5}
    assert {row["seed"] for row in report["cells"]} == {42, 43, 44}
    assert all(row["budget"]["episode_limit"] == 2 for row in report["cells"])
    assert len([call for call in calls if call[0] == "train"]) == 18
    assert len([call for call in calls if call[0] == "validation"]) == 54
    assert not (tmp_path / "candidate.json").exists()
    with pytest.raises(ValueError, match="registration changed"):
        run_reward_pilot(tmp_path, frozen, {}, dataset, models, episodes=3)


def test_reward_pilot_keeps_failed_cells_and_continues_remaining_seeds(tmp_path, monkeypatch):
    from research.ml_selection.rl_experiments import run_reward_pilot
    frozen, dataset, models, _ = harness(monkeypatch)
    original = pipeline.train_policy

    def fail_one(folder, protocol, *args):
        if protocol["settings"]["rl"]["seed"] == 43:
            raise RuntimeError("synthetic pilot engine failure")
        return original(folder, protocol, *args)

    monkeypatch.setattr(pipeline, "train_policy", fail_one)
    result = run_reward_pilot(tmp_path, frozen, {}, dataset, models, episodes=1, drawdown_weights=[0.])
    assert result["registered_cells"] == 3 and result["completed_cells"] == 2
    assert result["failed_cells"] == 1 and result["total_actual_updates"] == 2
    failed = next(row for row in result["cells"] if row["status"] == "failed")
    assert failed["seed"] == 43 and failed["error_type"] == "RuntimeError"


@pytest.mark.parametrize("options", [{"episodes": 2, "min_updates": 3}, {"min_updates": True},
    {"evaluation_thresholds": [.49]}, {"evaluation_thresholds": [.5, .5]},
    {"evaluation_threshold": float("nan")}, {"evaluation_thresholds": [.5, -1]}])
def test_invalid_update_or_threshold_budgets_fail_before_environment(options, tmp_path, monkeypatch):
    frozen, dataset, models, calls = harness(monkeypatch, options=options)
    with pytest.raises(ValueError):
        pipeline.train_policy(tmp_path, frozen, {}, dataset, models)
    assert calls == []
