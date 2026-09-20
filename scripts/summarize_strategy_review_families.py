"""Describe registered family comparisons against the same-window baseline.

This command only reads published research tables and writes new aggregate
reports. It never runs an engine, selects a winner, or grants admission.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FAMILY_COUNTS = {"timing": 8, "ablation": 3, "stops": 36, "scoring": 3, "donchian": 5}
VARYING_PATHS = {
    "timing": {"state.stability_period", "router.cooldown_bars"},
    "ablation": {"research.strategy_ablation", "strategy_health.enabled", "routing.TREND_UP", "routing.TREND_DOWN", "routing.SIDEWAYS", "routing.VOLATILE"},
    "stops": {"stops.initial_stop_mode", "stops.initial_atr_multiple", "stops.use_trailing_stop", "stops.trailing_atr_multiple"},
    "scoring": {"candidate_scoring.enabled", "candidate_scoring.weights.breakout_extent", "candidate_scoring.weights.trend_strength", "candidate_scoring.weights.volume_confirmation", "candidate_scoring.weights.liquidity"},
    "donchian": {"research.trend_breakout_parameters.entry_window", "research.trend_breakout_parameters.exit_window"},
}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def flatten(value, prefix=""):
    result = {}
    if isinstance(value, dict) and value:
        for key, child in value.items():
            result.update(flatten(child, f"{prefix}.{key}" if prefix else key))
    else:
        result[prefix] = value
    return result


def parameter_differences(reference, actual):
    baseline, arm = flatten(reference), flatten(actual)
    return [{"path": path, "baseline_present": path in baseline, "baseline_value": baseline.get(path),
             "arm_present": path in arm, "arm_value": arm.get(path)}
            for path in sorted(baseline.keys() | arm.keys())
            if path not in baseline or path not in arm or baseline[path] != arm[path]]


def effective_parameters(family, parameters):
    if family == "timing":
        return {"stability_period": parameters["state"]["stability_period"],
                "cooldown_bars": parameters["router"]["cooldown_bars"],
                "zero_cooldown_semantics": "no_subsequent_cooldown_switch_bar_still_skipped"}
    if family == "ablation":
        ablation = parameters.get("research", {}).get("strategy_ablation")
        return {"strategy_ablation": ablation,
                "obv_confirmation_enabled": ablation != "no_obv_confirmation",
                "regime_restrictions_enabled": ablation != "no_regime_restrictions",
                "health_enabled": parameters["strategy_health"]["enabled"],
                "routing": parameters["routing"]}
    if family == "stops":
        stops = parameters["stops"]
        return {"resolved_initial_stop_mode": stops.get("initial_stop_mode") or
                    ("hybrid" if stops.get("use_atr_initial_stop") else "structural_donchian"),
                "initial_atr_multiple": stops["initial_atr_multiple"],
                "initial_atr_multiple_active": (stops.get("initial_stop_mode") or
                    ("hybrid" if stops.get("use_atr_initial_stop") else "structural_donchian")) != "structural_donchian",
                "use_trailing_stop": stops["use_trailing_stop"],
                "trailing_atr_multiple": stops["trailing_atr_multiple"],
                "donchian_close_and_regime_exits_retained": True}
    if family == "scoring":
        return deepcopy(parameters["candidate_scoring"])
    return {"entry_window": parameters.get("research", {}).get("trend_breakout_parameters", {}).get("entry_window", 20),
            "exit_window": parameters.get("research", {}).get("trend_breakout_parameters", {}).get("exit_window", 10)}


def numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def clean_record(row):
    return {key: None if pd.isna(value) else value for key, value in row.items()}


def descriptive(values):
    values = pd.Series([number for value in values if (number := numeric(value)) is not None], dtype=float)
    return {"count": len(values), "minimum": float(values.min()) if len(values) else None,
            "median": float(values.median()) if len(values) else None,
            "maximum": float(values.max()) if len(values) else None}


def load_table(path):
    if not path.exists():
        return pd.DataFrame(), None
    raw = path.read_bytes()
    try:
        return pd.read_csv(io.BytesIO(raw)), digest(raw)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), digest(raw)


def assess(protocol, matrix_summary, all_runs):
    arms, windows = protocol["arms"], protocol["rolling_windows"]
    if len(arms) != 52 or len(windows) != 11:
        raise ValueError("Registered study must contain exactly 52 unique arms and 11 windows")
    arm_map = {arm["arm"]: arm for arm in arms}
    window_map = {window["name"]: window for window in windows}
    if len(arm_map) != 52 or len(window_map) != 11 or "baseline" not in arm_map:
        raise ValueError("Duplicate registration or missing shared baseline")
    reference = arm_map["baseline"]["parameters"]
    if reference != protocol["parameters"]:
        raise ValueError("Shared baseline differs from frozen default parameters")
    families = {family: [arm for arm in arms if family in arm["families"]] for family in FAMILY_COUNTS}
    if {family: len(members) for family, members in families.items()} != FAMILY_COUNTS:
        raise ValueError("Family counts must be timing8/ablation3/stops36/scoring3/donchian5")
    differences = {name: parameter_differences(reference, arm["parameters"]) for name, arm in arm_map.items()}
    for family, members in families.items():
        for arm in members:
            unexpected = {row["path"] for row in differences[arm["arm"]]} - VARYING_PATHS[family]
            if unexpected:
                raise ValueError(f"{arm['arm']} changes parameters outside {family}: {sorted(unexpected)}")
    if all_runs.empty:
        matrix = pd.DataFrame()
    elif "phase" not in all_runs:
        raise ValueError("all_run_summaries lacks explicit phase")
    else:
        matrix = all_runs.loc[all_runs.phase.eq("matrix")].copy()
    expected_names = {f"{name}__{window}" for name in arm_map for window in window_map}
    observed = {}
    if len(matrix):
        if "name" not in matrix or matrix.name.duplicated().any():
            raise ValueError("Duplicate or missing matrix run identity")
        if not set(matrix.name).issubset(expected_names):
            raise ValueError("Unregistered matrix result")
        for row in matrix.to_dict("records"):
            row = clean_record(row)
            name, window_name = row["name"].rsplit("__", 1)
            window = window_map[window_name]
            if any(pd.to_datetime(row.get(key), utc=True) != pd.to_datetime(window[key], utc=True) for key in ("start", "end")):
                raise ValueError(f"Result period does not match its registered window: {row['name']}")
            if numeric(row.get("return_pct")) is None or numeric(row.get("max_drawdown_pct")) is None or row["max_drawdown_pct"] < 0:
                raise ValueError(f"Unusable economic metrics: {row['name']}")
            observed[(name, window_name)] = row
    summaries = {}
    if len(matrix_summary):
        if "arm" not in matrix_summary or matrix_summary.arm.duplicated().any() or not set(matrix_summary.arm).issubset(arm_map):
            raise ValueError("Duplicate or unregistered matrix summary arm")
        summaries = {row["arm"]: clean_record(row) for row in matrix_summary.to_dict("records")}
        for name, summary in summaries.items():
            count = sum(key[0] == name for key in observed)
            if summary.get("windows") != count:
                raise ValueError(f"Published window counts disagree for {name}; regenerate both source tables")
    minimum_support = protocol["gates"]["minimum_cohorts"]
    paired = []
    for arm in arms:
        for window_name in window_map:
            current, baseline = observed.get((arm["arm"], window_name)), observed.get(("baseline", window_name))
            ready = current is not None and baseline is not None
            if ready and current.get("initial_capital") != baseline.get("initial_capital"):
                raise ValueError("Paired arms have different initial capital")
            row = {"arm": arm["arm"], "window": window_name,
                   "families": ",".join(family for family in FAMILY_COUNTS if family in arm["families"]),
                   "source_run": f"{arm['arm']}__{window_name}", "baseline_run": f"baseline__{window_name}",
                   "comparison_status": "complete" if ready else "pending",
                   "missing_evidence": ",".join(name for name, value in (("arm", current), ("baseline", baseline)) if value is None),
                   "return_delta_percentage_points": current["return_pct"] - baseline["return_pct"] if ready else None,
                   "drawdown_delta_percentage_points": current["max_drawdown_pct"] - baseline["max_drawdown_pct"] if ready else None}
            for prefix, item in (("arm", current), ("baseline", baseline)):
                for key in ("return_pct", "max_drawdown_pct", "cohort_count", "cohort_sample", "statistical_gate_status", "statistical_gate_status_raw", "cohort_admission_status", "cost_scope", "financing_attribution_status", "account_return_cost_scope", "cohort_pnl_cost_scope", "identity_sha256"):
                    row[f"{prefix}_{key}"] = item.get(key) if item else None
                count = numeric(item.get("cohort_count")) if item else None
                row[f"{prefix}_count_support_status"] = "pending" if item is None else "insufficient" if count is None or count < minimum_support else "sufficient"
            paired.append(row)
    arm_details = []
    for family, members in families.items():
        for arm in members:
            own = [row for row in paired if row["arm"] == arm["arm"]]
            available = [row for row in own if row["comparison_status"] == "complete"]
            completed = sum((arm["arm"], window) in observed for window in window_map)
            summary = summaries.get(arm["arm"], {})
            if completed == 11 and not summary:
                raise ValueError(f"Complete arm lacks published matrix summary: {arm['arm']}")
            arm_details.append({"family": family, "arm": arm["arm"],
                "shared_baseline": arm["arm"] == "baseline", "registered_windows": 11,
                "completed_windows": completed, "paired_windows": len(available),
                "pipeline_status": "complete" if len(available) == 11 else "pending",
                "research_status": "insufficient" if len(available) == 11 else "pending",
                "closure_status": "证据不足", "rolling_gate_observed": summary.get("rolling_gate", "pending"),
                "positive_windows": summary.get("positive_windows"), "median_return_pct": summary.get("median_return_pct"),
                "count_supported_windows": sum(row["arm_count_support_status"] == "sufficient" for row in own),
                "statistical_windows_pass": sum(row["arm_statistical_gate_status"] == "pass" for row in own),
                "statistical_windows_fail": sum(row["arm_statistical_gate_status"] == "fail" for row in own),
                "statistical_windows_insufficient": sum(row["arm_statistical_gate_status"] == "insufficient" for row in own),
                "return_delta_distribution": descriptive(row["return_delta_percentage_points"] for row in available),
                "drawdown_delta_distribution": descriptive(row["drawdown_delta_percentage_points"] for row in available),
                "effective_family_parameters": effective_parameters(family, arm["parameters"]),
                "only_family_changes_verified": True, "parameter_differences_from_baseline": differences[arm["arm"]],
                "nondefault_cost_and_final20_validated": False if arm["arm"] != "baseline" else None,
                "future_evidence": "pending_unseen_evidence"})
    assessments = []
    baseline_flat = flatten(reference)
    for family, members in families.items():
        details = [row for row in arm_details if row["family"] == family]
        complete = all(row["pipeline_status"] == "complete" for row in details)
        varying = sorted({change["path"] for arm in members for change in differences[arm["arm"]]})
        fixed = {key: value for key, value in baseline_flat.items() if key not in varying}
        assessments.append({"family": family, "registered_configurations": FAMILY_COUNTS[family],
            "registered_windows_per_configuration": 11, "comparison_rows": FAMILY_COUNTS[family] * 11,
            "complete_configurations": sum(row["completed_windows"] == 11 for row in details),
            "completed_run_references": sum(row["completed_windows"] for row in details),
            "paired_window_comparisons": sum(row["paired_windows"] for row in details),
            "baseline_included_as_shared_reference": any(arm["arm"] == "baseline" for arm in members),
            "pipeline_status": "complete" if complete else "pending",
            "research_status": "insufficient" if complete else "pending", "closure_status": "证据不足",
            "rolling_pass_configurations": sum(row["rolling_gate_observed"] == "pass" for row in details),
            "rolling_fail_configurations": sum(row["rolling_gate_observed"] == "fail" for row in details),
            "rolling_pending_configurations": sum(row["rolling_gate_observed"] not in {"pass", "fail"} for row in details),
            "count_supported_window_references": sum(row["count_supported_windows"] for row in details),
            "statistical_pass_window_references": sum(row["statistical_windows_pass"] for row in details),
            "registered_variable_paths": varying, "fixed_parameters": fixed,
            "fixed_parameters_sha256": digest(json.dumps(fixed, sort_keys=True, ensure_ascii=False).encode()),
            "arms": details,
            "interpretation": "Descriptive registered neighborhood; rolling pass is a local fact, not selection or full admission"})
    return {"schema": "strategy_review_family_assessment/v1", "registered_unique_arms": 52,
            "registered_unique_engine_runs": 572, "completed_unique_engine_runs": len(observed),
            "unique_paired_window_comparisons": sum(row["comparison_status"] == "complete" for row in paired),
            "family_arm_references": len(arm_details), "family_window_references": 55 * 11,
            "shared_baseline_accounting": "One baseline arm, 11 actual runs, reused by four registered families; baseline also comparator for ablations; no repeat engine runs",
            "pipeline_status": "complete" if len(observed) == 572 else "pending",
            "research_status": "insufficient" if len(observed) == 572 else "pending",
            "closure_status": "证据不足", "selected_candidate": None, "added_acceptance_thresholds": False,
            "comparison_units": {"return_delta": "arm_minus_same_window_baseline_percentage_points; positive_is_higher_return",
                                 "drawdown_delta": "arm_minus_same_window_baseline_percentage_points; negative_is_lower_drawdown",
                                 "count_support": "registered_30_cohort_count_only; not_cost_attribution_or_statistical_admission"},
            "limitations": ["Overlapping windows are not independent samples", "Only within-family parameters change",
                "Nondefault arms have no registered full-period cost or final20 validation", "Prospective observations remain pending",
                "Account net return and cohort statistics retain their separately published financing cost scopes"],
            "families": assessments}, paired, arm_details


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(batch):
    protocol_path = batch / "review_protocol.json"
    raw_protocol = protocol_path.read_bytes()
    protocol = json.loads(raw_protocol)
    matrix, matrix_hash = load_table(batch / "matrix_summary.csv")
    all_runs, all_hash = load_table(batch / "all_run_summaries.csv")
    report, paired, arms = assess(protocol, matrix, all_runs)
    report.update(generated_at=datetime.now(timezone.utc).isoformat(),
        source_hashes={"review_protocol.json": digest(raw_protocol), "matrix_summary.csv": matrix_hash,
                       "all_run_summaries.csv": all_hash}, script_sha256=digest(Path(__file__).read_bytes()))
    for name, expected in report["source_hashes"].items():
        if expected is not None and digest((batch / name).read_bytes()) != expected:
            raise ValueError("Published source tables changed during family postprocessing")
    save_json(batch / "family_assessment.json", report)
    pd.DataFrame([{key: value for key, value in row.items() if key not in {"arms", "fixed_parameters", "registered_variable_paths"}}
                  for row in report["families"]]).to_csv(batch / "family_assessment.csv", index=False)
    pd.DataFrame(paired).to_csv(batch / "family_paired_window_differences.csv", index=False)
    pd.DataFrame([{key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
                  for key, value in row.items()} for row in arms]).to_csv(batch / "family_arm_assessment.csv", index=False)
    print(json.dumps({"status": report["pipeline_status"], "unique_runs": report["completed_unique_engine_runs"],
                      "families": [{key: row[key] for key in ("family", "complete_configurations", "rolling_pass_configurations", "rolling_fail_configurations", "research_status")} for row in report["families"]]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    run(parser.parse_args().batch.resolve())
