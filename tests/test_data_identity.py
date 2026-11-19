"""Exact research identities retain nanoseconds, ordering, metadata and policy."""

from copy import deepcopy
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from analysis.research_evidence import ResearchEvidenceRun, panel_result_binding, source_identity
from analysis.search_scope import SearchScope, data_manifest
from core.foundation.data_identity import data_hashes, sha256_frame_exact, sha256_series_exact
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file, sha256_frame


def frame():
    index = pd.date_range("2020-01-01", periods=3, tz="UTC")
    return pd.DataFrame(
        {"close": [1.0, 2.0, 3.0], "available_at": index + pd.Timedelta(1, "ns")}, index=index
    )


def digest(value):
    return sha256_bytes(canonical_json(value).encode())


@pytest.mark.parametrize(
    "where",
    [
        "index",
        "datetime_column",
        "attrs_cutoff",
        "attrs_source_version",
        "row_order",
        "column_order",
        "dtype",
    ],
)
def test_exact_identity_covers_temporal_and_numeric_contract_changes(where):
    original = frame()
    original.attrs = {
        "selection_temporal_context": {"decision_cutoff": "2020-01-03T00:00:00.000000001Z"},
        "temporal_source_versions": {"x": "revision-1"},
    }
    changed = original.copy(deep=True)
    changed.attrs = deepcopy(original.attrs)
    if where == "index":
        changed.index = changed.index + pd.Timedelta(1, "ns")
    elif where == "datetime_column":
        changed["available_at"] = changed["available_at"] + pd.Timedelta(1, "ns")
    elif where == "attrs_cutoff":
        changed.attrs["selection_temporal_context"]["decision_cutoff"] = (
            "2020-01-03T00:00:00.000000002Z"
        )
    elif where == "attrs_source_version":
        changed.attrs["temporal_source_versions"]["x"] = "revision-2"
    elif where == "row_order":
        changed = changed.iloc[::-1]
    elif where == "column_order":
        changed = changed[["available_at", "close"]]
    else:
        changed["close"] = changed["close"].astype("float32")
    assert sha256_frame_exact(original) != sha256_frame_exact(changed)
    assert sha256_frame_exact(original) == sha256_frame_exact(original.copy(deep=True))


def test_legacy_index_nanosecond_collision_remains_unchanged_only_for_legacy_compatibility():
    a = frame().drop(columns=["available_at"])
    b = a.copy()
    b.index = b.index + pd.Timedelta(1, "ns")
    assert sha256_frame(a) == sha256_frame(b)
    assert data_hashes({"x": a}, version="legacy/v1") == data_hashes({"x": b}, version="legacy/v1")
    assert data_hashes({"x": a}) != data_hashes({"x": b})


def test_exact_series_retains_hidden_middle_values_names_and_nanoseconds():
    a = pd.Series(
        np.arange(100, dtype=float),
        index=pd.date_range("2020-01-01", periods=100, tz="UTC"),
        name="return",
    )
    b = a.copy()
    b.iloc[50] = 99
    assert sha256_series_exact(a) != sha256_series_exact(b)
    b = a.copy()
    b.index = b.index + pd.Timedelta(1, "ns")
    assert sha256_series_exact(a) != sha256_series_exact(b)
    assert sha256_series_exact(a) != sha256_series_exact(a.rename("different"))


def test_nullable_category_metadata_and_duplicate_column_positions_are_preserved():
    a = pd.DataFrame(
        {
            "x": pd.Categorical(["a", "b"], categories=["a", "b", "unused"], ordered=False),
            "y": pd.Series([1, pd.NA], dtype="Int64"),
        }
    )
    b = a.copy()
    b["x"] = b["x"].cat.remove_unused_categories()
    assert sha256_frame_exact(a) != sha256_frame_exact(b)
    b = a.copy()
    b.columns = ["same", "same"]
    assert sha256_frame_exact(b) != sha256_frame_exact(b.iloc[:, ::-1])
    assert sha256_frame_exact(a) == sha256_frame_exact(a.copy())


def test_unknown_frame_attrs_are_rejected_instead_of_hashed_as_unstable_repr():
    value = frame()
    value.attrs["opaque"] = object()
    with pytest.raises(ValueError, match="unsupported exact data value"):
        sha256_frame_exact(value)


def test_finite_json_keeps_old_hash_but_nonfinite_json_is_rejected():
    value = {"x": [1, 0.2, None, True], "policy": "fixed"}
    assert data_hashes({"x": value})["x"] == digest(value)
    for bad in [float("nan"), float("inf"), {"nested": [float("nan")]}]:
        with pytest.raises(ValueError, match="finite JSON"):
            data_hashes({"x": bad})
    with pytest.raises(ValueError, match="unknown data identity"):
        data_hashes({"x": value}, version="unregistered/v9")


