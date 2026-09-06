"""P0 fixed four-arm study, with passive attribution and deterministic replay."""
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
from scripts.run_health_recovery_experiment import CANDIDATE

STAGED = {**CANDIDATE, "recovery_stages": [0.1, 0.25, 0.5, 1.0],
          "recovery_stage_min_days": 30.0}
ARMS = {
    "baseline": ({}, {}),
    "baseline_no_audit": ({}, {"entry_audit": False}),
    "abrupt": (CANDIDATE, {}),
    "staged": (STAGED, {}),
    "staged_derisk": (STAGED, {"block_remaining_fraction": 0.5}),
    "staged_derisk_replay": (STAGED, {"block_remaining_fraction": 0.5}),
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
    changed_sources = ["core/entry_audit.py", "core/runtime.py", "core/allocation.py",
        "core/risk/entry_policy.py", "core/risk/position_sizing.py", "core/strategy_health.py",
        "strategies/base.py", "strategies/trend_breakout.py", "router/router.py", "backtest/engine.py",
        "scripts/run_expanded_universe_backtest.py", "scripts/run_p0_attribution_experiment.py"]
    save(output / "protocol.json", {
        "id": "P0-ATTRIBUTION-RECOVERY-001", "arms": ARMS, "code": code_identity(ROOT),
        "source_manifest_sha256": sha256_file(source / "download_manifest.json"),
        "code_hashes": {p: sha256_file(ROOT / p) for p in changed_sources},
        "production_config_sha256": sha256_file(ROOT / "config/params.yaml"),
        "acceptance": ["passive audit reproduces baseline", "all opening fills linked exactly once",
            "one primary outcome per observed bar", "manual locks and liquidation remain terminal",
            "accounting identity", "joint candidate replay identical"],
        "research_decision": "Compare all fixed arms; do not select new parameters from results; no production promotion or independent holdout claim.",
        "derisk_execution": "Research only: once per BLOCK_NEW epoch keep 50% of positions using existing close-based emergency matcher (including synthetic volume assumption); superseded stops are cancelled/rearmed by existing sync. No high-water reset.",
        "audit_cost_fix": "Correct forced-action audit cost from per-unit slip to slip times qty; does not change trades, cash or equity.",
    })
    for entry in inventory["symbols"].values():
        path = source / "data_inputs" / entry["file"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Input mismatch: {path.name}")
        shutil.copy2(path, output / "data_inputs" / path.name)
    save(output / "download_manifest.json", inventory)
    original = deepcopy(config._config)
    comparisons = {}
    logging.disable(logging.CRITICAL)
    try:
        for name, (health, research) in ARMS.items():
            config._config = deepcopy(original)
            config._config["strategy_health"].update(health)
            config._config["research"] = {"entry_audit": True, **research}
            comparisons[name] = run_arm(name, reference["symbols"], output, inventory,
                reference["initial_capital"], reference["requested_start"], reference["requested_end"])
            save(output / "comparison.json", comparisons)
        digest = lambda p: json.loads((p / "result_digest.json").read_text(encoding="utf-8"))
        checks = {
            "passive_baseline_unchanged": digest(output / "baseline") == digest(output / "baseline_no_audit"),
            "prior_economic_results_unchanged": all(digest(output / "baseline")[k] == digest(source / "expanded")[k] for k in ("trades", "equity", "benchmark")),
            "joint_replay_identical": digest(output / "staged_derisk") == digest(output / "staged_derisk_replay"),
            "audit_replay_identical": sha256_file(output / "staged_derisk/entry_observations.csv") == sha256_file(output / "staged_derisk_replay/entry_observations.csv"),
        }
        save(output / "verification.json", checks)
        if not all(checks.values()):
            raise ValueError(f"P0 verification failed: {checks}")
    finally:
        config._config = original


if __name__ == "__main__":
    main()
