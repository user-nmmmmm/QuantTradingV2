"""Run a new immutable, preregistered finite statistical calibration bundle."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import platform
import shutil
from pathlib import Path
import sys


from analysis.research_evidence import source_identity
from analysis.search_scope import SearchScope, data_manifest
from analysis.statistical_calibration import (
    DEFAULT_CALIBRATION_DESIGN,
    calibration_job_ids,
    run_calibration,
    validate_calibration_design,
)
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file


ROOT = Path(__file__).resolve().parents[1]


def _digest(value):
    return sha256_bytes(canonical_json(value).encode())


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def snapshot_sources(root, destination, hashes):
    destination.mkdir(parents=True, exist_ok=False)
    for name, expected in hashes.items():
        path = (root / name).resolve()
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or not path.is_relative_to(root.resolve())
        ):
            raise ValueError("snapshot source must remain in its declared root")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256_file(target) != expected:
            raise ValueError("source changed while snapshotting: " + name)
    return {"root": destination.name, "files_verified": len(hashes)}


def calibrate(output, *, design=None):
    design = validate_calibration_design(
        deepcopy(DEFAULT_CALIBRATION_DESIGN if design is None else design)
    )
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("calibration bundle must be inside the repository")
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "design.json", design)
    import numpy as np
    import pandas as pd

    environment = {
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "ci_execution": False,
    }
    _write(output / "environment.json", environment)
    # The generated frozen design is an input in this explicit source closure.
    # Relative paths are rooted at the repository so its actual bytes are checked.
    paths = [
        "analysis/research_evidence.py",
        "analysis/search_scope.py",
        "analysis/paper_validation.py",
        "analysis/statistical_calibration.py",
        "analysis/research_validation.py",
        "core/metrics",
        "core/foundation/reproducibility.py",
        "core/foundation/data_identity.py",
        "core/__init__.py",
        "core/analytics/strategy_health.py",
        "scripts/calibrate_paper_statistics.py",
        "requirements.txt",
        "requirements.lock.txt",
        "requirements.lock.sha256",
        "pyproject.toml",
        "tests/test_registered_statistics.py",
        "tests/test_statistical_calibration.py",
        "tests/test_statistics_calibration_cli.py",
        "tests/test_data_identity.py",
        "tests/test_paper_validation.py",
        (output / "design.json").relative_to(ROOT).as_posix(),
        (output / "environment.json").relative_to(ROOT).as_posix(),
    ]
    sources = source_identity(ROOT, paths=paths)
    inputs = {"simulation_design": design}
    jobs = calibration_job_ids(design)
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "statistical-calibration-" + output.name,
        "data_identity_version": "exact/v2",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L01", "L02", "L03"],
        "source_manifest_sha256": _digest(sources),
        "config_sha256": sha256_file(output / "design.json"),
        "data_manifest_sha256": _digest(data_manifest(inputs)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": jobs,
        "benchmark_ids": ["fixed_zero_cash_reference"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": design["primary_metric"],
        "economic_or_precision_target": {"rationale": design["precision_target"]},
        "training_selection_rule": "No tuning; report every fixed simulation cell, paired block sensitivities and failures.",
        "evaluation_windows": [
            {
                "id": "simulation",
                "role": "engineering_calibration",
                "start": "2000-01-01",
                "end": "2001-01-01",
            }
        ],
        "cost_policy_id": "simulated_net_returns_no_execution_claim/v1",
        "risk_policy_id": "synthetic_calibration_only/v1",
        "resampling": {
            "method": design["bootstrap_method"],
            "block_lengths": design["block_lengths"],
            "iterations": design["bootstrap_iterations"],
            "seed": design["seed"],
        },
        "attempt_budget": len(jobs) + 1,
        "resource_budget": {"wall_time_seconds": 900, "memory_mb": 512, "max_workers": 1},
        "stop_rules": [
            "Retain all failures and do not expand the grid after viewing calibration results.",
            "Stop on source or design drift; do not access empirical final windows.",
        ],
        "final_access_policy_id": None,
        "calibration_design_sha256": _digest(design),
    }
    scope = SearchScope.register(
        protocol=protocol,
        data_map=inputs,
        output=output / "evidence",
        source_root=ROOT,
        source_paths=paths,
    )
    _write(output / "protocol.json", protocol)
    _write(output / "source_manifest.json", sources)
    snapshot = snapshot_sources(ROOT, output / "source", sources)
    _write(output / "source_snapshot.json", snapshot)
    scope.record(candidate="fixed_zero_cash_reference", status="started")
    scope.record(
        candidate="fixed_zero_cash_reference",
        status="completed",
        result={"economic_return": 0, "basis": "fixed simulation null comparator"},
    )
    try:
        result = run_calibration(design, scope=scope)
    except BaseException as exc:
        _write(
            output / "failure.json",
            {
                "engineering": "fail",
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "search_scope_audit": scope.audit(),
                "research": "not_evaluated",
                "admission_eligible": False,
            },
        )
        raise
    audit = scope.audit()
    result["search_scope_audit"] = audit
    if audit["status"] != "ok" or not audit["results_complete"]:
        result["engineering"] = "fail"
    result["source_manifest_sha256"] = _digest(sources)
    result["source_snapshot"] = snapshot
    _write(output / "calibration.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--design", type=Path)
    args = parser.parse_args(argv)
    design = json.loads(args.design.read_text(encoding="utf-8")) if args.design else None
    result = calibrate(args.output, design=design)
    print(
        json.dumps(
            {
                "engineering": result["engineering"],
                "jobs": result["jobs_planned"],
                "output": str(args.output),
            }
        )
    )
    return 0 if result["engineering"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
