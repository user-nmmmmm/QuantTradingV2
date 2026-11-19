"""Serving receipts and replay snapshots bind the bytes actually consumed."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest import coin_selector
from research.ml_selection import serving
from research.ml_selection import models


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    records = {}
    for name, model_id in (("model", "parent"), ("policy", "policy")):
        data = json.dumps({"model_id": model_id, "value": "original"}).encode()
        (root / f"{name}.json").write_bytes(data)
        records[name] = {"path": f"{name}.json", "sha256": digest(data), "model_id": model_id}
    package = {"schema": "frozen-coin-selector/v1", "timeframe": "1d",
               "candidate": {"selected_candidate": "rl", "parent_model_id": "parent", "model_id": "policy"},
               "files": records, "dataset": {}, "selection": {"score_scale": 5},
               "source_protocol_id": "synthetic", "training_end_exclusive": "2024-01-01",
               "validation_end_exclusive": "2024-02-01"}
    path = root / "manifest.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    return path, package


def test_manifest_identity_is_from_consumed_bytes_when_file_changes_during_model_load(bundle, monkeypatch):
    path, package = bundle
    original = path.read_bytes()
    original_policy = (path.parent / "policy.json").read_bytes()
    loads = []

    def construct(name, payload):
        loads.append((name, payload["value"]))
        if len(loads) == 1:
            changed = deepcopy(package)
            changed["selection"]["score_scale"] = 99
            path.write_text(json.dumps(changed), encoding="utf-8")
            (path.parent / "policy.json").write_text(
                '{"model_id":"policy","value":"changed"}', encoding="utf-8")
        return SimpleNamespace(kind="lightgbm" if name == "model" else "bernoulli_policy",
                               model_id="parent" if name == "model" else "policy",
                               metadata={"evaluation_threshold": .5})

    def from_path(value):
        return construct(Path(value).stem, json.loads(Path(value).read_bytes()))

    def from_bytes(data):
        payload = json.loads(data)
        return construct("model" if payload["model_id"] == "parent" else "policy", payload)

    monkeypatch.setattr(serving, "load_model", from_path, raising=False)
    monkeypatch.setattr(serving, "load_model_bytes", from_bytes, raising=False)
    rows = pd.DataFrame({"eligible": [True]})
    rows.attrs["data_identity"] = {"data_identity_sha256": "synthetic", "symbols": {}}
    monkeypatch.setattr(serving, "build_inference_dataset", lambda *args, **kwargs: rows)
    monkeypatch.setattr(serving, "ResearchSelector", lambda *args, **kwargs: SimpleNamespace(
        policy_threshold=.5, gate_contract="policy_only", gate_contract_source="synthetic",
        policy_gate_mode="policy_only", selector_contract={}, capital_score_source="original_score"))

    _, identity = serving.create_selector({}, initial_capital=1000, bundle_path=path)
    assert loads == [("model", "original"), ("policy", "original")]
    assert identity["bundle_sha256"] == digest(original)
    assert identity["bundle_sha256"] != digest(path.read_bytes())
    with pytest.raises(ValueError, match="hash mismatch"):
        coin_selector.snapshot_bundle(identity, path.parent.parent / "snapshot")
    (path.parent / "policy.json").write_bytes(original_policy)
    with pytest.raises(ValueError, match="package changed"):
        coin_selector.snapshot_bundle(identity, path.parent.parent / "snapshot")


def test_snapshot_uses_verified_weight_bytes_even_if_source_changes_after_validation(bundle, monkeypatch):
    path, package = bundle
    output = path.parent.parent / "snapshot"
    original = (path.parent / "policy.json").read_bytes()
    mkdir = Path.mkdir

    def change_source_on_publish(directory, *args, **kwargs):
        if directory == output:
            (path.parent / "policy.json").write_text('{"model_id":"policy","value":"changed"}', encoding="utf-8")
        return mkdir(directory, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", change_source_on_publish)
    snapshot = coin_selector.snapshot_bundle({"bundle_path": str(path), "bundle_sha256": digest(path.read_bytes())}, output)
    assert (output / "policy.json").read_bytes() == original
    assert coin_selector.read_bundle(snapshot)[1] == package


def test_absolute_package_member_is_rejected_before_snapshot_creation(bundle):
    path, package = bundle
    package["files"]["model"]["path"] = str(path.parent / "model.json")
    path.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(ValueError, match="relative|portable|path"):
        coin_selector.read_bundle(path)


@pytest.mark.parametrize("relative", ["../model.json", r"..\model.json", r"C:\model.json", "manifest.json", "."])
def test_nonportable_member_paths_fail_before_output(bundle, relative):
    path, package = bundle
    package["files"]["model"]["path"] = relative
    path.write_text(json.dumps(package), encoding="utf-8")
    output = path.parent.parent / "snapshot"
    with pytest.raises(ValueError):
        coin_selector.snapshot_bundle({"bundle_path": str(path), "bundle_sha256": digest(path.read_bytes())}, output)
    assert not output.exists()


def test_nested_relative_paths_remain_self_contained_and_existing_output_is_preserved(bundle):
    path, package = bundle
    for role, record in package["files"].items():
        relative = f"weights/{role}.json"
        target = path.parent / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((path.parent / record["path"]).read_bytes())
        record["path"] = relative
    path.write_text(json.dumps(package), encoding="utf-8")
    output = path.parent.parent / "snapshot"
    identity = {"bundle_path": str(path), "bundle_sha256": digest(path.read_bytes())}
    snapshot = coin_selector.snapshot_bundle(identity, output)
    assert coin_selector.read_bundle(snapshot)[1] == package
    before = snapshot.read_bytes()
    with pytest.raises(FileExistsError):
        coin_selector.snapshot_bundle(identity, output)
    assert snapshot.read_bytes() == before


@pytest.mark.parametrize("relative", ["MANIFEST.JSON", "Manifest.Json", "manifest.json/model.json"])
def test_manifest_name_is_reserved_across_case_sensitive_and_insensitive_filesystems(bundle, relative):
    path, package = bundle
    current = path.with_name("current.json")
    original_model = (path.parent / "model.json").read_bytes()
    path.unlink()
    member = path.parent / relative
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_bytes(original_model)
    package["files"]["model"]["path"] = relative
    current.write_text(json.dumps(package), encoding="utf-8")
    output = current.parent.parent / "snapshot"
    with pytest.raises(ValueError, match="portable relative paths"):
        coin_selector.snapshot_bundle({"bundle_path": str(current), "bundle_sha256": digest(current.read_bytes())}, output)
    assert not output.exists()


def test_case_aliased_members_are_rejected_before_reading_second_artifact(bundle):
    path, package = bundle
    package["files"]["policy"]["path"] = "MODEL.JSON"
    path.write_text(json.dumps(package), encoding="utf-8")
    output = path.parent.parent / "snapshot"
    with pytest.raises(ValueError, match="paths must be distinct"):
        coin_selector.snapshot_bundle({"bundle_path": str(path), "bundle_sha256": digest(path.read_bytes())}, output)
    assert not output.exists()


def test_byte_model_loader_retains_path_loader_identity_and_predictions(tmp_path):
    frame = pd.DataFrame({"a": [1., 2., 3.], "b": [3., 1., 4.]})
    scaler = models.FeatureScaler.fit(frame, ("a", "b"))
    original = models.RidgeModel(scaler, [1., -2.], .5, {"purpose": "synthetic"})
    path = tmp_path / "ridge.json"
    original.save(path)
    loaded = models.load_model_bytes(path.read_bytes())
    assert loaded.model_id == models.load_model(path).model_id == original.model_id
    assert list(loaded.predict(frame)) == list(original.predict(frame))
    payload = json.loads(path.read_bytes())
    payload["intercept"] += 1
    with pytest.raises(ValueError, match="identity"):
        models.load_model_bytes(json.dumps(payload).encode())
