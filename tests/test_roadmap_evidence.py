"""Source/fixture identity must be a meaningful part of roadmap acceptance."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.roadmap_evidence import verify_source_binding


def write(root: Path, path: str, value) -> dict:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) if isinstance(value, (dict, list)) else value,
                      encoding="utf-8")
    return {"path": path, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def engineering(root: Path):
    source = write(root, "evidence/frozen/core/engine.py", "VALUE = 1\n")
    fixture = write(root, "evidence/frozen/tests/test_fixture.py", "INPUT = [1, 2, 3]\n")
    manifest = write(root, "evidence/source.json", {
        "task_id": "FIX-01", "files": {
            "core/engine.py": source["sha256"],
            "tests/test_fixture.py": {"sha256": fixture["sha256"]},
        }})
    return {"task_id": "FIX-01", "kind": "engineering", "source_identity": manifest,
            "source_snapshot_root": "evidence/frozen", "fixed_inputs": [fixture]}


def test_all_historical_members_and_fixture_are_verified(tmp_path):
    result = verify_source_binding(tmp_path, engineering(tmp_path))
    assert result["members_verified"] == 2
    assert result["inputs_verified"] == 1
    assert result["current_source_revalidated"] is False


def test_source_sha256_string_map_is_supported(tmp_path):
    envelope = engineering(tmp_path)
    manifest = json.loads((tmp_path / envelope["source_identity"]["path"]).read_text())
    manifest["source_sha256"] = manifest.pop("files")
    envelope["source_identity"] = write(tmp_path, "evidence/source.json", manifest)
    assert verify_source_binding(tmp_path, envelope)["members_verified"] == 2


@pytest.mark.parametrize("member", ["core/engine.py", "tests/test_fixture.py"])
def test_historical_member_drift_fails_even_with_unchanged_manifest(tmp_path, member):
    envelope = engineering(tmp_path)
    (tmp_path / "evidence/frozen" / member).write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_source_binding(tmp_path, envelope)


def test_missing_implementation_member_is_not_hidden_by_valid_fixture(tmp_path):
    envelope = engineering(tmp_path)
    (tmp_path / "evidence/frozen/core/engine.py").unlink()
    with pytest.raises(ValueError, match="missing historical source"):
        verify_source_binding(tmp_path, envelope)


def test_receipt_cannot_replace_source_manifest(tmp_path):
    envelope = engineering(tmp_path)
    envelope["source_identity"] = write(tmp_path, "evidence/receipt.json", {
        "task_id": "FIX-01", "engineering_status": "pass", "checks": []})
    with pytest.raises(ValueError, match="source identity requires"):
        verify_source_binding(tmp_path, envelope)


@pytest.mark.parametrize("keep_valid_fixture", [False, True])
def test_hashed_receipt_is_not_a_fixed_input(tmp_path, keep_valid_fixture):
    envelope = engineering(tmp_path)
    receipt = write(tmp_path, "evidence/receipt.json", {"engineering_status": "pass"})
    envelope["fixed_inputs"] = ([*envelope["fixed_inputs"], receipt]
                                if keep_valid_fixture else [receipt])
    with pytest.raises(ValueError, match="not bound to source identity"):
        verify_source_binding(tmp_path, envelope)


def test_identical_fixture_bytes_outside_snapshot_are_not_bound(tmp_path):
    envelope = engineering(tmp_path)
    envelope["fixed_inputs"] = [write(tmp_path, "current/tests/test_fixture.py", "INPUT = [1, 2, 3]\n")]
    with pytest.raises(ValueError, match="not bound to source identity"):
        verify_source_binding(tmp_path, envelope)


def test_wrong_task_and_test_only_source_manifest_are_rejected(tmp_path):
    envelope = engineering(tmp_path)
    source_path = tmp_path / envelope["source_identity"]["path"]
    manifest = json.loads(source_path.read_text())
    wrong_task = deepcopy(manifest)
    wrong_task["task_id"] = "FIX-02"
    envelope["source_identity"] = write(tmp_path, "evidence/source.json", wrong_task)
    with pytest.raises(ValueError, match="task_id mismatch"):
        verify_source_binding(tmp_path, envelope)
    del manifest["files"]["core/engine.py"]
    envelope["source_identity"] = write(tmp_path, "evidence/source.json", manifest)
    with pytest.raises(ValueError, match="implementation source"):
        verify_source_binding(tmp_path, envelope)


def test_archive_checks_every_snapshot_and_binds_derived_inputs(tmp_path):
    snapshot = write(tmp_path, "archive/sources/plan.md", "original plan\n")
    derived = write(tmp_path, "archive/emails/body.md", "archived message\n")
    manifest = write(tmp_path, "archive/manifest.json", {
        "files": [{"snapshot": "sources/plan.md", "sha256": snapshot["sha256"]}],
        "derived_artifacts": [{"path": "emails/body.md", "sha256": derived["sha256"]}],
    })
    envelope = {"task_id": "DOC-01", "kind": "documentation", "source_identity": manifest,
                "source_snapshot_root": "archive", "fixed_inputs": [derived]}
    assert verify_source_binding(tmp_path, envelope)["members_verified"] == 1
    (tmp_path / snapshot["path"]).write_text("later edit", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_source_binding(tmp_path, envelope)


def test_archive_rejects_unrecorded_derived_input(tmp_path):
    snapshot = write(tmp_path, "archive/sources/plan.md", "original\n")
    manifest = write(tmp_path, "archive/manifest.json", {
        "files": [{"snapshot": "sources/plan.md", "sha256": snapshot["sha256"]}]})
    envelope = {"task_id": "DOC-01", "kind": "documentation", "source_identity": manifest,
                "source_snapshot_root": "archive", "fixed_inputs": [
                    write(tmp_path, "archive/unrecorded.md", "unverified\n")]}
    with pytest.raises(ValueError, match="not bound to source identity"):
        verify_source_binding(tmp_path, envelope)


def test_structure_mode_never_reads_reports_but_requires_safe_snapshot_metadata(tmp_path):
    envelope = {"task_id": "FIX-01", "kind": "engineering", "source_snapshot_root": "ignored/source",
                "source_identity": {"path": "ignored/source.json", "sha256": "0" * 64},
                "fixed_inputs": [{"path": "ignored/source/tests/test_fixture.py", "sha256": "1" * 64}]}
    assert verify_source_binding(tmp_path, envelope, structure_only=True)["members_verified"] == 0
    envelope["source_snapshot_root"] = "../escape"
    with pytest.raises(ValueError, match="outside repository"):
        verify_source_binding(tmp_path, envelope, structure_only=True)
    del envelope["source_snapshot_root"]
    with pytest.raises(ValueError, match="nonempty text"):
        verify_source_binding(tmp_path, envelope, structure_only=True)


def test_manifest_member_cannot_escape_snapshot(tmp_path):
    envelope = engineering(tmp_path)
    envelope["source_identity"] = write(tmp_path, "evidence/source.json", {
        "files": {"../core/engine.py": "0" * 64}})
    with pytest.raises(ValueError, match="outside repository"):
        verify_source_binding(tmp_path, envelope)
