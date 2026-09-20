"""Postprocessing only: preserve pending, unsupported, failed and passed evidence."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import pandas as pd
from scripts.strategy_review_cost_evidence import (
    cost_adjusted_gates, cost_adjusted_status, financing_evidence,
)


GATE_KEYS = ("cohort_support", "profit_factor", "positive_net_return", "drawdown", "remove_top5_top10")


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def csv(path):
    if not Path(path).exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def combined_status(values):
    """Incomplete evidence cannot become a statistical failure or vacuous pass."""
    values = list(values)
    if not values or any(value in {None, "pending", "missing"} for value in values):
        return "pending"
    if any(value not in {"pass", "fail"} for value in values):
        return "insufficient"
    return "fail" if "fail" in values else "pass"


def closure(status):
    return {"pass": "研究通过", "fail": "研究失败"}.get(status, "证据不足")


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def distribution(values):
    clean = pd.Series([number for value in values if (number := finite(value)) is not None], dtype=float)
    if clean.empty:
        return {"count": 0, "minimum": None, "p05": None, "median": None, "p95": None, "maximum": None}
    return {"count": len(clean), "minimum": float(clean.min()), "p05": float(clean.quantile(.05)),
            "median": float(clean.median()), "p95": float(clean.quantile(.95)), "maximum": float(clean.max())}


def verified_runs(batch, protocol):
    output, identities = [], []
    protocol_hash = hashlib.sha256((batch / "review_protocol.json").read_bytes()).hexdigest()
    allowed = {
        "baseline": {row["name"] for row in protocol["baseline_specs"]},
        "validation": {row["name"] for row in protocol["validation_specs"]},
        "matrix": {f"{arm['arm']}__{window['name']}" for arm in protocol["arms"] for window in protocol["rolling_windows"]},
    }
    for phase in allowed:
        for folder in sorted((batch / f"{phase}_results/runs").glob("*")):
            summary, identity = read(folder / "summary.json"), read(folder / "review_identity.json")
            if summary is None or identity is None:
                continue
            if summary["name"] != folder.name or folder.name not in allowed[phase]:
                raise ValueError(f"Unregistered result: {folder}")
            if identity["identity"]["protocol_sha256"] != protocol_hash:
                raise ValueError(f"Run protocol differs from registered protocol: {folder}")
            if phase != "baseline" and "summary.json" not in identity.get("artifacts", {}):
                raise ValueError(f"Run receipt omits summary: {folder}")
            for filename, expected in identity.get("artifacts", {}).items():
                if hashlib.sha256((folder / filename).read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Run artifact changed: {folder / filename}")
            evidence = read(folder / "review_cohort_evidence.json", {})
            gates = read(folder / "review_research_gates.json", {})
            point = evidence.get("scenarios", {}).get("0", {})
            costs = financing_evidence(folder)
            raw_status = combined_status(gates.get(key) for key in GATE_KEYS)
            row = {key: value for key, value in summary.items() if key != "lifecycle"}
            row.update(phase=phase, identity_sha256=identity["sha256"],
                       artifact_receipt_status="verified" if identity.get("artifacts") else "legacy_source_input_receipt_without_output_hashes",
                       cohort_count=evidence.get("cohort_count"), cohort_pf=point.get("profit_factor"),
                       cohort_pf_unbounded=point.get("profit_factor_unbounded"),
                       pf_lower=(point.get("pf_95pct_ci") or [None])[0], pf_lower_unbounded=point.get("lower_unbounded"),
                       cohort_sample=evidence.get("sample_status", "missing"),
                       statistical_gate_status_raw=raw_status,
                       statistical_gate_status=cost_adjusted_status(raw_status, costs),
                       research_gates_raw=gates, research_gates_true_cost=cost_adjusted_gates(gates, costs),
                       **costs)
            output.append(row)
            identities.append({"phase": phase, "name": folder.name, "sha256": identity["sha256"]})
    return pd.DataFrame(output, columns=None if output else ["name", "phase", "return_pct", "max_drawdown_pct"]), identities


def economic_comparison(reference_folder, other_folder, cutoff=None):
    needed = ("trades.csv", "equity_requested_period.csv")
    if any(not (folder / name).exists() for folder in (reference_folder, other_folder) for name in needed):
        return {"status": "pending", "reason": "required_completed_run_artifacts_missing"}
    outcomes = {}
    for filename, timestamp, columns in (
        ("trades.csv", "fill_time", ["fill_time", "symbol", "side", "qty", "fill_price", "commission", "strategy_id", "exit_reason"]),
        ("equity_requested_period.csv", "timestamp", ["timestamp", "equity"]),
    ):
        frames = []
        for folder in (reference_folder, other_folder):
            frame = csv(folder / filename)
            if frame.empty and filename == "trades.csv":
                frame = pd.DataFrame(columns=columns)
            if not set(columns).issubset(frame):
                return {"status": "insufficient", "reason": f"missing_columns:{filename}"}
            frame[timestamp] = pd.to_datetime(frame[timestamp], utc=True)
            if cutoff is not None:
                frame = frame.loc[frame[timestamp] <= pd.Timestamp(cutoff)]
            if filename == "trades.csv":
                frame = frame.loc[frame.exit_reason.ne("EndOfBacktest")]
            frames.append(frame[columns].sort_values(columns, kind="stable").reset_index(drop=True))
        try:
            pd.testing.assert_frame_equal(*frames, check_dtype=False, atol=1e-8, rtol=1e-10)
            outcomes[filename] = "pass"
        except AssertionError:
            outcomes[filename] = "fail"
    return {"status": combined_status(outcomes.values()), "checks": outcomes,
            "identity_rule": "economic_fields_and_marked_equity; order_identifiers_excluded; tolerance1e-8_abs1e-10_rel"}


def validation_evidence(batch, protocol, completed_names):
    folder = batch / "validation_results/runs"
    by_name = {spec["name"]: spec for spec in protocol["validation_specs"]}
    seeds = protocol["missing_bar_seeds"]
    seed_rows = [read(folder / f"missing_1pct_seed_{seed}/summary.json")
                 for seed in seeds if f"missing_1pct_seed_{seed}" in completed_names]
    seeds_complete = len(seed_rows) == len(seeds)
    comparisons = {}
    for name in ("reversed_symbols", "prefix_2023", "prefix_2024"):
        comparisons[name] = (economic_comparison(folder / "main_1", folder / name,
            pd.to_datetime(by_name[name]["end"], utc=True) if name.startswith("prefix") else None)
            if {"main_1", name}.issubset(completed_names) else {"status": "pending"})
    independent = []
    for name in by_name:
        if name.startswith("fresh_") and name in completed_names:
            row = read(folder / name / "summary.json")
            independent.append({key: row.get(key) for key in ("name", "start", "end", "return_pct", "max_drawdown_pct", "fill_count", "accounting_ok")})
    stress = []
    for name in ("liquidity_quarter", "market_outage_7d", "end_exit_sensitivity", "cost_3"):
        if name in completed_names:
            row = read(folder / name / "summary.json")
            stress.append({key: row.get(key) for key in ("name", "return_pct", "max_drawdown_pct", "fill_count", "accounting_ok", "forced_exit")})
    return {
        "schema": "strategy_review_fixed_validation/v1", "comparisons": comparisons,
        "missing_one_percent": {"status": "complete" if seeds_complete else "pending", "expected_seeds": seeds,
            "completed_seeds": [int(row["name"].rsplit("_", 1)[-1]) for row in seed_rows],
            "return_pct_distribution": distribution(row["return_pct"] for row in seed_rows),
            "max_drawdown_pct_distribution": distribution(row["max_drawdown_pct"] for row in seed_rows),
            "positive_runs": sum(row["return_pct"] > 0 for row in seed_rows),
            "all_observed_accounting_ok": all(row["accounting_ok"] for row in seed_rows) if seed_rows else None},
        "independent_starts": independent, "stress_disclosures": stress,
        "independent_start_interpretation": "Fresh cash and lifecycle state; not expected to equal a sliced continuing account",
        "no_new_profit_threshold_inferred": True,
    }


def attribution_weight_evidence(root, expected_count):
    """Inspect actual emitted weights independently from fitted/fallback labels."""
    path = root / "p2_attribution.csv"
    rows = csv(path)
    counts = Counter()
    for row in rows.to_dict("records"):
        try:
            weights = json.loads(row.get("weights", ""))
            values = list(weights.values()) if isinstance(weights, dict) else []
            valid = (len(values) == 3 and all(isinstance(v, (float, int))
                     and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in values)
                     and math.isclose(sum(values), 1., rel_tol=0., abs_tol=1e-12))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            counts["invalid"] += 1
            continue
        uniform = all(math.isclose(v, 1 / 3, rel_tol=0., abs_tol=1e-12) for v in values)
        counts["uniform" if uniform else "nonuniform"] += 1
        if uniform:
            counts[str(row.get("status", "unknown")) + "_uniform"] += 1
    complete = bool(len(rows)) and len(rows) == expected_count and not counts["invalid"]
    return {"source": "p2_attribution.csv", "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
            "rows": len(rows), "expected_rows": expected_count, "weights_valid_and_complete": complete,
            "uniform_count": counts["uniform"], "nonuniform_count": counts["nonuniform"],
            "invalid_count": counts["invalid"], "fitted_uniform_count": counts["fitted_uniform"],
            "fallback_uniform_count": counts["uniform_fallback_uniform"], "absolute_tolerance": 1e-12,
            "all_actual_weights_uniform": complete and counts["uniform"] == len(rows)}


def meta_evidence(batch):
    root = batch / "meta_review"
    acceptance = read(root / "acceptance.json", {})
    reports = {"p1": read(root / "ev_summary.json", {}), "p2": read(root / "p2_summary.json", {}),
               "p3": read(root / "p3_summary.json", {}), "p3_primary": read(root / "primary/p3_summary.json", {}),
               "p3_fixed_quarter": read(root / "quarter_control/p3_summary.json", {})}
    components = acceptance.get("components", {})
    reasons = []
    actual = acceptance.get("status", "pending")
    for name in ("p0", "p1", "p2", "p3", "p3_primary", "p3_fixed_quarter"):
        if components.get(name, {}).get("status") != "complete" or components.get(name, {}).get("errors"):
            reasons.append(f"{name}_component_missing_or_incomplete")
    if acceptance and not acceptance.get("engineering_isolation"):
        reasons.append("official_account_isolation_failed_or_unknown")
    accounts = acceptance.get("primary_accounts", [])
    lookup = {row.get("arm"): row for row in accounts}
    deltas = {}
    for name, comparator in (("gate", "baseline"), ("sizing", "fixed_quarter")):
        row, control = lookup.get(name, {}), lookup.get(comparator, {})
        if not row.get("metrics_valid") or not control.get("metrics_valid"):
            reasons.append(f"{name}_comparison_metrics_unavailable")
            continue
        if not row.get("comparison_eligible") or not control.get("comparison_eligible"):
            reasons.append(f"{name}_or_control_inactive")
        if min(row.get("finite_closed_candidate_count", 0), control.get("finite_closed_candidate_count", 0)) == 0:
            reasons.append(f"{name}_or_control_has_no_finite_closed_candidate_outcome")
        deltas[name] = {"comparator": comparator,
            "return_percentage_point_delta": 100 * (row["return_fraction"] - control["return_fraction"]),
            "drawdown_percentage_point_delta": 100 * (row["max_drawdown_fraction"] - control["max_drawdown_fraction"]),
            "interpretation": "descriptive_same_initial_capital; no causal or same_risk significance test"}
    p2 = reports["p2"]
    if not p2.get("forecast_count"):
        reasons.append("no_finite_frozen_p2_forecast_with_mature_executable_label")
    if not p2.get("status_counts", {}).get("allow", 0):
        reasons.append("no_p2_allow_prediction")
    if p2.get("regime_model_count") and not p2.get("available_regime_model_count"):
        reasons.append("no_fully_available_three_axis_regime_model")
    if p2.get("attribution_count") and p2.get("attribution_fallback_count") == p2.get("attribution_count"):
        reasons.append("all_attribution_books_use_uniform_fallback")
    weights = attribution_weight_evidence(root, p2.get("attribution_count"))
    if weights["all_actual_weights_uniform"]:
        reasons.append("all_actual_attribution_weights_uniform_including_fitted_books")
    if "sizing" in deltas and abs(deltas["sizing"]["return_percentage_point_delta"]) < 1e-10 and abs(deltas["sizing"]["drawdown_percentage_point_delta"]) < 1e-10:
        reasons.append("sizing_economics_equal_fixed_quarter_control")
    # No new positive-edge acceptance threshold is selected after seeing the replay.
    # Existing source acceptance is engineering completeness, never profitability.
    reasons.append("no_preregistered_inferential_meta_uplift_acceptance_test")
    return {"schema": "strategy_review_meta_assessment/v1", "pipeline_status": actual,
            "research_status": "pending" if not acceptance else "insufficient",
            "observed_limitations": reasons, "components": components,
            "required_replay_checks": acceptance.get("required_replay_checks", {}),
            "primary_selector": acceptance.get("primary_selector"), "primary_accounts": accounts,
            "descriptive_comparisons": deltas,
            "attribution_weight_evidence": weights,
            "p1": {key: reports["p1"].get(key) for key in ("status", "prediction_count", "status_counts", "forecast_count", "eligible_count", "paired_forecast_count")},
            "p2": {key: p2.get(key) for key in ("status", "prediction_count", "status_counts", "forecast_count", "eligible_count", "regime_model_count", "available_regime_model_count", "attribution_count", "attribution_fallback_count", "attribution_reason_counts", "forecast_comparisons")},
            "replay_statuses": {key: report.get("status", "pending") for key, report in reports.items() if key.startswith("p3")},
            "interpretation": "Pipeline completion and descriptive positive returns do not establish incremental predictive or sizing edge"}


def public_evidence(batch):
    source = batch / "public_data_validated/manifest.json"
    manifest = read(source, {})
    stream_rows = []
    for row in manifest.get("streams", []):
        path = source.parent / row["csv_path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["csv_sha256"]:
            raise ValueError(f"Public data artifact changed: {path}")
        stream_rows.append({key: row.get(key) for key in ("venue", "symbol", "timeframe", "status", "rows", "missing_bars", "segments", "gaps", "failures")})
    lifecycle = read(batch / "public_data/lifecycle_sources/manifest.json", {})
    archive = read(batch / "public_data_archive_audit/manifest.json", {})
    return {"schema": "strategy_review_public_evidence/v1", "source": str(source),
            "pipeline_status": "complete" if len(stream_rows) == 24 else "pending",
            "matrix_streams": manifest.get("matrix_streams"), "complete_streams": manifest.get("complete_streams"),
            "rows": manifest.get("rows"), "missing_bars": manifest.get("missing_bars"), "streams": stream_rows,
            "historical_complete_streams": sum(r.get("segments", {}).get("historical", {}).get("observed") == r.get("segments", {}).get("historical", {}).get("expected") and "historical" in r.get("segments", {}) for r in stream_rows),
            "recent_complete_streams": sum(r.get("segments", {}).get("recent_drift", {}).get("observed") == r.get("segments", {}).get("recent_drift", {}).get("expected") and "recent_drift" in r.get("segments", {}) for r in stream_rows),
            "lifecycle": {"status": "complete" if lifecycle else "pending",
                "existing_corroborated": sum(r["status"] == "corroborated" for r in lifecycle.get("existing_events", [])),
                "current_instruments_verified": sum(r["status"] == "current_instrument_verified" for r in lifecycle.get("current_instruments", [])),
                "selected_events": lifecycle.get("selected_events_non_exhaustive", []),
                "historical_complete_registry": False, "selection_bias_resolved": False},
            "binance_archive_audit": archive or {"status": "pending"}}


def cross_evidence(batch):
    raw = read(batch / "cross_market/comparison.json", {})
    pairs = raw.get("paired_comparisons", [])
    runs = raw.get("runs", {})
    expected_pairs = {(period, timeframe) for period in ("historical", "recent_drift")
                      for timeframe in ("1d", "4h")}
    expected_runs = {f"{period}_{venue}_{timeframe}" for period, timeframe in expected_pairs
                     for venue in ("binance", "okx")}
    pair_keys = [(row.get("period"), row.get("timeframe")) for row in pairs]
    pair_partition_complete = len(pair_keys) == len(expected_pairs) and set(pair_keys) == expected_pairs
    run_partition_complete = set(runs) == expected_runs
    pending = (not raw or not run_partition_complete or not pair_partition_complete
               or any(row.get("reason") == "run_not_completed" for row in runs.values()))
    retrospective = "pending" if pending else combined_status(row.get("status") for row in pairs)
    failed_pairs = [{"period": row.get("period"), "timeframe": row.get("timeframe")}
                    for row in pairs if row.get("status") == "fail"]
    # The registered eight jobs are retrospective transfer diagnostics. They
    # never supply the cost stress, rolling or unseen evidence for admission.
    # Keep observed failures even when another pair remains unsupported.
    research = "fail" if failed_pairs else "pending" if pending else "insufficient"
    return {"schema": "strategy_review_cross_assessment/v2", "pipeline_status": "pending" if pending else "complete",
            "retrospective_status": retrospective, "research_status": research,
            "pair_partition_complete": pair_partition_complete, "run_partition_complete": run_partition_complete,
            "observed_failed_pairs": failed_pairs, "not_admission_test": True,
            "missing_required_research_evidence": ["cross_market_cost_reruns_1.5_and_2",
                "cross_market_eleven_window_stability", "mature_unseen_observation"],
            "prospective_status": "pending_unseen_evidence",
            "interpretation": "Retrospective pair pass does not establish complete research admission; no additional trials inferred",
            "paired_comparisons": pairs, "runs": runs, "assumptions": raw.get("assumptions", {}),
            "historical_margin_account_replication": False}


def diagnostic_summaries(batch, protocol, matrix):
    run_rows, score_rows = [], []
    for name in matrix.get("name", []):
        folder = batch / "matrix_results/runs" / name
        evidence = read(folder / "review_diagnostics.json", {})
        if not evidence:
            continue
        row = {"name": name, "arm": name.rsplit("__", 1)[0], "status": evidence.get("status"),
               "candidate_count": evidence.get("candidate_count"), "competing_batch_count": evidence.get("competing_batch_count"),
               "unmatched_close_event_count": evidence.get("unmatched_close_event_count")}
        for key, value in evidence.get("setup_outcomes", {}).items():
            row[f"setups_{key}"] = value
        for filename, columns in (
            ("review_state_transitions.csv", ["confirmation_delay_bars"]),
            ("review_setup_timing.csv", ["signal_to_fill_bars"]),
            ("review_exit_quality.csv", ["net_profit_giveback", "observed_trend_capture"]),
            ("review_reentry_costs.csv", ["exit_and_reentry_execution_cost"]),
        ):
            frame = csv(folder / filename)
            for column in columns:
                values = pd.to_numeric(frame.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
                row[column + "_median"] = float(values.median()) if len(values) else None
                row[column + "_observations"] = len(values)
        scores = csv(folder / "review_score_buckets.csv")
        score_rows.extend({"name": name, "arm": row["arm"], **item} for item in scores.to_dict("records"))
        run_rows.append(row)
    run_frame = pd.DataFrame(run_rows)
    families = []
    for family in sorted({name for arm in protocol["arms"] for name in arm["families"]}):
        members = {arm["arm"] for arm in protocol["arms"] if family in arm["families"]}
        subset = run_frame.loc[run_frame.arm.isin(members)] if len(run_frame) else pd.DataFrame()
        row = {"family": family, "registered_arms": len(members), "diagnostic_runs": len(subset),
               "expected_runs": len(members) * len(protocol["rolling_windows"]),
               "interpretation": "descriptive_medians_of_run_summaries; overlapping_windows_not_independent_samples"}
        for column in ("candidate_count", "competing_batch_count", "confirmation_delay_bars_median", "signal_to_fill_bars_median", "net_profit_giveback_median", "observed_trend_capture_median", "exit_and_reentry_execution_cost_median"):
            values = pd.to_numeric(subset.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
            row[column] = float(values.median()) if len(values) else None
        families.append(row)
    return run_frame, pd.DataFrame(families), pd.DataFrame(score_rows)
