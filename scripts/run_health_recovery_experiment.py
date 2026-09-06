"""Offline fixed-candidate comparison; never edits production configuration."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.config import config
from core.reproducibility import code_identity, sha256_file
from scripts.run_expanded_universe_backtest import run_arm, save

CANDIDATE = {
    "repeated_failure_action": "extended_cooldown",
    "extended_cooldown_days": 90.0,
    "recovery_risk_multiplier": 0.10,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    inventory = json.loads((source / "download_manifest.json").read_text(encoding="utf-8"))
    reference = json.loads((source / "expanded/summary.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    (output / "data_inputs").mkdir()
    original = deepcopy(config._config)
    save(output / "experiment.json", {
        "experiment_id": "SR-HEALTH-RECOVERY-001", "candidate": CANDIDATE,
        "source": str(source), "code": code_identity(ROOT),
        "source_manifest_sha256": sha256_file(source / "download_manifest.json"),
        "code_hashes": {path: sha256_file(ROOT / path) for path in (
            "core/strategy_health.py", "scripts/run_health_recovery_experiment.py",
            "scripts/run_expanded_universe_backtest.py", "config/params.yaml")},
        "purpose": "Fixed-candidate engineering comparison; reused history is not independent holdout",
        "criteria": ["default baseline unchanged", "explicit manual locks remain terminal",
                     "accounting reconciles", "candidate deterministic replay",
                     "report returns AND drawdown AND any remaining inactivity"],
    })
    for entry in inventory["symbols"].values():
        path = source / "data_inputs" / entry["file"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Hash mismatch: {entry['file']}")
        shutil.copy2(path, output / "data_inputs" / entry["file"])
    save(output / "download_manifest.json", inventory)
    comparisons = {}
    logging.disable(logging.CRITICAL)
    try:
        for name in ("baseline", "candidate", "candidate_replay"):
            config._config = deepcopy(original)
            if name != "baseline":
                config._config["strategy_health"].update(CANDIDATE)
            comparisons[name] = run_arm(
                name, reference["symbols"], output, inventory,
                reference["initial_capital"], reference["requested_start"], reference["requested_end"],
            )
            save(output / "comparison.json", comparisons)
        digest = lambda folder: json.loads((folder / "result_digest.json").read_text(encoding="utf-8"))
        verification = {
            "baseline_matches_prior": digest(output / "baseline") == digest(source / "expanded"),
            "candidate_replay_matches": digest(output / "candidate") == digest(output / "candidate_replay"),
        }
        save(output / "verification.json", verification)
        if not all(verification.values()):
            raise ValueError(f"Reproducibility failure: {verification}")
    finally:
        config._config = original


if __name__ == "__main__":
    main()
