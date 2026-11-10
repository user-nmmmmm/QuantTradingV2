"""SR-ALPHA-004: marginal value of the OBV, stability and router-cooldown entry gates.

Protocol: docs/research/entry_delay_study_20261110.md. One dimension changes
per arm; every arm runs train60 and validation20; final20 is refused. The
study selects at most one dimension for stage C1 and changes no config.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Mapping

import pandas as pd

from analysis.overlay_study import run_diagnostics
from config.config import config
from core.foundation.reproducibility import sha256_file, sha256_frame
from scripts.run_revalidation60 import load_inputs, run_one, save, source_hashes

ROOT = Path(__file__).resolve().parents[1]

EXPERIMENT_ID = "SR-ALPHA-004"
PROTOCOL = ROOT / "docs/research/entry_delay_study_20261110.md"
ARMS: Dict[str, Dict[str, Any]] = {
    "b0_baseline": {},
    "obv_off": {"research": {"strategy_ablation": "no_obv_confirmation"}},
    "stab2": {"state": {"stability_period": 2}},
    "stab3": {"state": {"stability_period": 3}},
    "stab10": {"state": {"stability_period": 10}},
    "cool0": {"router": {"cooldown_bars": 0}},
    "cool1": {"router": {"cooldown_bars": 1}},
}
DIMENSIONS = {"obv_off": "obv", "stab2": "stability", "stab3": "stability", "stab10": "stability",
              "cool0": "cooldown", "cool1": "cooldown"}
CONTROL_ONLY = {"stab10"}
BASELINE = "b0_baseline"
WINDOWS = ("train60", "validation20")
FORBIDDEN_WINDOW = "final20"


def arm_parameters(base: Mapping[str, Any], arm: str) -> Dict[str, Any]:
    if arm not in ARMS:
        raise ValueError("Unknown registered study arm")
    params = deepcopy(dict(base))
    for section, values in ARMS[arm].items():
        params.setdefault(section, {}).update(values)
    params.setdefault("research", {}).update(
        entry_audit=True, experiment_id=f"{EXPERIMENT_ID}:{arm}")
    return params


def ratio(row: Mapping[str, Any]) -> float:
    drawdown = float(row["max_drawdown_pct"])
    return float(row["return_pct"]) / drawdown if drawdown > 0 else float("inf")


def passes(row: Mapping[str, Any], base: Mapping[str, Any]) -> Dict[str, bool]:
    cap = max(1.25 * float(base["max_drawdown_pct"]), float(base["max_drawdown_pct"]) + 3.0)
    return {"accounting_ok": bool(row["accounting_ok"]),
            "drawdown_within_cap": float(row["max_drawdown_pct"]) <= cap,
            "top5_not_worse": float(row["net_closed_pnl_without_top_5"]) >= float(base["net_closed_pnl_without_top_5"]),
            "ratio_beats_baseline": ratio(row) > ratio(base)}


def decide(rows: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    """Protocol section 5. ``rows[window][arm]`` holds that run's diagnostics."""
    verdicts = {}
    for arm in ARMS:
        if arm == BASELINE:
            continue
        checks = {w: passes(rows[w][arm], rows[w][BASELINE]) for w in WINDOWS}
        verdicts[arm] = {"dimension": DIMENSIONS[arm], "checks": checks,
                         "passed": all(all(c.values()) for c in checks.values()),
                         "validation_ratio": ratio(rows["validation20"][arm])}
    base_ratio = ratio(rows["validation20"][BASELINE])
    candidates = {}
    for arm, verdict in verdicts.items():
        if not verdict["passed"] or arm in CONTROL_ONLY:
            continue
        best = candidates.get(verdict["dimension"])
        if best is None or verdict["validation_ratio"] > verdicts[best]["validation_ratio"]:
            candidates[verdict["dimension"]] = arm
    chosen = max(candidates.values(), key=lambda a: verdicts[a]["validation_ratio"] - base_ratio,
                 default=None)
    return {"verdicts": verdicts, "baseline_validation_ratio": base_ratio,
            "dimension_candidates": candidates, "stage_c1_arm": chosen,
            "stage_c1_dimension": DIMENSIONS[chosen] if chosen else None,
            "control_passed": {arm: verdicts[arm]["passed"] for arm in CONTROL_ONLY},
            "conclusion": "stage_C1_candidate" if chosen else "retain_current_entry_gates"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/entry_delay_study_20261110")
    parser.add_argument("--review-root", type=Path, default=ROOT / "reports/strategy_review_20260919",
                        help="folder holding frozen_inputs/ and reference_protocol.json")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if FORBIDDEN_WINDOW in WINDOWS:
        raise ValueError("final holdout must not be used")
    inputs = args.review_root / "frozen_inputs"
    reference = json.loads((args.review_root / "reference_protocol.json").read_text(encoding="utf-8"))
    frames, _, _ = load_inputs(inputs)
    base = deepcopy(config._config)
    identity = {"schema": "entry-delay-study/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "data_manifest_sha256": sha256_file(inputs / "data_manifest.json"),
                "engine_input_hashes": {s: sha256_frame(f) for s, f in frames.items()},
                "arms": ARMS, "control_only": sorted(CONTROL_ONLY),
                "windows": {w: reference["segments"][w] for w in WINDOWS},
                "forbidden_window": FORBIDDEN_WINDOW, "forced_end_liquidation": True}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/data identity changed; use a new study output directory")
    else:
        save(manifest, identity)
    if (args.output / "result.json").exists():
        raise ValueError("The registered comparison is single-use and already recorded")

    rows: Dict[str, Dict[str, Dict[str, Any]]] = {w: {} for w in WINDOWS}
    for window in WINDOWS:
        start, end = reference["segments"][window]
        for arm in ARMS:
            name = f"{arm}_{window}"
            run_one(name, args.output, frames, {**reference, "parameters": arm_parameters(base, arm)},
                    start=start, end=end, forced=True)
            folder = args.output / "runs" / name
            row = {k: v for k, v in run_diagnostics(folder).items() if not isinstance(v, (dict, list))}
            row["closed_trades"] = len(pd.read_csv(folder / "closed_trades.csv"))
            rows[window][arm] = row
            print(f"DONE {name}: return={row['return_pct']:.2f}% dd={row['max_drawdown_pct']:.2f}% "
                  f"trades={row['closed_trades']}", flush=True)
    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    table = pd.DataFrame([{"window": w, "arm": a, "ratio": ratio(r), **r}
                          for w in WINDOWS for a, r in rows[w].items()])
    table.to_csv(args.output / "comparison.csv", index=False)
    print(table[["window", "arm", "return_pct", "max_drawdown_pct", "ratio",
                 "net_closed_pnl_without_top_5", "closed_trades", "mean_exposure"]].to_string(index=False))
    result = decide(rows)
    save(args.output / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "verdicts"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
