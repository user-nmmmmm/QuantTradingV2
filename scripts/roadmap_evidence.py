"""Bind historical completion envelopes to their retained source/input bytes.

This validates recorded local identities only. It does not rerun the source or
establish that the current working tree still has the historical identity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re


def _path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("source binding path must be nonempty text")
    relative = PurePosixPath(name.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or ":" in name:
        raise ValueError(f"source binding path outside repository: {name}")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"source binding path outside repository: {name}")
    return result


def _digest(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label}: lowercase SHA-256 required")
    return value


def _reference(root: Path, value) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise ValueError("source binding reference must contain path and sha256")
    return _path(root, value.get("path")), _digest(value.get("sha256"), "reference")


def _verify_file(path: Path, digest: str) -> None:
    if not path.is_file():
        raise ValueError(f"missing historical source/input: {path.name}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"historical source/input hash mismatch: {path.name}")


def _read_manifest(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate source manifest key: {key}")
            result[key] = value
        return result

    result = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("source identity must be a JSON manifest object")
    return result


def _member(snapshot: Path, name: str) -> Path:
    # A manifest member must remain inside both the repository and its own
    # snapshot, including after resolving any filesystem links.
    return _path(snapshot, name)


def _engineering_members(snapshot: Path, manifest: dict) -> dict[Path, str]:
    entries = manifest.get("files", manifest.get("source_sha256"))
    if not isinstance(entries, dict) or not entries:
        raise ValueError("engineering source identity requires nonempty files/source_sha256 map")
    members = {}
    implementation_count = 0
    for name, value in entries.items():
        member = _member(snapshot, name)
        digest = _digest(value.get("sha256") if isinstance(value, dict) else value, "source member")
        if member in members:
            raise ValueError(f"duplicate normalized source member: {name}")
        members[member] = digest
        relative = PurePosixPath(name.replace("\\", "/"))
        if (relative.suffix == ".py" and "tests" not in relative.parts
                and relative.name != "conftest.py" and not relative.name.startswith("test_")):
            implementation_count += 1
    if not implementation_count:
        raise ValueError("engineering source identity must include Python implementation source")
    return members


def _documentation_members(root: Path, snapshot: Path, manifest: dict) -> tuple[dict, dict]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("documentation source identity requires nonempty archive snapshot list")
    members = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("archive snapshot entry must be an object")
        member = _member(snapshot, entry.get("snapshot"))
        if member in members:
            raise ValueError("duplicate normalized archive snapshot")
        members[member] = _digest(entry.get("sha256"), "archive snapshot")
    inputs = dict(members)
    for field, key, base in (("derived_artifacts", "path", snapshot),
                             ("input_sources", "source", root)):
        entries = manifest.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"archive {field} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"archive {field} entry must be an object")
            member = _path(base, entry.get(key))
            digest = _digest(entry.get("sha256"), field)
            if member in inputs and inputs[member] != digest:
                raise ValueError("conflicting archive input identity")
            inputs[member] = digest
    return members, inputs


def verify_source_binding(root: Path, envelope: dict, *, structure_only=False) -> dict:
    """Reject receipt-shaped source identities and inputs unrelated to the source.

    Engineering fixed inputs are explicit members of the same frozen manifest.
    Tests containing deterministic fixtures and frozen configuration files are
    supported. Archive inputs may additionally use the recorded input_sources
    or derived_artifacts. Structure-only mode never reads report/source files.
    """
    snapshot = _path(root, envelope.get("source_snapshot_root"))
    manifest_path, manifest_digest = _reference(root, envelope.get("source_identity"))
    fixed = envelope.get("fixed_inputs")
    if not isinstance(fixed, list) or not fixed:
        raise ValueError("source binding requires fixed inputs")
    input_refs = [_reference(root, item) for item in fixed]
    if len({path for path, _ in input_refs}) != len(input_refs):
        raise ValueError("duplicate fixed input references")
    kind = envelope.get("kind")
    if kind not in {"engineering", "documentation"}:
        raise ValueError("source binding requires a known envelope kind")
    if structure_only:
        return {"status": "source_binding_metadata_only", "members_verified": 0,
                "inputs_verified": 0, "current_source_revalidated": False}
    _verify_file(manifest_path, manifest_digest)
    manifest = _read_manifest(manifest_path)
    if "task_id" in manifest and manifest["task_id"] != envelope.get("task_id"):
        raise ValueError("source manifest task_id mismatch")
    if kind == "engineering":
        members = _engineering_members(snapshot, manifest)
        permitted_inputs = members
    else:
        members, permitted_inputs = _documentation_members(root, snapshot, manifest)
    for path, digest in members.items():
        _verify_file(path, digest)
    for path, digest in input_refs:
        if permitted_inputs.get(path) != digest:
            raise ValueError(f"fixed input is not bound to source identity: {path.name}")
        _verify_file(path, digest)
    return {"status": "historical_source_binding_verified", "members_verified": len(members),
            "inputs_verified": len(input_refs), "current_source_revalidated": False}