@pytest.mark.parametrize("where", ["index", "datetime_column", "attrs"])
def test_new_evidence_verification_detects_exact_data_drift_with_legacy_default_preserved(
    tmp_path, where
):
    value = frame()
    run = ResearchEvidenceRun(
        candidates=["x"],
        parameters={},
        data_map={"x": value},
        source_hashes={},
        data_identity_version="exact/v2",
    )
    assert run.registration["data_identity_version"] == "exact/v2"
    assert run.verify_identity({"x": value})["data_unchanged"]
    if where == "index":
        value.index = value.index + pd.Timedelta(1, "ns")
    elif where == "datetime_column":
        value["available_at"] = value["available_at"] + pd.Timedelta(1, "ns")
    else:
        value.attrs["temporal_source_versions"] = {"x": "different"}
    result = run.verify_identity({"x": value})
    assert result["identity_changed"] and not result["data_unchanged"]
    old = ResearchEvidenceRun(
        candidates=["x"], parameters={}, data_map={"x": frame()}, source_hashes={}
    )
    assert old.registration["data_identity_version"] == "legacy/v1"


def make_scope(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.json").write_text("{}", encoding="utf-8")
    paths = ["config.json"]
    data = {"x": frame()}
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "exact-inputs",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L08"],
        "source_manifest_sha256": digest(source_identity(root, paths=paths)),
        "config_sha256": sha256_file(root / "config.json"),
        "data_manifest_sha256": digest(data_manifest(data)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["a"],
        "benchmark_ids": ["b"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "engineering_identity",
        "economic_or_precision_target": "exact nanosecond provenance",
        "training_selection_rule": "no fitting",
        "evaluation_windows": [
            {"id": "fixed", "role": "engineering", "start": "2020-01-01", "end": "2021-01-01"}
        ],
        "cost_policy_id": "fixed",
        "risk_policy_id": "fixed",
        "resampling": {"method": "not_applicable", "reason": "identity fixture"},
        "attempt_budget": 2,
        "resource_budget": {"wall_time_seconds": 60, "memory_mb": 128, "max_workers": 1},
        "stop_rules": ["stop on drift"],
        "final_access_policy_id": None,
    }
    scope = SearchScope.register(
        protocol=protocol,
        data_map=data,
        output=tmp_path / "run",
        source_root=root,
        source_paths=paths,
    )
    return scope, data, root


@pytest.mark.parametrize("where", ["index", "datetime_column", "attrs"])
def test_search_scope_audit_resume_and_direct_identity_verification_preserve_exact_policy(
    tmp_path, where
):
    scope, data, root = make_scope(tmp_path)
    assert (
        scope.protocol["data_identity_version"]
        == scope.run.registration["data_identity_version"]
        == "exact/v2"
    )
    restored = SearchScope.resume(output=scope.output, data_map=data, source_root=root)
    assert restored.audit()["status"] == "ok"
    if where == "index":
        data["x"].index = data["x"].index + pd.Timedelta(1, "ns")
    elif where == "datetime_column":
        data["x"]["available_at"] = data["x"]["available_at"] + pd.Timedelta(1, "ns")
    else:
        data["x"].attrs["selection_temporal_context"] = {
            "available_at": "2020-01-01T00:00:00.000000001Z"
        }
    assert restored.audit()["status"] == "invalid"
    with pytest.raises(ValueError):
        SearchScope.resume(output=scope.output, data_map=data, source_root=root)
    assert scope.run.verify_identity(data)["identity_changed"]


def test_exact_result_binding_has_no_nanosecond_series_alias():
    a = frame()["close"]
    b = a.copy()
    b.index = b.index + pd.Timedelta(1, "ns")
    basis = {
        "return_basis": "net_account_equity_returns",
        "capital_policy_id": "fixed",
        "cost_policy_id": "fixed",
        "risk_policy_id": "fixed",
        "timeframe": "1d",
        "currency": "USDT",
        "account_trace_sha256": "a" * 64,
    }
    assert panel_result_binding(a, basis) != panel_result_binding(b, basis)


def test_cyclic_semantic_metadata_fails_explicitly_instead_of_truncating():
    value = frame()
    circular = {}
    circular["self"] = circular
    value.attrs["source_context"] = circular
    with pytest.raises(ValueError, match="cyclic metadata"):
        sha256_frame_exact(value)
