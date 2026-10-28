"""Lifecycle restrictions share the selected OHLCV revision and cutoff."""

from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from core.market.temporal_data import OHLCV, TemporalDataError, TemporalOHLCV


def frame():
    result = pd.DataFrame(
        {"open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0], "volume": [5.0]},
        index=pd.DatetimeIndex(["2024-01-01"]),
    )
    result["available_at"] = result["observed_at"] = pd.Timestamp("2024-01-02", tz="UTC")
    result["revision_id"] = "initial"
    result.attrs["availability_evidence"] = {
        "kind": "source_publication",
        "reference": "synthetic:fixture",
    }
    return result


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
@pytest.mark.parametrize("knowledge", ["local", "published"])
def test_only_lifecycle_revision_is_durable_and_obeys_knowledge_cutoff(tmp_path, flag, knowledge):
    policy = {"mode": "strict", "knowledge": knowledge, "store_path": str(tmp_path)}
    source = TemporalOHLCV("lifecycle", timeframe="1d", policy=policy)
    initial = frame()
    initial[flag] = True
    source.ingest(initial)
    before = source.as_of("2024-01-02")
    correction = initial.copy()
    correction[flag] = False
    correction["revision_id"] = "explicit-clear"
    correction["available_at"] = pd.Timestamp("2024-01-03", tz="UTC")
    correction["observed_at"] = pd.Timestamp("2024-01-04", tz="UTC")
    source.ingest(correction)
    assert len(source.records) == len(source.snapshot_ids) == 2
    assert bool(source.as_of("2024-01-03").iloc[0][flag]) is (knowledge == "local")
    assert bool(source.as_of("2024-01-04").iloc[0][flag]) is False
    pd.testing.assert_frame_equal(source.as_of("2024-01-02"), before)
    restored = TemporalOHLCV("lifecycle", timeframe="1d", policy=policy)
    assert restored.identity["records_sha256"] == source.identity["records_sha256"]
    assert set(restored.snapshot_ids) == set(source.snapshot_ids)
    pd.testing.assert_frame_equal(restored.as_of("2024-01-03"), source.as_of("2024-01-03"))
    frozen = restored.annotate(correction.copy())
    reader = TemporalOHLCV("lifecycle", timeframe="1d", policy={"mode": "strict"})
    assert reader.import_identity(frozen)
    assert bool(reader.as_of("2024-01-04").iloc[0][flag]) is False


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_local_capture_detects_only_lifecycle_revision(tmp_path, flag):
    source = TemporalOHLCV(
        "local", timeframe="1d", policy={"mode": "strict", "store_path": str(tmp_path)}
    )
    initial = frame()[OHLCV].copy()
    initial[flag] = False
    source.ingest(initial, observed_at="2024-01-02T00:00:01Z", local_receipt=True)
    correction = initial.copy()
    correction[flag] = True
    source.ingest(correction, observed_at="2024-01-02T00:00:02Z", local_receipt=True)
    assert len(source.records) == 2
    assert bool(source.as_of("2024-01-02T00:00:01Z").iloc[0][flag]) is False
    assert bool(source.as_of("2024-01-02T00:00:02Z").iloc[0][flag]) is True


@pytest.mark.parametrize("value", [None, np.nan, pd.NA, "", "False", "0", "true", [], {}])
@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_present_ambiguous_lifecycle_values_never_grant_entry(value, flag):
    source = TemporalOHLCV("unknown", timeframe="1d", policy={"mode": "strict"})
    initial = frame()
    initial[flag] = pd.Series([value], index=initial.index, dtype=object)
    source.ingest(initial)
    assert source.records[0]["data"][flag] is True
    assert bool(source.as_of("2024-01-02").iloc[0][flag]) is True


@pytest.mark.parametrize(
    "value,expected",
    [(False, False), (np.bool_(False), False), (0, False), (0.0, False), (True, True), (1, True)],
)
def test_explicit_boolean_and_numeric_lifecycle_claims(value, expected):
    source = TemporalOHLCV("explicit", timeframe="1d", policy={"mode": "strict"})
    initial = frame()
    initial["entry_blocked"] = value
    source.ingest(initial)
    assert source.records[0]["data"]["entry_blocked"] is expected


def test_plain_ohlcv_legacy_columns_records_and_identity_stay_unchanged():
    source = TemporalOHLCV("legacy", timeframe="1d", policy={"mode": "strict"})
    original = frame()
    source.ingest(original)
    records, identity = deepcopy(source.records), deepcopy(source.identity)
    visible = source.as_of("2024-01-02")
    assert list(visible.columns) == OHLCV
    assert set(source.records[0]["data"]) == set(OHLCV)
    assert source.records == records and source.identity == identity
    restored = TemporalOHLCV("legacy", timeframe="1d", policy={"mode": "strict"})
    assert restored.import_identity(source.annotate(original.copy()))
    assert restored.identity == identity
    pd.testing.assert_frame_equal(restored.as_of("2024-01-02"), visible)


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_old_frozen_ohlcv_without_guard_cannot_erase_new_frame_restriction(flag):
    source = TemporalOHLCV("legacy", timeframe="1d", policy={"mode": "strict"})
    original = frame()
    source.ingest(original)
    frozen = source.annotate(original.copy())
    frozen[flag] = True
    reader = TemporalOHLCV("legacy", timeframe="1d", policy={"mode": "strict"})
    with pytest.raises(TemporalDataError, match="lifecycle.*frozen"):
        reader.import_identity(frozen)


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_guard_change_with_unchanged_frozen_identity_is_rejected(flag):
    source = TemporalOHLCV("guarded", timeframe="1d", policy={"mode": "strict"})
    original = frame()
    original[flag] = True
    source.ingest(original)
    frozen = source.annotate(original.copy())
    frozen[flag] = False
    reader = TemporalOHLCV("guarded", timeframe="1d", policy={"mode": "strict"})
    with pytest.raises(TemporalDataError, match="lifecycle.*frozen"):
        reader.import_identity(frozen)


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_later_price_revision_cannot_silently_omit_previously_known_guard(flag):
    source = TemporalOHLCV("guarded", timeframe="1d", policy={"mode": "strict"})
    original = frame()
    original[flag] = True
    source.ingest(original)
    correction = frame()
    correction["close"] = 100.5
    correction["revision_id"] = "lost-guard"
    correction["available_at"] = correction["observed_at"] = pd.Timestamp("2024-01-03", tz="UTC")
    source.ingest(correction)
    assert bool(source.as_of("2024-01-02").iloc[0][flag]) is True
    with pytest.raises(TemporalDataError, match="omits.*lifecycle"):
        source.as_of("2024-01-03")


@pytest.mark.parametrize("flag", ["entry_blocked", "scheduled_exit"])
def test_future_guard_does_not_change_earlier_legacy_projection(flag):
    source = TemporalOHLCV("legacy", timeframe="1d", policy={"mode": "strict"})
    initial = frame()
    source.ingest(initial)
    before = source.as_of("2024-01-02")
    correction = frame()
    correction[flag] = True
    correction["revision_id"] = "new-guard"
    correction["available_at"] = correction["observed_at"] = pd.Timestamp("2024-01-03", tz="UTC")
    source.ingest(correction)
    pd.testing.assert_frame_equal(source.as_of("2024-01-02"), before)
    assert bool(source.as_of("2024-01-03").iloc[0][flag]) is True
