"""Reject tracked scratch files and verify frozen repository artifacts.

Run this after checkout in CI. The allowlist is kept in
``docs/file_retention_manifest.json`` so exceptions have a recorded purpose.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "file_retention_manifest.json"
FORBIDDEN_PREFIXES = ("tmp/", ".claude/", ".vscode/")
FORBIDDEN_FILES = {"cua_probe.txt"}
AUTOMATION_WORKFLOW = ".github/workflows/backtest-automation.yml"
AUTOMATION_ENTRYPOINTS = (
    "config/automation.json",
    "scripts/run_automation.py",
    "tests/test_automation_runner.py",
)


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def _verify_frozen_file(entry: dict[str, object], errors: list[str]) -> None:
    path = entry.get("path")
    expected_hash = entry.get("sha256")
    expected_size = entry.get("size_bytes")
    if (
        not isinstance(path, str)
        or path.startswith("/")
        or ".." in Path(path).parts
        or not isinstance(expected_hash, str)
        or not isinstance(expected_size, int)
    ):
        errors.append(f"Invalid frozen-file entry: {entry!r}")
        return
    file_path = ROOT / path
    if not file_path.is_file():
        errors.append(f"Frozen artifact is missing: {path}")
        return
    if file_path.stat().st_size != expected_size:
        errors.append(f"Frozen artifact size changed: {path}")
        return
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        errors.append(f"Frozen artifact checksum changed: {path}")


def _verify_external_archive(entry: dict[str, object], tracked: set[str], errors: list[str]) -> None:
    """Archives kept outside git: metadata must be complete; a local copy, if any, must match."""
    name, size, digest, url = (entry.get(key) for key in ("name", "size_bytes", "sha256", "url"))
    if (not isinstance(name, str) or "/" in name or not isinstance(size, int)
            or not isinstance(digest, str) or len(digest) != 64
            or not isinstance(url, str) or not url.startswith("https://")):
        errors.append(f"Invalid external-archive entry: {entry!r}")
        return
    if any(path.rsplit("/", 1)[-1] == name for path in tracked):
        errors.append(f"External archive must not be tracked in git: {name}")
    local = ROOT / "reports" / name
    if local.is_file():  # optional local copy, e.g. downloaded from the release
        _verify_frozen_file({"path": f"reports/{name}", "size_bytes": size, "sha256": digest}, errors)


def check() -> list[str]:
    tracked = _tracked_paths()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors: list[str] = []

    for path in sorted(tracked):
        if path in FORBIDDEN_FILES or path.startswith(FORBIDDEN_PREFIXES):
            errors.append(f"Local or temporary file is tracked: {path}")

    report_entries = manifest["tracked_reports"]
    allowed_reports = {entry["path"] for entry in report_entries}
    if len(allowed_reports) != len(report_entries):
        errors.append("Duplicate reports in file retention manifest")
    tracked_reports = {path for path in tracked if path.startswith("reports/")}
    for path in sorted(tracked_reports - allowed_reports):
        errors.append(f"Unreviewed report is tracked: {path}")
    for path in sorted(allowed_reports - tracked_reports):
        errors.append(f"Report in manifest is not tracked: {path}")
    for entry in report_entries:
        _verify_frozen_file(entry, errors)

    market_data = manifest["tracked_market_data"]
    prefix = market_data["path_prefix"]
    csv_count = sum(
        path.startswith(prefix) and path.endswith(".csv") for path in tracked
    )
    if csv_count != market_data["tracked_csv_count"]:
        errors.append(
            f"Tracked market-data CSV count changed: {csv_count} "
            f"(expected {market_data['tracked_csv_count']})"
        )
    if market_data["manifest_path"] not in tracked:
        errors.append("Market-data manifest is not tracked")

    if AUTOMATION_WORKFLOW in tracked:
        for path in AUTOMATION_ENTRYPOINTS:
            if path not in tracked:
                errors.append(f"Automation workflow dependency is not tracked: {path}")

    external = manifest.get("external_archives", {})
    external_names = [entry.get("name") for entry in external.get("files", [])]
    if len(set(external_names)) != len(external_names):
        errors.append("Duplicate external archive in file retention manifest")
    for entry in external.get("files", []):
        _verify_external_archive(entry, tracked, errors)

    for entry in manifest["reference_documents"]:
        if entry["path"] not in tracked:
            errors.append(f"Reference document is not tracked: {entry['path']}")
        _verify_frozen_file(entry, errors)
    return errors


def main() -> int:
    try:
        errors = check()
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError, TypeError) as exc:
        print(f"Repository hygiene check could not run: {exc}")
        return 2
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Repository hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
