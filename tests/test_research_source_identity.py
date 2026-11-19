"""PR25-02 byte identity checks against mutations independent of Git tracking."""

from __future__ import annotations

import hashlib
import json

import pytest

from analysis.research_evidence import ResearchEvidenceRun, source_identity


def write(root, relative, value=b"initial"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def run(root, **kwargs):
    return ResearchEvidenceRun(
        candidates=["a"], parameters={}, data_map={}, source_root=root, **kwargs
    )


@pytest.mark.parametrize(
    "relative",
    [
        "research/ml_selection/models.py",
        "live_trading/execution_adapter.py",
        "tests/test_ml_selection_models.py",
        "requirements-ml.txt",
        "requirements-dev.txt",
        "requirements.lock.sha256",
        "pyproject.toml",
        "config/model.bin",
        "docs/research/current_strategy_protocol.json",
        "docs/research/paper_validation_contract.md",
        ".github/workflows/tests.yml",
        "data/loaders.py",
    ],
)
def test_default_identity_covers_ml_runtime_tests_protocols_and_dependencies(tmp_path, relative):
    path = write(tmp_path, relative)
    before = source_identity(tmp_path)
    assert before[relative] == hashlib.sha256(b"initial").hexdigest()
    path.write_bytes(b"changed")
    assert source_identity(tmp_path)[relative] != before[relative]


def test_default_ignores_bytecode_data_and_generated_acceptance(tmp_path):
    write(tmp_path, "research/ml_selection/models.py")
    before = source_identity(tmp_path)
    write(tmp_path, "research/ml_selection/__pycache__/models.pyc")
    write(tmp_path, "data/history.csv")
    write(tmp_path, "docs/research/new_acceptance.json")
    assert source_identity(tmp_path) == before


def test_explicit_directory_detects_untracked_add_delete_and_binary_modification(tmp_path):
    removed = write(tmp_path, "study/new_untracked.py")
    model = write(tmp_path, "study/model.weights", b"model-v1")
    journal = run(tmp_path, source_paths=["study"])
    original = dict(journal.registration["source_hashes"])
    removed.unlink()
    model.write_bytes(b"model-v2")
    write(tmp_path, "study/another_untracked.py")
    result = journal.verify_identity({})
    assert result["source_unchanged"] is False and result["identity_changed"] is True
    assert result["source_changes"] == {
        "added": ["study/another_untracked.py"],
        "deleted": ["study/new_untracked.py"],
        "modified": ["study/model.weights"],
    }
    assert journal.registration["source_hashes"] == original


def test_default_detects_a_new_untracked_source(tmp_path):
    write(tmp_path, "research/ml_selection/models.py")
    journal = run(tmp_path)
    write(tmp_path, "research/ml_selection/new_code.py")
    result = journal.verify_identity({})
    assert result["source_changes"]["added"] == ["research/ml_selection/new_code.py"]
    assert result["identity_changed"]


def test_explicit_file_deletion_has_named_difference(tmp_path):
    source = write(tmp_path, "study.py")
    journal = run(tmp_path, source_paths=["study.py"])
    source.unlink()
    result = journal.verify_identity({})
    assert result["source_changes"]["deleted"] == ["study.py"]
    assert result["identity_changed"]


def test_explicit_manifest_must_match_actual_bytes_before_registration(tmp_path):
    write(tmp_path, "study.py")
    with pytest.raises(ValueError, match="manifest does not match"):
        run(tmp_path, source_hashes={"study.py": "not-the-current-digest"})


def test_locally_verified_external_manifest_is_copied_and_detects_model_drift(tmp_path):
    model = write(tmp_path, "model.pkl", b"model-a")
    hashes = source_identity(tmp_path, paths=["model.pkl"])
    journal = run(tmp_path, source_hashes=hashes)
    assert journal.verify_identity({})["source_unchanged"] is True
    model.write_bytes(b"model-b")
    hashes["model.pkl"] = hashlib.sha256(b"model-b").hexdigest()
    result = journal.verify_identity({})
    assert result["source_unchanged"] is False
    assert result["source_changes"]["modified"] == ["model.pkl"]


def test_external_directory_manifest_requires_exact_registered_closure(tmp_path):
    write(tmp_path, "study/a.py")
    hashes = source_identity(tmp_path, paths=["study"])
    write(tmp_path, "study/b.py")
    with pytest.raises(ValueError, match="declared closure"):
        run(tmp_path, source_hashes=hashes, source_paths=["study"])


def test_legacy_external_hashes_remain_unverified():
    journal = ResearchEvidenceRun(
        candidates=["a"], parameters={}, data_map={}, source_hashes={"external.py": "unverified"}
    )
    result = journal.verify_identity({})
    assert result["source_unchanged"] is None
    assert result["source_verification"] == "external_identity_not_verified"


@pytest.mark.parametrize("relative", ["../escape.py", "study/../../escape.py"])
def test_path_traversal_is_rejected_before_registration(tmp_path, relative):
    with pytest.raises(ValueError, match="parent traversal"):
        source_identity(tmp_path, paths=[relative])


def test_absolute_path_outside_root_is_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "outside.py")
    with pytest.raises(ValueError, match="escapes"):
        source_identity(root, paths=[outside])


