"""Preserve original rejected decisions while a bridge blocks new risk."""
import numpy as np

from core.reproducibility import canonical_json
from research.ml_selection.dataset import build_dataset
from research.ml_selection.forward_bridge import RecordingSelector, bridge_decision
from research.ml_selection.selector import ResearchSelector
from tests.test_ml_selection_next_forward import FixedPolicy, captured, full_context
from tests.test_ml_selection_pipeline import ScoreModel, candidate, market


def exhausted_context():
    context = full_context()
    context["risk_manager"].drawdown_budget.snapshot().to_dict().update(
        budget=100., open_risk=100., pending_risk=50., available=0.)
    return context


def test_exhausted_budget_keeps_rejected_policy_audit_and_original_hook_parity():
    dataset = build_dataset(market(), horizon_bars=3)
    selector = RecordingSelector(ResearchSelector(dataset, mode="policy",
        model=ScoreModel(), policy=FixedPolicy(), policy_threshold=.5), "protocol")
    assert selector.select([candidate(), candidate("B/USDT")], **exhausted_context()) == []
    assert selector.verification_errors == []
    assert selector.replay_comparisons[0]["identical"]
    snapshot = selector.snapshots[0]
    decision = bridge_decision(snapshot, dataset, ScoreModel(), FixedPolicy(),
        protocol_id="protocol", information_cutoff=snapshot["available_at"],
        selection_options={"policy_threshold": .5})
    assert decision["risk_blocks_new_entries"] and decision["selected"] == []
    assert canonical_json(decision["decisions"]) == canonical_json(selector.audit)
    assert all(row["reason"] == "policy_gate" and not row["selected"]
        and "policy_selected_before_risk" not in row for row in decision["decisions"])
    assert all(row["selection_probability"] == .495 for row in decision["decisions"])


def test_blocked_mixed_batch_preserves_rejection_and_only_revokes_selected_entry():
    class MixedPolicy(FixedPolicy):
        def act(self, frame, *, deterministic=True, threshold=.5):
            probabilities = np.asarray([.495, .505])
            return probabilities >= threshold, probabilities

    context = exhausted_context()
    dataset, model, policy = build_dataset(market(), horizon_bars=3), ScoreModel(), MixedPolicy()
    original = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
        policy_threshold=.5)
    assert len(original.select([candidate(), candidate("B/USDT")], **context)) == 1
    snapshot = captured(context)
    decision = bridge_decision(snapshot, dataset, model, policy, protocol_id="protocol",
        information_cutoff=snapshot["available_at"], selection_options={"policy_threshold": .5})
    assert decision["risk_blocks_new_entries"] and decision["selected"] == []
    rejected, revoked = decision["decisions"]
    assert canonical_json(rejected) == canonical_json(original.audit[0])
    assert revoked["selected"] is False and revoked["policy_selected_before_risk"] is True
    assert revoked["reason"] == "original_engine_risk_blocks_new_entries"
    before = {key: value for key, value in original.audit[1].items() if key not in {"selected", "reason"}}
    after = {key: value for key, value in revoked.items()
        if key not in {"selected", "reason", "policy_selected_before_risk"}}
    assert canonical_json(before) == canonical_json(after)
    assert not decision["execution_approval_available"]
    assert not decision["simulated_account_performance_available"]
