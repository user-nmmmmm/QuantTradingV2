"""Explicit source withdrawals invalidate future labels without erasing history."""
from copy import deepcopy
from dataclasses import asdict

import pandas as pd
import pytest

from core.data_versions import DataVersionStore
from core.signal_label_versions import OutcomeRevisionBook
from core.signal_meta_layer import _validate_input
from core.temporal_data import TemporalOHLCV
from core.temporal_labels import TemporalLabelError
from tests.test_temporal_labels import reader, tracker, event, documented, candidate, iso


def retract(data, at="2020-01-02", known="2020-01-06", observed=None):
    return data.retract(at, revision_id="withdrawn", available_at=iso(known),
        observed_at=iso(observed or known), reason="provider withdrew the candle",
        availability_evidence={"kind": "source_publication", "reference": "fixture:withdrawal"})


def payload(result):
    c = candidate()
    return {"schema": "signal_observation/v1", "status": "complete", "policy": asdict(result.policy),
        "snapshot_version": c.context.snapshot_version, "strategy_versions": {c.strategy: c.signal_version},
        "candidates": [c.to_dict()], "decisions": [{"candidate_id": c.candidate_id, "veto_stage": "allowed"}],
        "outcomes": result.results, "outcome_revisions": result.revisions,
        "costs": asdict(result.costs), "temporal_label_protocol": result.protocol}


def test_version_store_and_temporal_history_never_fall_back_after_tombstone(tmp_path):
    data = TemporalOHLCV("fixture", timeframe="1d", policy={"mode": "strict", "store_path": str(tmp_path)})
    data.ingest(documented())
    retract(data)
    assert pd.Timestamp("2020-01-02") in data.as_of("2020-01-05").index
    assert pd.Timestamp("2020-01-02") not in data.as_of("2020-01-06").index
    tombstone = data.as_of("2020-01-06").attrs["temporal_source_versions"][iso("2020-01-02")]
    assert tombstone["record"]["record_type"] == "tombstone"
    restored = TemporalOHLCV("fixture", timeframe="1d", policy={"mode": "strict", "store_path": str(tmp_path)})
    assert pd.Timestamp("2020-01-02") not in restored.as_of("2020-02-01").index
    store = DataVersionStore(tmp_path)
    bundle = store.create_snapshot("all-revisions", data.records)
    selected = store.as_of(bundle["snapshot_id"], iso("2020-01-06"))
    assert iso("2020-01-02") not in [r["record_id"] for r in selected]
    assert any(r.get("record_type") == "tombstone" for r in store.as_of(
        bundle["snapshot_id"], iso("2020-01-06"), include_tombstones=True))


@pytest.mark.parametrize("withdrawn", ["2020-01-01", "2020-01-02", "2020-01-03"])
def test_signal_entry_and_intermediate_withdrawals_append_ineligible_revision_and_keep_old_cutoff(withdrawn):
    data = reader()
    result = tracker(data, horizons=(3,))
    result.advance(event(data, "2020-01-05"))
    old = deepcopy(result.revisions)
    retract(data, at=withdrawn)
    result.advance(event(data, "2020-01-06"))
    assert result.revisions[:1] == old
    assert result.results[0]["status"] == "censored_source_retracted"
    assert not result.results[0]["training_eligible"]
    assert result.results[0]["available_at"] == iso("2020-01-06")
    p = payload(result)
    h, c, _, o = _validate_input(p)
    book = OutcomeRevisionBook(p, c, h, o)
    assert book.eligible(book.as_of("2020-01-06")["c", 3])
    assert not book.eligible(book.as_of("2020-01-07")["c", 3])
    result.advance(event(data, "2020-01-07"))
    assert len(result.revisions) == 2


def test_reinstatement_is_an_explicit_new_revision_and_does_not_resurrect_early():
    data = reader()
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-03"))
    retract(data)
    result.advance(event(data, "2020-01-06"))
    frame = documented().iloc[[1]].copy()
    frame["revision_id"] = "restored"
    frame["available_at"] = frame["observed_at"] = pd.Timestamp("2020-01-08", tz="UTC")
    data.ingest(frame)
    result.advance(event(data, "2020-01-07"))
    assert not result.results[0]["training_eligible"]
    result.advance(event(data, "2020-01-08"))
    assert result.results[0]["training_eligible"] and len(result.revisions) == 3


def test_withdrawal_observation_latency_and_missing_rows_are_distinct():
    data = reader()
    retract(data, observed="2020-01-08")
    assert pd.Timestamp("2020-01-02") in data.as_of("2020-01-07").index
    assert pd.Timestamp("2020-01-02") not in data.as_of("2020-01-08").index
    result = tracker(reader(), horizons=(1,))
    source = reader()
    result.advance(event(source, "2020-01-03"))
    market = event(source, "2020-01-07")
    market.histories["X"].attrs["temporal_source_versions"] = {}
    result.advance(market)
    assert len(result.revisions) == 1 and result.results[0]["training_eligible"]


def test_withdrawal_with_unknown_clock_or_visible_old_value_is_rejected():
    data = reader()
    with pytest.raises(ValueError):
        data.retract("2020-01-02", revision_id="bad", available_at=None,
            observed_at=iso("2020-01-06"), availability_evidence=None, reason="unknown")
    result = tracker(data, horizons=(1,))
    old = event(data, "2020-01-06")
    retract(data)
    new = event(data, "2020-01-06")
    old.histories["X"].attrs = deepcopy(new.histories["X"].attrs)
    with pytest.raises(TemporalLabelError, match="withdrawn source still"):
        result.advance(old)
