"""SR5-SUCC-001: parent (OBV on) vs successor (C1) backtest comparison and single final20 evaluation.

Protocol: docs/research/successor_comparison_20261110.md. final20 runs once:
after result.json exists the script refuses to run again. The verdict can
only reject the successor or leave it "not rejected (retrospective)"; it
never admits anything and changes no configuration.
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

EXPERIMENT_ID = "SR5-SUCC-001"
PROTOCOL = ROOT / "docs/research/successor_comparison_20261110.md"
ARMS = {"parent": {"trend_breakout": {"use_obv": True}}, "successor": {}}
WINDOWS = ("train60", "validation20", "final20")
JUDGED_WINDOW = "final20"
COST_MULTIPLIERS = (1, 2)


def arm_parameters(base: Mapping[str, Any], arm: str) -> Dict[str, Any]:
    if arm not in ARMS:
        raise ValueError("Unknown registered comparison arm")
    params = deepcopy(dict(base))
    for section, values in ARMS[arm].items():
        params.setdefault(section, {}).update(values)
    params.setdefault("research", {}).update(entry_audit=True, experiment_id=f"{EXPERIMENT_ID}:{arm}")
    return params


def ratio(row: Mapping[str, Any]) -> float:
    drawdown = float(row["max_drawdown_pct"])
    return float(row["return_pct"]) / drawdown if drawdown > 0 else float("inf")


def judge(rows: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Protocol section 5 on final20; ``rows[f"{arm}@{cost}"]`` are that window's diagnostics."""
    checks = {}
    for cost in COST_MULTIPLIERS:
        parent, successor = rows[f"parent@{cost}"], rows[f"successor@{cost}"]
        cap = max(1.25 * float(parent["max_drawdown_pct"]), float(parent["max_drawdown_pct"]) + 3.0)
        checks[str(cost)] = {
            "accounting_ok": bool(successor["accounting_ok"]) and bool(parent["accounting_ok"]),
            "drawdown_within_cap": float(successor["max_drawdown_pct"]) <= cap,
            "ratio_not_worse": ratio(successor) >= ratio(parent),
            "top5_not_worse": float(successor["net_closed_pnl_without_top_5"])
                              >= float(parent["net_closed_pnl_without_top_5"]),
        }
    passed = all(all(c.values()) for c in checks.values())
    return {"window": JUDGED_WINDOW, "checks": checks,
            "conclusion": "not_rejected_retrospective" if passed else "reject_successor",
            "admission": "paused_revalidation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/successor_comparison_20261110")
    parser.add_argument("--review-root", type=Path, default=ROOT / "reports/strategy_review_20260919",
                        help="folder holding frozen_inputs/ and reference_protocol.json")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    inputs = args.review_root / "frozen_inputs"
    reference = json.loads((args.review_root / "reference_protocol.json").read_text(encoding="utf-8"))
    frames, _, _ = load_inputs(inputs)
    base = deepcopy(config._config)
    identity = {"schema": "successor-comparison/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "data_manifest_sha256": sha256_file(inputs / "data_manifest.json"),
                "engine_input_hashes": {s: sha256_frame(f) for s, f in frames.items()},
                "arms": ARMS, "windows": {w: reference["segments"][w] for w in WINDOWS},
                "judged_window": JUDGED_WINDOW, "cost_multipliers": list(COST_MULTIPLIERS),
                "forced_end_liquidation": True}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/data identity changed; use a new study output directory")
    else:
        save(manifest, identity)
    if (args.output / "result.json").exists():
        raise ValueError("final20 is single-use and its evaluation is already recorded")

    table, judged = [], {}
    for window in WINDOWS:
        start, end = reference["segments"][window]
        for cost in COST_MULTIPLIERS:
            for arm in ARMS:
                name = f"{arm}_{window}_cost{cost}"
                run_one(name, args.output, frames, {**reference, "parameters": arm_parameters(base, arm)},
                        start=start, end=end, multiplier=cost, forced=True)
                folder = args.output / "runs" / name
                row = {k: v for k, v in run_diagnostics(folder).items() if not isinstance(v, (dict, list))}
                row["closed_trades"] = len(pd.read_csv(folder / "closed_trades.csv"))
                table.append({"window": window, "cost": cost, "arm": arm, "ratio": ratio(row), **row})
                if window == JUDGED_WINDOW:
                    judged[f"{arm}@{cost}"] = row
    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    frame = pd.DataFrame(table)
    frame.to_csv(args.output / "comparison.csv", index=False)
    print(frame[["window", "cost", "arm", "return_pct", "max_drawdown_pct", "ratio",
                 "net_closed_pnl_without_top_5", "closed_trades", "mean_exposure"]].to_string(index=False))
    result = judge(judged)
    save(args.output / "result.json", result)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
