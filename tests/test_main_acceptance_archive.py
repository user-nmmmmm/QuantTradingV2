"""Portable evidence integrity checks; no market data, network or credentials."""
import io
import json
import subprocess
import sys
import zipfile

import pytest

from scripts import main_acceptance as module
from scripts.main_acceptance import sha, verify_bundle


def bundle(tmp_path, *, payload=b"evidence", recorded=b"evidence", extra=None):
    path = tmp_path / "evidence.zip"
    manifest = {"files": {"evidence/run.json": {"sha256": sha(recorded), "bytes": len(recorded)}}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("evidence/run.json", payload)
        archive.writestr("bundle_manifest.json", json.dumps(manifest))
        if extra:
            # ZipInfo normally normalizes backslashes on Windows; preserve
            # hostile member bytes to exercise cross-platform verification.
            info = zipfile.ZipInfo()
            info.filename = extra
            archive.writestr(info, b"unlisted")
    return path


def test_verified_bundle_reports_external_checksum(tmp_path):
    path = bundle(tmp_path)
    result = verify_bundle(path)
    assert result["passed"]
    assert result["members_verified"] == 1
    assert result["zip_sha256"] == sha(path.read_bytes())


def test_tampered_payload_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_bundle(bundle(tmp_path, payload=b"tampered"))


def test_unlisted_member_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="member set"):
        verify_bundle(bundle(tmp_path, extra="unexpected.txt"))


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "C:/drive.txt", "bad\\path.txt"])
def test_unsafe_member_is_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="Unsafe"):
        verify_bundle(bundle(tmp_path, extra=name))


def test_duplicate_member_is_rejected(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate"):
        path = bundle(tmp_path, extra="evidence/run.json")
    with pytest.raises(ValueError, match="Duplicate"):
        verify_bundle(path)


@pytest.fixture()
def packaging_world(tmp_path, monkeypatch):
    """Package a real selected Git commit, with isolated synthetic input evidence."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    output = root / "acceptance"
    (output / "logs").mkdir(parents=True)
    source_manifest = root / "reports/20260902_211518_3239d_30Syms_Ret137.0pct/run_manifest.json"
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text(json.dumps({"data_snapshots": {}}), encoding="utf-8")
    test_path = root / "tests/test_main_acceptance_archive.py"
    test_path.parent.mkdir()
    test_path.write_text("# packaged verifier tests\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == sys.executable:
            assert command[1:] in (
                ["scripts/run_portable_tests.py", "-q", "tests/test_main_acceptance_archive.py"],
                ["-m", "ruff", "check", "scripts/main_acceptance.py", "tests/test_main_acceptance_archive.py"],
            )
            return subprocess.CompletedProcess(command, 0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", run)
    return root, output, tmp_path / "new-bundle.zip"


def commit_packaging_source(world, metadata):
    root, output, _ = world
    if metadata is not None:
        (root / "pyproject.toml").write_text(metadata, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "source"],
                   cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    module.write_json(output / "acceptance.json", {"commit": commit, "local_passed": True})
    module.write_json(output / "remote_ci.json", {"head_sha": commit, "event": "push", "branch": "main",
                                               "conclusion": "success", "url": "https://example.test/ci"})
    # The packager checkout may differ from the selected historical source commit.
    (root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.99"\n', encoding="utf-8")
    return commit


@pytest.mark.parametrize("requirement", [">=3.11.5", ">=3.13,<3.15"])
def test_package_instructions_follow_selected_source_commit(packaging_world, requirement):
    commit = commit_packaging_source(packaging_world, f'[project]\nrequires-python = "{requirement}"\n')
    _, output, destination = packaging_world
    assert module.package(output, destination) == 0
    assert verify_bundle(destination)["passed"]
    with zipfile.ZipFile(destination) as archive:
        instructions = archive.read("README.md").decode()
        assert f"Python environment satisfying `{requirement}`" in instructions
        assert ">=3.99" not in instructions
        assert f"Commit: {commit}" in instructions
        with zipfile.ZipFile(io.BytesIO(archive.read("source.zip"))) as source:
            assert f'requires-python = "{requirement}"' in source.read("pyproject.toml").decode()
    index = json.loads(destination.with_suffix(".index.json").read_text(encoding="utf-8"))
    assert index["engineering_acceptance_passed"] and index["commit"] == commit


@pytest.mark.parametrize("metadata", [None, '[project]\nname = "legacy"\n'])
def test_package_legacy_source_keeps_explicit_legacy_python_guidance(packaging_world, metadata):
    commit_packaging_source(packaging_world, metadata)
    _, output, destination = packaging_world
    assert module.package(output, destination) == 0
    assert verify_bundle(destination)["passed"]
    with zipfile.ZipFile(destination) as archive:
        instructions = archive.read("README.md").decode()
        assert "Python 3.11+ environment" in instructions
        assert "legacy source has no requires-python metadata" in instructions
        assert ">=3.99" not in instructions


@pytest.mark.parametrize("metadata", ['[project', '[project]\nrequires-python = 311\n',
                                    '[project]\nrequires-python = ""\n'])
def test_invalid_selected_python_metadata_does_not_publish_bundle(packaging_world, metadata):
    commit_packaging_source(packaging_world, metadata)
    _, output, destination = packaging_world
    with pytest.raises(ValueError):
        module.package(output, destination)
    assert not destination.exists() and not destination.with_suffix(".index.json").exists()
