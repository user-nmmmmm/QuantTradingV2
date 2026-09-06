"""Portable evidence integrity checks; no market data, network or credentials."""
import json
import zipfile

import pytest

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
