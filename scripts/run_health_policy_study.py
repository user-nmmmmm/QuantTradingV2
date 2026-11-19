"""SR1-HEALTH-006: pre-registered health-policy study. Never changes live admission.

Protocol: docs/research/health_policy_study_20261110.md. Selection uses train60
only; validation20 confirms the single selected arm once; final20 is refused.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
from pathlib import Path

import pandas as pd

from analysis.overlay_study import run_diagnostics
from composition.factory import build_strategy_registry
from config.config import config
from core.foundation.reproducibility import sha256_file, sha256_frame
from ops.strategy_registration import register_health_policies
from scripts.run_revalidation60 import load_inputs, run_one, save, source_hashes

ROOT = Path(__file__).resolve().parents[1]

EXPERIMENT_ID = "SR1-HEALTH-006"
PROTOCOL = ROOT / "docs/research/health_policy_study_20261110.md"
ARMS = {
    "h0_baseline": {},
    "h1_streak4": {"consecutive_negative_cohorts": 4},
    "h2_streak5": {"consecutive_negative_cohorts": 5},
    "h3_no_without_best": {"probation_require_positive_without_best": False},
    "h4_streak5_no_without_best": {"consecutive_negative_cohorts": 5,
                                   "probation_require_positive_without_best": False},
    "href_health_off": {"enabled": False},
}
REFERENCE_ONLY = {"href_health_off"}
BASELINE = "h0_baseline"
SELECTION_WINDOW, CONFIRMATION_WINDOW, FORBIDDEN_WINDOW = "train60", "validation20", "final20"


def arm_parameters(base, arm):
    if arm not in ARMS:
        raise ValueError("Unknown registered study arm")
    params = deepcopy(base)
    params.setdefault("research", {}).update(
        entry_audit=True, experiment_id=f"{EXPERIMENT_ID}:{arm}", overlay_study=EXPERIMENT_ID)
    if ARMS[arm]:
        params["research"]["strategy_health_overrides"] = {"TrendBreakout": dict(ARMS[arm])}
    return params


def ratio(row):
    drawdown = float(row["max_drawdown_pct"])
    return float(row["return_pct"]) / drawdown if drawdown > 0 else float("inf")


def annual_win_share(folder, baseline_folder):
    arm = pd.read_csv(folder / "annual_results.csv").set_index("year").return_pct
    base = pd.read_csv(baseline_folder / "annual_results.csv").set_index("year").return_pct
    years = arm.index.intersection(base.index)
    return float((arm.loc[years] > base.loc[years]).mean()) if len(years) else 0.0


def select(rows, folders):
    """Apply the pre-registered selection rule (protocol section 5) to train60 rows."""
    base = rows[BASELINE]
    drawdown_cap = max(1.25 * base["max_drawdown_pct"], base["max_drawdown_pct"] + 3.0)
    verdicts = {}
    for arm, row in rows.items():
        if arm == BASELINE or arm in REFERENCE_ONLY:
            continue
        checks = {
            "accounting_ok": bool(row["accounting_ok"]),
            "drawdown_within_cap": row["max_drawdown_pct"] <= drawdown_cap,
            "top5_not_worse": row["net_closed_pnl_without_top_5"] >= base["net_closed_pnl_without_top_5"],
            "annual_win_share_ge_half": annual_win_share(folders[arm], folders[BASELINE]) >= 0.5,
            "ratio_beats_baseline": ratio(row) > ratio(base),
        }
        verdicts[arm] = {"ratio": ratio(row), "eligible": all(checks.values()), "checks": checks,
                         "annual_win_share": annual_win_share(folders[arm], folders[BASELINE])}
    eligible = [arm for arm, verdict in verdicts.items() if verdict["eligible"]]
    chosen = max(eligible, key=lambda arm: verdicts[arm]["ratio"]) if eligible else None
    return {"baseline_ratio": ratio(base), "drawdown_cap": drawdown_cap,
            "verdicts": verdicts, "selected": chosen,
            "conclusion": "selected_for_confirmation" if chosen else "retain_current_policy"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["select", "confirm"], required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/health_policy_study_20261110")
    parser.add_argument("--inputs", type=Path, default=ROOT / "reports/strategy_review_20260919/frozen_inputs")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    reference = json.loads((ROOT / "reports/strategy_review_20260919/reference_protocol.json").read_text(encoding="utf-8"))
    if FORBIDDEN_WINDOW in {SELECTION_WINDOW, CONFIRMATION_WINDOW}:
        raise ValueError("final holdout must not be used")
    frames, _, _ = load_inputs(args.inputs)
    base = deepcopy(config._config)
    identity = {"schema": "health-policy-study/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "data_manifest_sha256": sha256_file(args.inputs / "data_manifest.json"),
                "engine_input_hashes": {s: sha256_frame(f) for s, f in frames.items()},
                "arms": ARMS, "reference_only": sorted(REFERENCE_ONLY),
                "selection_window": [SELECTION_WINDOW, reference["segments"][SELECTION_WINDOW]],
                "confirmation_window": [CONFIRMATION_WINDOW, reference["segments"][CONFIRMATION_WINDOW]],
                "forbidden_window": FORBIDDEN_WINDOW, "forced_end_liquidation": True}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/data identity changed; use a new study output directory")
    else:
        save(manifest, identity)

    if args.phase == "select":
        window, arms = SELECTION_WINDOW, list(ARMS)
    else:
        selection_path = args.output / "selection.json"
        if not selection_path.exists():
            raise ValueError("Run --phase select first")
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if selection["selected"] is None:
            raise ValueError("No arm was selected; the registered conclusion is retain_current_policy")
        if (args.output / "confirmation.json").exists():
            raise ValueError("Confirmation is single-use and already recorded")
        window, arms = CONFIRMATION_WINDOW, [BASELINE, selection["selected"]]

    start_text, end_text = reference["segments"][window]
    start, end = pd.Timestamp(start_text).tz_localize(None), pd.Timestamp(end_text).tz_localize(None)
    eligible = {s for s, f in frames.items() if not f.loc[start:end].empty}
    rows, folders = {}, {}
    prior = config._config
    for arm in arms:
        params = arm_parameters(base, arm)
        try:
            config._config = params
            registration = register_health_policies(build_strategy_registry(config), eligible,
                                                    params["routing"], research=params["research"])
        finally:
            config._config = prior
        name = f"{arm}_{window}"
        print(f"RUN {name}", flush=True)
        run_one(name, args.output, frames, {**reference, "parameters": params},
                start=start_text, end=end_text, forced=True)
        folder = args.output / "runs" / name
        snapshots = json.loads((folder / "strategy_health.json").read_text(encoding="utf-8"))
        for strategy, registered in registration.items():
            if snapshots[strategy]["policy_registration"] != registered:
                raise ValueError("Runtime health policy/universe differs from pre-registration")
        save(folder / "health_registration.json", registration)
        folders[arm] = folder
        rows[arm] = {k: v for k, v in run_diagnostics(folder).items() if not isinstance(v, (dict, list))}
        print(f"DONE {name}: return={rows[arm]['return_pct']:.2f}% dd={rows[arm]['max_drawdown_pct']:.2f}%", flush=True)

    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    table = pd.DataFrame([{"arm": arm, "window": window, "ratio": ratio(row), **row} for arm, row in rows.items()])
    table.to_csv(args.output / f"comparison_{window}.csv", index=False)
    print(table[["arm", "return_pct", "max_drawdown_pct", "ratio", "net_closed_pnl_without_top_5",
                 "mean_exposure"]].to_string(index=False), flush=True)
    if args.phase == "select":
        result = select(rows, folders)
        save(args.output / "selection.json", result)
    else:
        selected = arms[1]
        base_row, arm_row = rows[BASELINE], rows[selected]
        checks = {"ratio_ge_baseline": ratio(arm_row) >= ratio(base_row),
                  "top5_ge_baseline": arm_row["net_closed_pnl_without_top_5"] >= base_row["net_closed_pnl_without_top_5"]}
        result = {"selected": selected, "checks": checks, "confirmed": all(checks.values()),
                  "effect": "eligible for SR5 out-of-sample process only; no config or governance change"}
        save(args.output / "confirmation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
