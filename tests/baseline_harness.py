"""Utilities for immutable Batch 0 backtest baselines."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any

ARTIFACTS = ("orders", "fills", "closed_trades", "equity", "metrics")

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def data_digest(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()

def load_bundle(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        bundle = json.load(stream)
    required = {"schema_version", "scenario", "metadata", "data", "artifacts"}
    missing = required.difference(bundle)
    if missing:
        raise ValueError(f"{path.name} missing keys: {sorted(missing)}")
    if tuple(bundle["artifacts"].keys()) != ARTIFACTS:
        raise ValueError(f"{path.name} artifact order/keys must be {ARTIFACTS}")
    if data_digest(bundle["data"]) != bundle["metadata"]["data_sha256"]:
        raise ValueError(f"{path.name} data_sha256 mismatch")
    return bundle

def materialize(bundle: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {}
    for name in ARTIFACTS:
        value = bundle["artifacts"][name]
        target = output_dir / f"{name}.json"
        target.write_text(canonical_json(value) + "\n", encoding="utf-8")
        result[name] = json.loads(target.read_text(encoding="utf-8"))
    manifest = {"schema_version": bundle["schema_version"], "scenario": bundle["scenario"], "metadata": bundle["metadata"], "artifacts": list(ARTIFACTS)}
    (output_dir / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    result["manifest"] = manifest
    return result
