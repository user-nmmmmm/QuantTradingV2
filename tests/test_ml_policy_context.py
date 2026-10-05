"""Opt-in decision context with fixed test facts only; no fits or training."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from research.ml_selection.policy_context import (
    POLICY_CONTEXT_FEATURES, PolicyContextUnavailable, build_policy_context, decision_context_snapshot,
)
from research.ml_selection.selector import ResearchSelector
from tests.test_ml_selector_execution_contract import FixedPolicy, FixedScoreModel, decision_fixture


def context_candidate():
    _, candidates, context = decision_fixture()
    strategy = SimpleNamespace(name="TrendBreakout", health=SimpleNamespace(status="probation"),
        health_risk_multiplier=Mock(return_value=.25), entry_risk_multiplier=Mock(return_value=.5))
    candidate = replace(candidates[0], strategy=strategy,
                        signal={"action": "buy", "stop_loss": 90., "requested_qty": 10.})
    options = {"batch_candidate_count": 4, "current_prices": context["current_prices"],
               "equity": 10000., "risk_manager": SimpleNamespace(risk_multiplier=.8),
               "as_of": pd.Timestamp("2026-09-02", tz="UTC")}
    return candidate, options


def test_all_context_values_come_from_current_candidate_and_runtime_queries():
    candidate, options = context_candidate()
    result = build_policy_context(candidate, requested_features=POLICY_CONTEXT_FEATURES, **options)
    assert result == {"native_candidate_score": 1.5, "stop_distance_fraction": .1,
        "batch_candidate_count": 4., "health_risk_multiplier": .25, "market_risk_multiplier": .5,
        "portfolio_risk_multiplier": .8, "requested_notional_fraction": .1, "candidate_is_short": 0.}
    candidate.strategy.entry_risk_multiplier.assert_called_once_with(candidate.state)
    assert all(np.isfinite(value) for value in result.values())


def test_post_allocation_audit_is_not_a_policy_feature_source():
    candidate, options = context_candidate()
    candidate = replace(candidate, audit={"health_multiplier": 999., "market_multiplier": 999.,
        "decision_context": {"health_risk_multiplier": 999., "available_at": "2099-01-01"}})
    result = build_policy_context(candidate,
        requested_features=("health_risk_multiplier", "market_risk_multiplier"), **options)
    assert result == {"health_risk_multiplier": .25, "market_risk_multiplier": .5}


def test_unrequested_unknown_features_are_not_queried_or_fabricated():
    candidate, options = context_candidate()
    candidate.strategy.health_risk_multiplier.side_effect = AssertionError("must not query health")
    assert build_policy_context(candidate, requested_features=(), **options) == {}
    assert build_policy_context(candidate, requested_features=("native_candidate_score",), **options) == {
        "native_candidate_score": candidate.score}
    candidate.strategy.health_risk_multiplier.assert_not_called()


@pytest.mark.parametrize("feature, replace_signal, option_change", [
    ("stop_distance_fraction", {"action": "buy", "stop_loss": 110.}, {}),
    ("stop_distance_fraction", {"action": "short", "stop_loss": 90.}, {}),
    ("stop_distance_fraction", {"action": "buy"}, {}),
    ("requested_notional_fraction", {"action": "buy"}, {}),
    ("requested_notional_fraction", {"action": "buy", "requested_qty": -1.}, {}),
    ("candidate_is_short", {"action": "exit"}, {}),
    ("portfolio_risk_multiplier", None, {"risk_manager": SimpleNamespace()}),
    ("portfolio_risk_multiplier", None, {"risk_manager": SimpleNamespace(risk_multiplier=np.nan)}),
    ("batch_candidate_count", None, {"batch_candidate_count": 2.5}),
    ("batch_candidate_count", None, {"batch_candidate_count": True}),
])
def test_missing_or_invalid_requested_values_fail_unknown(feature, replace_signal, option_change):
    candidate, options = context_candidate()
    if replace_signal is not None:
        candidate = replace(candidate, signal=replace_signal)
    with pytest.raises(PolicyContextUnavailable) as failure:
        build_policy_context(candidate, requested_features=(feature,), **{**options, **option_change})
    assert failure.value.missing_features == (feature,)


def test_missing_health_method_is_not_replaced_with_neutral_health():
    candidate, options = context_candidate()
    candidate = replace(candidate, strategy=SimpleNamespace(name="UnknownHealth"))
    with pytest.raises(PolicyContextUnavailable, match="health_risk_multiplier"):
        build_policy_context(candidate, requested_features=("health_risk_multiplier",), **options)


def test_short_stop_distance_and_direction_use_actual_signal():
    candidate, options = context_candidate()
    candidate = replace(candidate, signal={"action": "short", "stop_loss": 110.})
    assert build_policy_context(candidate, requested_features=("stop_distance_fraction", "candidate_is_short"),
                                **options) == {"stop_distance_fraction": .1, "candidate_is_short": 1.}


def test_snapshot_keeps_information_cutoff_and_does_not_claim_unknown_health_values():
    candidate, options = context_candidate()
    snapshot = decision_context_snapshot(candidate, current_prices=options["current_prices"], as_of=options["as_of"])
    assert snapshot["available_at"] == options["as_of"].isoformat()
    assert snapshot["current_price"] == 100. and snapshot["stop_loss"] == 90.
    assert snapshot["strategy_health"] == "probation"
    assert snapshot["health_multiplier"] is None and snapshot["market_multiplier"] is None
    candidate.strategy.health_risk_multiplier.assert_not_called()


def test_new_policy_receives_requested_context_and_rejects_candidate_with_missing_health():
    dataset, candidates, context = decision_fixture()
    known, options = context_candidate()
    candidates[0] = known
    policy = FixedPolicy()
    policy.features = (*policy.features, "health_risk_multiplier", "native_candidate_score")
    policy.act = Mock(wraps=policy.act)
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=policy)
    selected = selector.select(candidates, **context)
    assert [c.symbol for c in selected] == [known.symbol]
    frame = policy.act.call_args.args[0]
    assert frame.health_risk_multiplier.tolist() == [.25]
    assert frame.native_candidate_score.tolist() == [known.score]
    unknown = [row for row in selector.audit if row["reason"] == "policy_context_unavailable"]
    assert len(unknown) == 3
    assert all(row["missing_context_features"] == ["health_risk_multiplier"] for row in unknown)
    selected_audit = next(row for row in selector.audit if row["selected"])
    assert selected_audit["decision_context"]["health_multiplier"] == .25
    assert pd.Timestamp(selected_audit["decision_context_available_at"]) == pd.Timestamp(selected_audit["as_of"])


def test_legacy_policy_does_not_request_new_health_context_and_stochastic_credit_is_explicit():
    dataset, candidates, context = decision_fixture()
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=FixedPolicy(),
                                deterministic=False)
    assert len(selector.select(candidates, **context)) == 2
    assert selector.requested_context_features == ()
    for row in selector.trajectory:
        assert row["sampled_action"] is row["action"]
        assert row["effective_action"] is row["effective_gate_passed"]
        assert row["credit_assignment"] == "episode_account_return_to_go_not_individual_pnl"
        assert "individual_pnl" not in row
        assert row["decision_context_available_at"] == row["as_of"].isoformat()


def test_new_policy_context_is_captured_and_replayed_with_exact_decision_facts():
    from research.ml_selection.dataset import build_dataset
    from research.ml_selection.forward_bridge import RecordingSelector
    from tests.test_ml_selection_next_forward import FixedPolicy as BridgePolicy, full_context
    from tests.test_ml_selection_pipeline import ScoreModel, candidate, market
    policy = BridgePolicy()
    policy.features = (*FixedPolicy.features, "health_risk_multiplier", "stop_distance_fraction", "batch_candidate_count")
    strategy = SimpleNamespace(name="OriginalStrategy", health=SimpleNamespace(status="probation"),
        health_risk_multiplier=lambda: .25)
    candidates = [replace(candidate(), strategy=strategy), replace(candidate("B/USDT"), strategy=strategy)]
    original = ResearchSelector(build_dataset(market(), horizon_bars=3), mode="policy",
        model=ScoreModel(), policy=policy)
    recording = RecordingSelector(original, "protocol")
    assert len(recording.select(candidates, **full_context())) == 2
    assert recording.verification_errors == []
    assert recording.replay_comparisons[0]["identical"] is True
    snapshot = recording.snapshots[0]["candidates"][0]["decision_context"]
    assert snapshot["market_state"] == "trend" and snapshot["strategy_health"] == "probation"
    assert snapshot["policy_context_features"] == {
        "health_risk_multiplier": .25, "stop_distance_fraction": .25, "batch_candidate_count": 2.}


def test_context_unknown_rejection_and_partial_facts_replay_identically():
    from research.ml_selection.dataset import build_dataset
    from research.ml_selection.forward_bridge import RecordingSelector
    from tests.test_ml_selection_next_forward import FixedPolicy as BridgePolicy, full_context
    from tests.test_ml_selection_pipeline import ScoreModel, candidate, market
    policy = BridgePolicy()
    policy.features = (*FixedPolicy.features, "health_risk_multiplier", "native_candidate_score")
    original = ResearchSelector(build_dataset(market(), horizon_bars=3), mode="policy", model=ScoreModel(), policy=policy)
    recording = RecordingSelector(original, "protocol")
    assert recording.select([candidate(), candidate("B/USDT")], **full_context()) == []
    assert recording.verification_errors == []
    assert recording.replay_comparisons[0]["identical"] is True
    assert all(row["reason"] == "policy_context_unavailable" for row in original.audit)
    assert original.audit[0]["decision_context"]["policy_context_features"] == {"native_candidate_score": 1.5}


def test_captured_context_cannot_be_relabelled_as_available_after_the_decision():
    from research.ml_selection.forward_bridge import validate_hook_state
    from tests.test_ml_selection_next_forward import FixedPolicy as BridgePolicy, captured, resign
    from tests.test_ml_selection_pipeline import ScoreModel
    # Resigning the outer identity does not make a future context causal.
    snapshot = captured()
    snapshot["candidates"][0]["decision_context"]["available_at"] = "2099-01-01T00:00:00Z"
    resign(snapshot)
    with pytest.raises(ValueError, match="decision as_of"):
        validate_hook_state(snapshot, protocol_id="protocol", policy_id=BridgePolicy.model_id,
            parent_model_id=ScoreModel.model_id, information_cutoff=snapshot["available_at"])


def test_old_hook_snapshots_without_context_preserve_legacy_inference_and_unknown_context():
    from research.ml_selection.dataset import build_dataset
    from research.ml_selection.forward_bridge import bridge_decision
    from tests.test_ml_selection_next_forward import FixedPolicy as BridgePolicy, captured, resign
    from tests.test_ml_selection_pipeline import ScoreModel, market
    snapshot = captured()
    for row in snapshot["candidates"]:
        row.pop("decision_context")
        row.pop("selection_rank_score")
    resign(snapshot)
    decision = bridge_decision(snapshot, build_dataset(market(), horizon_bars=3), ScoreModel(), BridgePolicy(),
        protocol_id="protocol", information_cutoff=snapshot["available_at"])
    assert len(decision["selected"]) == 2
    assert decision["decisions"][0]["decision_context"]["market_state"] is None
    assert decision["decisions"][0]["decision_context"]["health_multiplier"] is None
