"""Historical delivery archives cannot disappear from the retention gate."""
from copy import deepcopy
import json

import pytest

from scripts import check_repository_hygiene as hygiene


@pytest.fixture
def retained_manifest(tmp_path, monkeypatch):
    manifest = json.loads(hygiene.MANIFEST.read_text(encoding="utf-8"))
    manifest["tracked_reports"] = []
    manifest["reference_documents"] = []
    manifest["tracked_market_data"] = {"path_prefix": "data/", "tracked_csv_count": 0,
                                       "manifest_path": "data/manifest.json"}
    path = tmp_path / "manifest.json"
    monkeypatch.setattr(hygiene, "ROOT", tmp_path)
    monkeypatch.setattr(hygiene, "MANIFEST", path)
    monkeypatch.setattr(hygiene, "_tracked_paths", lambda: {"data/manifest.json"})
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hygiene.check() == []
    return path, manifest


@pytest.mark.parametrize("change", ["section", "files", "one_entry"])
def test_external_archive_inventory_cannot_be_silently_removed(retained_manifest, change):
    path, manifest = retained_manifest
    if change == "section":
        del manifest["external_archives"]
    elif change == "files":
        manifest["external_archives"]["files"] = []
    else:
        manifest["external_archives"]["files"].pop()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hygiene.check()


@pytest.mark.parametrize("field,value", [
    ("sha256", "z" * 64), ("sha256", "0" * 64), ("size_bytes", True),
    ("size_bytes", -1), ("url", "https://example.invalid/replaced.zip"),
    ("former_path", "reports/different.zip"),
])
def test_external_archive_identity_is_pinned_when_optional_copy_is_absent(retained_manifest, field, value):
    path, manifest = retained_manifest
    manifest["external_archives"]["files"][0][field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hygiene.check()


@pytest.mark.parametrize("name", [r"..\outside.zip", r"C:\outside.zip", "../outside.zip", "", ".."])
def test_external_archive_name_is_a_portable_basename(retained_manifest, name):
    _, manifest = retained_manifest
    entry = deepcopy(manifest["external_archives"]["files"][0])
    entry["name"] = name
    errors = []
    hygiene._verify_external_archive(entry, set(), errors)
    assert errors


@pytest.mark.parametrize("change", ["duplicate", "extra", "null", "not_record", "wrong_release"])
def test_external_archive_inventory_is_complete_and_structured(retained_manifest, change):
    path, manifest = retained_manifest
    external = manifest["external_archives"]
    if change == "duplicate":
        external["files"].append(deepcopy(external["files"][0]))
    elif change == "extra":
        entry = deepcopy(external["files"][0])
        entry["name"] = "unexpected.zip"
        external["files"].append(entry)
    elif change == "null":
        external["files"] = None
    elif change == "not_record":
        external["files"][0] = []
    else:
        external["release"] = "different-release"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hygiene.check()


def test_archive_order_does_not_change_identity_and_corrupt_optional_copy_fails(retained_manifest):
    path, manifest = retained_manifest
    manifest["external_archives"]["files"].reverse()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hygiene.check() == []
    entry = manifest["external_archives"]["files"][0]
    local = path.parent / "reports" / entry["name"]
    local.parent.mkdir()
    local.write_bytes(b"incomplete archive")
    assert any("size changed" in message for message in hygiene.check())
