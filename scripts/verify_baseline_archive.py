"""Verify historical Phase 0 artifacts against pinned Git blob bytes.

The v1 manifests are retained as history; their Windows checkout hashes are
not portable. The v2 manifest records committed bytes and explicit LF text
normalization for checkout verification. No permissions or current HEAD
equality are used as evidence of immutability.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/baseline/phase0_verification_v2.json"


def verify_archive(root: Path = ROOT, manifest_path: Path = MANIFEST) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    revision = manifest["archive_revision"]
    subprocess.check_call(["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=root)
    protected_roots = ("docs/baseline/phase0/archived_reports", "docs/baseline/phase0/config_snapshot")
    for prefix in protected_roots:
        expected = {name for name in manifest["files"] if name.startswith(prefix + "/")}
        recorded = set(subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", revision, "--", prefix], cwd=root
        ).decode("utf-8").splitlines())
        current = {p.relative_to(root).as_posix() for p in (root / prefix).rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts}
        if expected != recorded or current != expected:
            raise ValueError(f"Protected archive member set differs: {prefix}")
    checked = 0
    for name, record in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Archive manifest escapes repository")
        blob = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=root)
        if hashlib.sha256(blob).hexdigest() != record["sha256"]:
            raise ValueError(f"Pinned blob differs from manifest: {name}")
        content = path.read_bytes()
        if record["checkout_policy"] == "text-lf":
            content = content.replace(b"\r\n", b"\n")
        if content != blob:
            raise ValueError(f"Protected historical archive changed: {name}")
        checked += 1
    return {"schema_version": "archive-verification/v2", "passed": True,
            "archive_revision": revision, "files_verified": checked,
            "historical_strategy_revision": manifest["historical_strategy_revision"]}


if __name__ == "__main__":
    print(json.dumps(verify_archive(), indent=2))