def test_missing_declared_source_refuses_a_new_identity(tmp_path):
    with pytest.raises(FileNotFoundError, match="declared source"):
        run(tmp_path, source_paths=["missing-model.pkl"])


@pytest.mark.parametrize("paths", ["study.py", b"study.py"])
def test_single_string_is_not_silently_iterated_as_path_list(tmp_path, paths):
    with pytest.raises(ValueError, match="sequence"):
        source_identity(tmp_path, paths=paths)


def test_declared_scope_is_persisted_and_generator_candidates_are_planned(tmp_path):
    write(tmp_path, "study/a.py")
    journal = ResearchEvidenceRun(
        candidates=(str(n) for n in range(2)),
        parameters={},
        data_map={},
        output=tmp_path / "run",
        source_root=tmp_path,
        source_paths=["study"],
    )
    stored = json.loads((journal.output / "registration.json").read_text(encoding="utf-8"))
    assert stored["source_scope"] == {
        "policy": "explicit_paths/v1",
        "paths": ["study"],
        "verification": "local_worktree",
    }
    assert [event["candidate"] for event in journal.events] == ["0", "1"]
    assert journal.verify_identity({})["source_unchanged"] is True


def test_symlink_escape_never_reads_external_source(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "outside.bin")
    link = root / "model.bin"
    try:
        link.symlink_to(outside)
        declared = ["model.bin"]
    except (OSError, NotImplementedError):
        import os
        import subprocess

        if os.name != "nt":
            pytest.skip("symlink creation is unavailable on this host")
        # Directory junctions require no symlink privilege on Windows and
        # exercise the same real-path escape invariant with actual filesystem IO.
        target = tmp_path / "external_models"
        target.mkdir()
        write(target, "model.bin")
        link = root / "linked_models"

        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
                + quote(link)
                + " -Target "
                + quote(target)
                + " | Out-Null",
            ],
            capture_output=True,
            timeout=20,
        )
        if result.returncode:
            pytest.skip("neither symlinks nor directory junctions are available on this host")
        declared = ["linked_models"]
    with pytest.raises(ValueError, match="escapes"):
        source_identity(root, paths=declared)


def test_unreadable_registered_source_fails_closed(monkeypatch, tmp_path):
    import core.foundation.source_bytes as module

    write(tmp_path, "model.pkl")
    journal = run(tmp_path, source_paths=["model.pkl"])

    def denied(path):
        raise PermissionError("fixture permission denied")

    monkeypatch.setattr(module, "sha256_file", denied)
    result = journal.verify_identity({})
    assert result["source_unchanged"] is False and result["identity_changed"]
    assert result["source_error"].startswith("PermissionError:")
