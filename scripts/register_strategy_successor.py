"""Freeze a repaired candidate in a new directory without opening any holdout."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.strategy_review import freeze_prospective, validate_prospective
from scripts.roadmap_baseline import source_manifest, verify_source


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n")


def _utc_now():
    return datetime.now(timezone.utc)


def register_successor(*, source_root, historical_batch, output):
    root, historical, output = map(Path, (source_root, historical_batch, output))
    old_path = historical / "prospective_protocol.json"
    old_bytes = old_path.read_bytes()
    old = validate_prospective(json.loads(old_bytes))
    if old["opened_at"] is not None or old["data_access_log"] or old["status"] != "pending_unseen_evidence":
        raise ValueError("Opened historical samples cannot become a new unseen candidate")
    if old_path.with_suffix(".json.opened").exists():
        raise ValueError("Historical protocol has an exclusive opening receipt")
    registry = historical / "review_protocol.json"
    if _hash(registry) != old["experiment_registry_hash"]:
        raise ValueError("Historical experiment registry identity mismatch")
    files = source_manifest(root)
    config_hash = files.get("config/params.yaml")
    if config_hash != old["config_hash"]:
        raise ValueError("Formal configuration changed; requires a separately specified protocol")
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if identity == old["code_hash"]:
        raise ValueError("Successor must have a distinct source identity")
    point = _utc_now().isoformat()
    # No backdating the replacement candidate to inherit elapsed observation.
    from pandas import Timestamp
    now = Timestamp(point)
    if now.tzinfo is None or now <= Timestamp(old["registered_at"]):
        raise ValueError("Successor registration must follow the original registration")
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "source"
    for relative, digest in files.items():
        src, dst = root / relative, snapshot / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if _hash(dst) != digest:
            raise ValueError(f"Source changed while freezing: {relative}")
    verify_source(root, files)
    verify_source(snapshot, files)
    shutil.copyfile(registry, output / "review_protocol.json")
    if _hash(output / "review_protocol.json") != old["experiment_registry_hash"]:
        raise ValueError("Historical registry changed during registration")
    (output / "parent_protocol.json").write_bytes(old_bytes)
    protocol = freeze_prospective(output / "prospective_protocol.json", code_hash=identity,
        config_hash=config_hash, registry_hash=old["experiment_registry_hash"], frozen_at=point)
    _write(output / "source_manifest.json", {
        "schema": "strategy_successor_source/v1", "source_sha256": identity, "files": files,
        "snapshot": "source", "includes_untracked_controlled_source": True,
    })
    receipt = {
        "schema": "strategy_successor_registration/v1", "task_id": "SYS-11",
        "engineering_status": "pass", "research_status": "pending_unseen_evidence",
        "admission": "paused_revalidation", "registered_at": now.isoformat(),
        "parent_protocol_sha256": hashlib.sha256(old_bytes).hexdigest(),
        "parent_code_hash": old["code_hash"], "code_hash": identity,
        "config_hash": config_hash, "protocol_hash": protocol["protocol_hash"],
        "change_reason": "Repaired source and isolated capability additions; unchanged formal configuration",
        "historical_research_result": "fail", "historical_results_reused_for_admission": False,
        "old_protocol_modified": False, "new_observation_inherits_elapsed_days": False,
        "test_start": protocol["test_start"], "test_end_exclusive": protocol["test_end_exclusive"],
        "mature_after": protocol["mature_after"], "opened_at": None,
        "limitations": ["Registration is not independent strategy validation or approval to trade.",
                        "Selection and target-position modules are disabled for the official candidate.",
                        "Historical data, capacity and full-account evidence remain outstanding."],
    }
    if old_path.read_bytes() != old_bytes:
        raise ValueError("Historical protocol changed during registration")
    _write(output / "acceptance.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = register_successor(source_root=ROOT, historical_batch=args.historical_batch,
                                 output=args.output)
    print(json.dumps({k: receipt[k] for k in ("code_hash", "test_start", "mature_after", "admission")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
