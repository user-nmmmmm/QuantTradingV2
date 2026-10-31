"""Register a new frozen identity bound to an annotated release tag (infrastructure roadmap A5, ADR 0032).

``register_strategy_successor.py`` froze a candidate from a working tree and refused any change of the formal
configuration. After the restructuring (roadmap track B) the registered identity no longer matches ``main`` and the
configuration gained the disabled ``allocation.capital`` block, so the new registration:

* binds an annotated release tag and its behavioral identity (``release-bound-identity/v1``, ADR 0010) instead of a
  byte manifest of a working tree;
* accepts a changed configuration only through a machine-checked change statement: the parent configuration, the
  tagged configuration and the declared additions must agree, and every addition must be disabled by default;
* leaves the parent protocol, its receipts and its research conclusions untouched, opens no sample, and starts a fresh
  observation clock at registration time (no backdating, no inherited elapsed days).

This command writes only into a new ``--output`` directory. It does not create or move tags and does not touch
``reports/`` of the parent. Registering is not validation of the strategy and not approval to trade.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

import yaml

from analysis.strategy_review import exclusive_path_lock, freeze_prospective, validate_prospective
from core.foundation.source_identity import bind_release
from scripts.register_strategy_successor import _hash, _write
from scripts.release_check import verify_tag

ROOT = Path(__file__).resolve().parents[1]
STATEMENT_SCHEMA = "config-change-statement/v1"
CONFIG_FILE = "config/params.yaml"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_hash_at(root: Path, commit: str) -> tuple[str, bytes]:
    """Hash of ``config/params.yaml`` at a commit with line endings normalized, so every checkout agrees."""
    data = _git(root, "cat-file", "blob", f"{commit}:{CONFIG_FILE}").replace(b"\r\n", b"\n")
    return _sha(data), data


def structural_diff(old: Any, new: Any, path: str = "") -> List[tuple]:
    """Differences between two parsed configurations as ``("added"|"removed"|"changed", dotted.path)``."""
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return [] if old == new else [("changed", path)]
    out: List[tuple] = []
    for key in sorted(set(old) | set(new), key=str):
        here = f"{path}.{key}" if path else str(key)
        if key not in old:
            out.append(("added", here))
        elif key not in new:
            out.append(("removed", here))
        else:
            out.extend(structural_diff(old[key], new[key], here))
    return out


def _lookup(config: Dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        node = node[part]
    return node


def check_config_change(statement: Dict[str, Any], *, parent_config: bytes, new_config: bytes,
                        parent_config_hash: str, new_config_hash: str) -> None:
    """The statement is evidence only if the files agree with it; it never widens what a registration may change."""
    if statement.get("schema") != STATEMENT_SCHEMA:
        raise ValueError("unsupported configuration change statement")
    if not str(statement.get("reason") or "").strip():
        raise ValueError("the configuration change statement must give a reason")
    if statement.get("parent_config_sha256") != parent_config_hash:
        raise ValueError("the statement does not name the parent's frozen configuration")
    if statement.get("new_config_sha256") != new_config_hash:
        raise ValueError("the statement does not name the tagged configuration")
    declared = statement.get("added")
    if not isinstance(declared, list) or not declared or any(not isinstance(item, str) for item in declared):
        raise ValueError("the statement must list the added configuration paths")
    old, new = yaml.safe_load(parent_config), yaml.safe_load(new_config)
    actual = structural_diff(old, new)
    if actual != [("added", path) for path in sorted(declared)]:
        raise ValueError(f"the configuration differs from the statement: {actual}")
    for path in declared:
        block = _lookup(new, path)
        if not isinstance(block, dict) or block.get("enabled") is not False:
            raise ValueError(f"added block {path} must be disabled by default (enabled: false)")


def register_bound_successor(*, repo_root, tag: str, historical_batch, config_change, output) -> Dict[str, Any]:
    historical = Path(historical_batch)
    with exclusive_path_lock(historical / "prospective_protocol.json"):
        return _register_bound_successor(repo_root=repo_root, tag=tag, historical_batch=historical,
                                         config_change=config_change, output=output)


def _publish_registration(staged: Path, output: Path) -> None:
    """Atomically publish a complete directory, refusing every existing destination entry.

    Windows rename already refuses replacement. Linux needs renameat2's RENAME_NOREPLACE;
    ordinary POSIX rename can replace an empty directory after an existence check. Missing
    native support (including unsupported filesystems) fails closed without a rename fallback.
    """
    if sys.platform == "win32":
        staged.rename(output)
        return
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Atomic no-replace registration publication is unsupported", str(output))
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "Atomic no-replace registration publication requires renameat2", str(output)) from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    # AT_FDCWD = -100; RENAME_NOREPLACE = 1. Both paths share the same parent filesystem.
    if renameat2(-100, os.fsencode(staged), -100, os.fsencode(output), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(staged), None, str(output))


def _register_bound_successor(*, repo_root, tag, historical_batch, config_change, output) -> Dict[str, Any]:
    root, historical, output = Path(repo_root), Path(historical_batch), Path(output)
    output = output.parent.resolve() / output.name
    if os.path.lexists(output):
        raise FileExistsError(f"Registration output already exists: {output}")
    statement_path = Path(config_change)
    problems = verify_tag(tag, root=root)
    if problems:
        raise ValueError("release tag is not usable: " + "; ".join(problems))
    binding = bind_release(root, tag)
    commit = binding["tag_commit"]

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
    parent_config = (historical / "source" / CONFIG_FILE).read_bytes()
    if _sha(parent_config) != old["config_hash"]:
        raise ValueError("the parent's source snapshot does not match its frozen configuration")

    new_hash, new_config = config_hash_at(root, commit)
    statement_bytes = statement_path.read_bytes()
    check_config_change(json.loads(statement_bytes), parent_config=parent_config, new_config=new_config,
                        parent_config_hash=old["config_hash"], new_config_hash=new_hash)

    identity = binding["receipt"]["identity_sha256"]
    point = _utc_now()
    tagged_at = datetime.fromisoformat(_git(root, "for-each-ref", f"refs/tags/{tag}",
                                            "--format=%(taggerdate:iso-strict)").decode().strip())
    if point.tzinfo is None or point <= datetime.fromisoformat(old["registered_at"]):
        raise ValueError("Successor registration must follow the original registration")
    if tagged_at > point:
        raise ValueError("the release tag is dated after the registration")

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{output.name}.registration-", dir=output.parent) as temporary:
        staged = Path(temporary)
        receipt = _stage_registration(staged, registry=registry, old_bytes=old_bytes, old=old,
                                      statement_bytes=statement_bytes, binding=binding, new_hash=new_hash,
                                      point=point, tag=tag, commit=commit, tagged_at=tagged_at, identity=identity)
        # The sample opener holds the same protocol lock. Recheck even non-cooperating/older writers.
        if old_path.with_suffix(".json.opened").exists():
            raise ValueError("Historical protocol has an exclusive opening receipt")
        if old_path.read_bytes() != old_bytes:
            raise ValueError("Historical protocol changed during registration")
        if _hash(registry) != old["experiment_registry_hash"]:
            raise ValueError("Historical experiment registry changed during registration")
        with exclusive_path_lock(output):
            if os.path.lexists(output):
                raise FileExistsError(f"Registration output already exists: {output}")
            _publish_registration(staged, output)
        return receipt


def _stage_registration(output, *, registry, old_bytes, old, statement_bytes, binding, new_hash,
                        point, tag, commit, tagged_at, identity):
    shutil.copyfile(registry, output / "review_protocol.json")
    if _hash(output / "review_protocol.json") != old["experiment_registry_hash"]:
        raise ValueError("Historical experiment registry changed during registration")
    (output / "parent_protocol.json").write_bytes(old_bytes)
    (output / "config_change.json").write_bytes(statement_bytes)
    _write(output / "identity_binding.json", binding)
    protocol = freeze_prospective(output / "prospective_protocol.json", code_hash=identity, config_hash=new_hash,
                                  registry_hash=old["experiment_registry_hash"], frozen_at=point.isoformat())
    receipt = {
        "schema": "bound_successor_registration/v1", "task_id": "A5",
        "engineering_status": "pass", "research_status": "pending_unseen_evidence",
        "admission": "paused_revalidation", "registered_at": point.isoformat(),
        "tag": tag, "tag_commit": commit, "tag_dated": tagged_at.isoformat(),
        "identity_scheme": binding["receipt"]["scheme"], "code_hash": identity,
        "identity_binding_sha256": _hash(output / "identity_binding.json"),
        "config_hash": new_hash, "config_hash_basis": "sha256 of config/params.yaml at the tagged commit, LF-normalized",
        "config_change_sha256": _sha(statement_bytes),
        "parent_protocol_sha256": _sha(old_bytes), "parent_code_hash": old["code_hash"],
        "parent_config_hash": old["config_hash"], "protocol_hash": protocol["protocol_hash"],
        "historical_research_result": "fail", "historical_results_reused_for_admission": False,
        "old_protocol_modified": False, "new_observation_inherits_elapsed_days": False,
        "test_start": protocol["test_start"], "test_end_exclusive": protocol["test_end_exclusive"],
        "mature_after": protocol["mature_after"], "opened_at": None,
        "limitations": ["Registration is not independent strategy validation or approval to trade.",
                        f"The code identity uses {binding['receipt']['scheme']}; it does not prove runtime equivalence.",
                        "Historical data, capacity and full-account evidence remain outstanding."],
    }
    _write(output / "acceptance.json", receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="annotated release tag to bind (e.g. v3.1.0)")
    parser.add_argument("--historical-batch", type=Path, required=True)
    parser.add_argument("--config-change", type=Path, required=True, help="config-change-statement/v1 JSON")
    parser.add_argument("--output", type=Path, required=True, help="new directory; never overwritten")
    args = parser.parse_args(argv)
    receipt = register_bound_successor(repo_root=ROOT, tag=args.tag, historical_batch=args.historical_batch,
                                       config_change=args.config_change, output=args.output)
    print(json.dumps({k: receipt[k] for k in ("tag", "code_hash", "config_hash", "registered_at", "test_start",
                                              "mature_after", "admission")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
