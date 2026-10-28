"""Latest-main serving contracts stay effective on PR25 temporal inputs."""
from copy import deepcopy

import pandas as pd
import pytest

from research.ml_selection.dataset import build_dataset, build_inference_dataset
from research.ml_selection.forward_bridge import capture_hook_state, validate_hook_state
from research.ml_selection.selector import ResearchSelector
from tests.test_ml_selection_pipeline import candidate, market
from tests.test_ml_selection_next_forward import FixedPolicy
from tests.test_pr25_temporal_ml import (
    StrictScoreModel, documented, membership, strict_serving,
)


def test_temporal_inference_keeps_adoption_policy_and_never_builds_proxy_labels():
    context, training, _ = strict_serving()
    frames = {symbol: documented() for symbol in ("A/USDT", "B/USDT")}
    kwargs = {"temporal_contexts": [context], "membership": membership()}
    inference = build_inference_dataset(frames, **kwargs)
    assert not any(column.startswith("label_") for column in inference)
    assert inference.attrs["future_labels_computed"] is False
    assert inference.attrs["data_identity"] == training.attrs["data_identity"]
    assert inference.attrs["data_identity"]["schema"] == "ml-selection-data-identity/v2"
    strict_quote = build_inference_dataset(frames, require_exchange_quote_volume=True, **kwargs)
    assert not strict_quote.eligible.any()
    assert strict_quote.exclusion_reason.eq("quote_volume_source_unverified").all()
    assert (strict_quote.attrs["data_identity"]["data_identity_sha256"] !=
            inference.attrs["data_identity"]["data_identity_sha256"])


def test_membership_source_alias_preserves_main_data_identity_v1():
    frames = market(95)
    kwargs = {"membership": membership(), "horizon_bars": 3}
    old_flag = build_dataset(frames, require_verified_membership=True, **kwargs)
    alias = build_dataset(frames, membership_require_sources=True, **kwargs)
    pd.testing.assert_frame_equal(old_flag, alias)
    assert old_flag.attrs["data_identity"] == alias.attrs["data_identity"]
    assert old_flag.attrs["data_identity"]["schema"] == "ml-selection-data-identity/v1"


def test_strict_selection_keeps_main_ablation_contract_and_native_capital_score():
    context, dataset, engine = strict_serving()
    contract = {"eligibility_filter": True, "ranking": True, "return_gate": False, "policy_gate": False}
    candidates = [candidate(), candidate("B/USDT")]
    selector = ResearchSelector(dataset, mode="model", model=StrictScoreModel(),
        selector_contract=contract, capital_score_source="original_score")
    selected = selector.select(candidates, **engine)
    assert len(selected) == len(candidates)
    for before, after, row in zip(candidates, selected, selector.audit, strict=True):
        assert after.score == before.score and after.selection_rank_score is not None
        assert row["selector_contract"] == contract and row["gate_contract"] == "ranking_only"
        assert row["temporal_mode"] == "strict"
        assert row["decision_context"]["available_at"] == context.decision_cutoff.isoformat()


def test_strict_bridge_context_is_captured_at_the_actual_delayed_cutoff():
    context, _, engine = strict_serving()
    snapshot = capture_hook_state([candidate()], protocol_id="protocol", policy_id=FixedPolicy.model_id,
        parent_model_id=StrictScoreModel.model_id, context_features=("native_candidate_score",), **engine)
    actual = snapshot["candidates"][0]["decision_context"]
    assert actual["available_at"] == context.decision_cutoff.isoformat()
    assert actual["policy_context_features"]["native_candidate_score"] == candidate().score
    validate_hook_state(snapshot, protocol_id="protocol", policy_id=FixedPolicy.model_id,
        parent_model_id=StrictScoreModel.model_id, information_cutoff=context.decision_cutoff)


def test_temporal_policy_rejects_malformed_membership_exit_and_nonboolean_flags():
    context, _, _ = strict_serving()
    facts = deepcopy(membership(("A/USDT",)))
    facts["A/USDT"]["delisted_at"] = "not-a-date"
    rows = build_dataset({"A/USDT": documented()}, membership=facts, temporal_contexts=[context])
    assert not rows.eligible.any()
    with pytest.raises(ValueError, match="boolean"):
        build_dataset({"A/USDT": documented()}, membership_require_sources=1)
