"""Immutable history must not turn today's downloads into historical knowledge."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.data_versions import DataVersionStore
from analysis.factor_registry import (
    default_proxy_definitions, freeze_factor_registry, validate_factor_definition,
)


def record(**overrides):
    return {
        "record_id": "BTC/USDT:2020-01-01", "revision_id": "original",
        "event_time": "2020-01-01T00:00:00Z", "observed_at": "2020-01-02T00:05:00Z",
        "available_at": "2020-01-02T00:00:00Z", "published_at": "2020-01-02T00:00:00Z",
        "availability_evidence": {"kind": "source_publication", "reference": "archive:original"},
        "data": {"close": 100.0}, **overrides,
    }


def test_late_observation_distinguishes_collector_from_published_history(tmp_path):
    store = DataVersionStore(tmp_path)
    snap = store.create_snapshot("bars", [record()])
    at = "2020-01-02T00:00:00Z"
    assert store.as_of(snap["snapshot_id"], at) == []
    assert store.as_of(snap["snapshot_id"], at, knowledge="published")[0]["data"]["close"] == 100
    assert len(store.as_of(snap["snapshot_id"], "2020-01-02T00:05:00Z")) == 1


def test_future_revision_preserves_old_snapshot_and_is_not_visible_early(tmp_path):
    store = DataVersionStore(tmp_path)
    old = store.create_snapshot("bars", [record()])
    amended = record(revision_id="revision-2", available_at="2020-01-05T00:00:00Z",
                     revision_at="2020-01-05T00:00:00Z", observed_at="2020-01-05T00:01:00Z",
                     data={"close": 105.0})
    new = store.create_snapshot("bars", [record(), amended])
    assert old["snapshot_id"] != new["snapshot_id"]
    for snap in (old, new):
        assert store.as_of(snap["snapshot_id"], "2020-01-03T00:00:00Z")[0]["data"]["close"] == 100
    assert store.as_of(new["snapshot_id"], "2020-01-06T00:00:00Z")[0]["data"]["close"] == 105
    assert store.as_of(old["snapshot_id"], "2020-01-06T00:00:00Z")[0]["data"]["close"] == 100
    assert store.create_snapshot("bars", [amended, record()])["snapshot_id"] == new["snapshot_id"]


def test_revision_identity_cannot_be_reused_with_different_data(tmp_path):
    store = DataVersionStore(tmp_path)
    snapshot = store.create_snapshot("bars", [record()])
    with pytest.raises(ValueError, match="immutable content conflict"):
        store.create_snapshot("bars", [record(data={"close": 99.0})])
    assert store.verify_snapshot(snapshot["snapshot_id"]) == snapshot


def test_unknown_available_at_remains_unknown_and_can_fail_closed(tmp_path):
    store = DataVersionStore(tmp_path)
    snapshot = store.create_snapshot("bars", [record(available_at=None, availability_evidence=None)])
    assert snapshot["availability"]["point_in_time_complete"] is False
    assert store.as_of(snapshot["snapshot_id"], "2030-01-01T00:00:00Z") == []
    with pytest.raises(ValueError, match="availability is unknown"):
        store.as_of(snapshot["snapshot_id"], "2030-01-01T00:00:00Z", unknown="raise")


def test_current_capture_cannot_certify_earlier_publication(tmp_path):
    store = DataVersionStore(tmp_path)
    with pytest.raises(ValueError, match="earlier historical availability"):
        store.create_snapshot("bars", [record(availability_evidence={
            "kind": "collector_capture", "reference": "capture:today"})])
    with pytest.raises(ValueError, match="availability_evidence"):
        store.create_snapshot("bars", [record(availability_evidence=None)])
    with pytest.raises(ValueError, match="precedes revision_at"):
        store.create_snapshot("bars", [record(revision_at="2020-02-01T00:00:00Z")])


def test_event_time_still_blocks_future_fact_and_timezone_is_explicit(tmp_path):
    store = DataVersionStore(tmp_path)
    snapshot = store.create_snapshot("bars", [record(event_time="2020-01-04T00:00:00Z")])
    assert store.as_of(snapshot["snapshot_id"], "2020-01-03T00:00:00Z") == []
    with pytest.raises(ValueError, match="timezone"):
        store.create_snapshot("bars", [record(event_time="2020-01-01")])
    with pytest.raises(ValueError, match="microsecond precision"):
        store.create_snapshot("bars", [record(event_time="2020-01-01T00:00:00.000000001Z")])


def test_ambiguous_revision_order_and_nan_are_rejected(tmp_path):
    store = DataVersionStore(tmp_path)
    with pytest.raises(ValueError, match="ambiguous revision"):
        store.create_snapshot("bars", [record(), record(revision_id="b")])
    with pytest.raises(ValueError, match="JSON"):
        store.create_snapshot("bars", [record(data={"close": float("nan")})])


def test_file_freeze_retains_bytes_and_unknown_pit_after_source_changes(tmp_path):
    source = tmp_path / "input.csv"
    source.write_bytes(b"timestamp,close\n2020-01-01,100\n")
    store = DataVersionStore(tmp_path / "store")
    snapshot = store.freeze_files("bars", {"BTC.csv": source},
                                 observed_at="2026-10-03T00:00:00Z",
                                 code_refs={"tree": "exact-source-hash"}, config_refs={"risk": 0.1})
    source.write_bytes(b"revised source")
    assert store.read_file(snapshot["snapshot_id"], "BTC.csv").endswith(b",100\n")
    assert snapshot["available_at"] is None
    assert not snapshot["availability"]["point_in_time_complete"]
    with pytest.raises(ValueError, match="unknown historical availability"):
        store.as_of(snapshot["snapshot_id"], "2026-10-03T00:00:00Z")


def test_code_config_references_participate_in_snapshot_identity(tmp_path):
    store = DataVersionStore(tmp_path)
    first = store.create_snapshot("bars", [record()], code_refs={"tree": "a"})
    second = store.create_snapshot("bars", [record()], code_refs={"tree": "b"})
    assert first["snapshot_id"] != second["snapshot_id"]
    assert first["records"] == second["records"]


def test_concurrent_identical_writes_publish_complete_objects_once(tmp_path):
    store = DataVersionStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as workers:
        snapshots = list(workers.map(lambda _: store.create_snapshot("bars", [record()]), range(12)))
    assert len({value["snapshot_id"] for value in snapshots}) == 1
    assert store.verify_snapshot(snapshots[0]["snapshot_id"]) == snapshots[0]
    assert not list(tmp_path.rglob(".pending-*"))


def test_tampered_object_is_never_silently_replaced_by_identical_import(tmp_path):
    store = DataVersionStore(tmp_path)
    snapshot = store.create_snapshot("bars", [record()])
    object_path = tmp_path / "objects" / snapshot["records"]["sha256"]
    object_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="immutable content conflict"):
        store.create_snapshot("bars", [record()])
    assert object_path.read_bytes() == b"corrupt"


@pytest.mark.parametrize("target", ["object", "manifest", "revision"])
def test_all_persisted_layers_detect_tampering(tmp_path, target):
    store = DataVersionStore(tmp_path)
    snapshot = store.create_snapshot("bars", [record()])
    if target == "object":
        path = tmp_path / "objects" / snapshot["records"]["sha256"]
    elif target == "manifest":
        path = tmp_path / "snapshots" / (snapshot["snapshot_id"] + ".json")
    else:
        path = next((tmp_path / "revisions").iterdir())
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="mismatch"):
        store.verify_snapshot(snapshot["snapshot_id"])


def test_untrusted_identifiers_and_file_names_cannot_escape_store(tmp_path):
    store = DataVersionStore(tmp_path)
    for identifier in ("../outside", "/outside", "a" * 63, "g" * 64):
        with pytest.raises(ValueError, match="invalid content identity"):
            store.read_snapshot(identifier)
    with pytest.raises(ValueError, match="path components"):
        store.freeze_files("bars", {"../outside": tmp_path / "irrelevant"},
                           observed_at="2026-10-03T00:00:00Z")


def test_factor_registry_preserves_proxy_scope_and_blocks_changed_version(tmp_path):
    definitions = default_proxy_definitions()
    assert [row["status"] for row in definitions] == ["proxy", "proxy", "unavailable"]
    assert definitions[2]["parameters"]["volume_is_market_cap"] is False
    snapshot = freeze_factor_registry(tmp_path, definitions, observed_at="2026-10-03T00:00:00Z")
    assert DataVersionStore(tmp_path).verify_snapshot(snapshot["snapshot_id"])["record_count"] == 3
    changed = json.loads(json.dumps(definitions))
    changed[0]["parameters"]["risk_free_rate"] = 0.1
    with pytest.raises(ValueError, match="immutable content conflict"):
        freeze_factor_registry(tmp_path, changed, observed_at="2026-10-03T00:00:00Z")
    changed[0]["version"] = "1.1.0"
    assert freeze_factor_registry(tmp_path, changed, observed_at="2026-10-03T00:00:00Z")["snapshot_id"] != snapshot["snapshot_id"]
    with pytest.raises(ValueError, match="limitations"):
        validate_factor_definition({**definitions[0], "limitations": []})
