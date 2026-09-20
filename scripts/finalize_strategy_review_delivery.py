"""Publish a new, auditable delivery index without changing sealed research."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def status_of(payload):
    if payload.get("errors") or any(value is False for value in payload.get("validation", {}).values()):
        return "fail"
    return "pass" if payload.get("status") == "complete" else "insufficient"


def combine_status(values):
    values = list(values)
    return "fail" if "fail" in values else "insufficient" if not values or any(value != "pass" for value in values) else "pass"


def account_summary(payload):
    summary = {key: payload.get(key) for key in ("schema", "policy", "status", "validation", "errors", "protocol",
        "input_identity", "implementation_version")}
    accounts = []
    for account in payload.get("accounts", []):
        valid = status_of(payload) == "pass" and account.get("status") == "completed" and account.get("halt_time") is None and all(
            isinstance(account.get(key), (int, float)) and not isinstance(account.get(key), bool)
            and math.isfinite(account[key]) for key in ("return_fraction", "max_drawdown_fraction"))
        row = {**account, "metrics_valid": valid, "comparison_eligible": valid and account.get("activity") == "active" and account.get("fills", 0) > 0}
        if not valid:
            row.update(net_pnl=None, return_fraction=None, max_drawdown_fraction=None)
        accounts.append(row)
    summary["accounts"] = accounts
    summary["status"] = "complete" if status_of(payload) == "pass" and len(accounts) == 3 and {row.get("arm") for row in accounts} == {"baseline", "gate", "sizing"} and all(row["metrics_valid"] for row in accounts) else "incomplete"
    summary["recovery_scope"] = "exact cached account summaries; no derived closed-cohort attribution or account ranking"
    summary["interpretation"] = "finite_capital_policy_replay_no_causal_uplift_no_live_admission"
    return summary


def inspect_csv(path, prior=None):
    digest = sha(path)
    if prior and prior.get("sha256") == digest and prior.get("bytes") == path.stat().st_size:
        return {key: prior[key] for key in ("sha256", "bytes", "row_count", "columns")}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        fields = next(reader)
        count = sum(1 for _ in reader)
    return {"sha256": digest, "bytes": path.stat().st_size, "row_count": count, "columns": fields}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch, output = args.batch.resolve(), args.output.resolve()
    if output == batch or output.is_relative_to(batch):
        raise ValueError("Append-only delivery must be outside the historical batch")
    meta = batch / "meta_review"
    original = read(batch / "completion.json")
    off, on = (read(output / f"{name}_projection.json") for name in ("official_off", "official_p0"))
    identity = read(meta / "identity.json")
    producer = read(batch / "meta_export_resume_amendment.json")
    if identity["runner"] != producer["original_driver_sha256"]:
        raise ValueError("Original producer differs from recovery amendment")
    integrity = {}
    for name in ("baseline", "revised"):
        manifest = read(batch / f"{name}_manifest.json")
        checks = {}
        for kind, folder in (("source_hashes", f"{name}_source"), ("input_hashes", "frozen_inputs")):
            mismatches = [relative for relative, expected in manifest.get(kind, {}).items()
                          if not (batch / folder / relative).is_file() or sha(batch / folder / relative) != expected]
            checks[kind] = {"verified_files": len(manifest.get(kind, {})), "mismatches": mismatches,
                            "status": "pass" if not mismatches else "fail"}
        integrity[name] = checks
    if identity["source_hashes"] != read(batch / "revised_manifest.json")["source_hashes"]:
        raise ValueError("Historical producer source identity mismatch")
    if identity["protocol"] != sha(batch / "review_protocol.json"):
        raise ValueError("Historical producer protocol mismatch")
    inventory, counts = {}, {}
    for phase, expected in original["expected_run_counts"].items():
        summaries = sorted((batch / f"{phase}_results/runs").glob("*/summary.json"))
        counts[phase] = {"expected": expected, "actual": len(summaries), "status": "pass" if len(summaries) == expected else "fail"}
        for summary in summaries:
            sidecar = summary.parent / "review_identity.json"
            inventory[summary.relative_to(batch).as_posix()] = {"summary_sha256": sha(summary),
                "review_identity_sha256": sha(sidecar) if sidecar.is_file() else None}
    write(output / "historical_run_inventory.json", {"counts": counts, "runs": inventory, "frozen_manifest_recheck": integrity})
    isolation = {key: off["payload"][key] == on["payload"][key] for key in ("digest", "health", "allocation")}
    write(output / "official_isolation.json", {"status": "pass" if all(isolation.values()) else "fail", "checks": isolation,
        "scope": "full_60_coin_history_real_engine_all_P0_P1_P2_P3_switches",
        "method": "new exact comparison of verified sealed off/on cache facts; no engine rerun",
        "caches": {"off": off["cache"], "on": on["cache"]}, "original_flags": read(meta / "official_isolation.json")})
    p2 = on["payload"]["p2"]
    p2_summary = {key: p2.get(key) for key in ("schema", "policy", "status", "model_version", "validation", "errors", "input_identity", "protocol")}
    p2_summary.update(recovery_schema="projected_frozen_p2_summary/v1", evidence="diagnostic_only", original_payload_digest_recomputed=False)
    sealed = read(meta / "report_reuse_manifest.json")["files"]
    sealed.update(producer["interrupted_report_attempt"]["completed_exports"])
    source_files = {}
    prior_csv = read(output / "p2_summary.json").get("csv_reconciliation", {}) if (output / "p2_summary.json").is_file() else {}
    mapping = {"p2_predictions.csv": "predictions", "p2_evaluations.csv": "evaluations", "p2_folds.csv": "folds",
        "p2_cells.csv": "cell_snapshots", "p2_attribution.csv": "attribution", "p2_calibration_predictions.csv": "calibration_predictions"}
    for name, field in mapping.items():
        print(f"AUDIT {name}", flush=True)
        record = inspect_csv(meta / name, prior_csv.get(name))
        value = p2[field]
        cache_count = value["count"] if isinstance(value, dict) and value.get("projection") == "omitted_table" else len(value)
        record.update(cache_field=field, cache_row_count=cache_count, row_count_matches_cache=record["row_count"] == cache_count,
            sealed_hash_matches=record["sha256"] == sealed[name] if name in sealed else None,
            assurance="sealed export hash + cached table count" if name in sealed else "current file hash and cache row-count reconciliation; no original full-export hash recorded")
        source_files[name] = record
        p2_summary[field + "_count"] = cache_count
    for name in ("ev_predictions.csv", "ev_evaluations.csv", "ev_diagnostics.csv", "p2_diagnostics.csv"):
        source_files[name] = inspect_csv(meta / name, prior_csv.get(name))
    with (meta / "p2_diagnostics.csv").open(encoding="utf-8-sig", newline="") as stream:
        diagnostics = list(csv.DictReader(stream))
    cohorts = [row for row in diagnostics if row["diagnostic_kind"] == "cohort"]
    statuses = Counter()
    for row in cohorts:
        statuses[row["predicted_status"]] += int(row["prediction_count"])
    p2_summary["status_counts"] = dict(statuses)
    p2_summary["diagnostic_cohort_count"] = len(cohorts)
    p2_summary["forecast_comparisons"] = [row for row in diagnostics if row["diagnostic_kind"] == "paired_mse"]
    independent = read(meta / "independent_review.json")
    p2_summary["attribution_audit"] = {"global": independent["p2_attribution_global"], "primary": independent["p2_attribution_primary"]}
    p2_summary["csv_reconciliation"] = source_files
    p2_summary["limitations"] = ["Descriptive forecasts do not establish causal uplift, new out-of-sample evidence or live admission.",
        "No original full-export checksum exists for the resumed calibration table; its row count is reconciled to the sealed cache.",
        "Uniform weights, abstention and failed research gates remain visible; no candidate is selected by final outcomes."]
    write(output / "p2_summary.json", p2_summary)
    p3 = account_summary(on["payload"]["p3"])
    p3["cache_sha256"] = on["cache"]["sha256"]
    p3["table_counts"] = {key: value for key, value in on["omitted_tables"].items() if key.startswith("p3/")}
    write(output / "p3_summary.json", p3)
    cross = read(batch / "cross_market/comparison.json")
    cross_audit = read(batch / "cross_market/delivery_audit.json")
    cross_checks = []
    for item in cross_audit.get("references", []):
        path = batch / item["path"]
        cross_checks.append({"path": item["path"], "sha256": sha(path), "matches": sha(path) == item["sha256"]})
    run_checks = []
    for run in cross_audit["runs"]:
        completion_path = batch / run["completion"]["path"]
        completion = read(completion_path)
        artifact_mismatch = [name for name, expected in completion["artifacts"].items()
                             if sha(completion_path.parent / name) != expected]
        input_mismatch = []
        for item in run["inputs"]:
            for file_field, hash_field in (("source_csv", "source_csv_sha256"), ("source_manifest", "source_manifest_sha256"),
                                           ("selected_csv", "selected_file_sha256")):
                if sha(batch / item[file_field]) != item[hash_field]:
                    input_mismatch.append(item[file_field])
        run_checks.append({"name": run["name"], "completion_hash_matches": sha(completion_path) == run["completion"]["sha256"],
            "artifact_count": len(completion["artifacts"]), "artifact_mismatch": artifact_mismatch, "input_mismatch": input_mismatch})
    write(output / "cross_market_summary.json", {"historical_comparison": cross, "reference_recheck": cross_checks,
        "run_artifact_recheck": run_checks, "historical_delivery_audit_sha256": sha(batch / "cross_market/delivery_audit.json"), "new_engine_runs": 0})
    controls = {name: read(output / name / "p3_summary.json") if (output / name / "p3_summary.json").is_file() else None
                for name in ("primary", "quarter_control")}
    meta_checks = {"p0": status_of(read(meta / "signal_observation_summary.json")), "p1": status_of(read(meta / "ev_summary.json")),
        "p2": status_of(p2_summary), "p3": status_of(p3), **{key: status_of(value) if value else "insufficient" for key, value in controls.items()},
        "official_isolation": "pass" if all(isolation.values()) else "fail"}
    checks = {"registered_626_results": "pass" if all(item["status"] == "pass" for item in counts.values()) and all(item["review_identity_sha256"] for item in inventory.values()) else "fail",
        "frozen_manifest_recheck": "pass" if all(item["status"] == "pass" for group in integrity.values() for item in group.values()) else "fail",
        "p2_csv_cache_row_reconciliation": "pass" if all(item.get("row_count_matches_cache", True) and item.get("sealed_hash_matches") is not False for item in source_files.values()) else "fail",
        "official_isolation": meta_checks["official_isolation"], "cross_market_references": "pass" if all(item["matches"] for item in cross_checks)
        and len(run_checks) == 8 and all(item["completion_hash_matches"] and not item["artifact_mismatch"] and not item["input_mismatch"] for item in run_checks) else "fail"}
    # Delivery completeness concerns reviewable outcomes, including fail/insufficient.
    # It does not convert failed research or unavailable historical evidence to pass.
    required = {name: {"status": "pass", "evidence": path} for name, path in {
        "matrix_diagnostics": str(batch / "matrix_results"), "family_assessment": str(batch / "family_assessment.json"),
        "official_archive_audit": str(batch / "public_data_archive_audit/manifest.json"),
        "prospective_protocol": str(batch / "prospective_protocol.json"), "meta_stratified_report": str(output / "meta_stratified/summary.json"),
        "p2_summary": str(output / "p2_summary.json"), "p3_summary": str(output / "p3_summary.json"),
        "official_isolation": str(output / "official_isolation.json"), "cross_market": str(output / "cross_market_summary.json")}.items()}
    required.update({name: {"status": "pass", "evidence": str(output / name / "p3_summary.json")}
                     for name in ("primary", "quarter_control")})
    for record in required.values():
        if not Path(record["evidence"]).exists():
            record["status"] = "insufficient"
    limitations = ["Historical study remains retrospective fail; admission remains paused_revalidation.",
        "No fresh official full-history replay under current source is claimed; off/on equality rechecks original sealed caches.",
        "Costs were charged at account level; historical closed-cohort financing attribution remains insufficient.",
        "Static historical pool survival bias, historical borrowing eligibility, capacity and independent-source anomalies remain unresolved.",
        "Resumed calibration CSV has no original full-export checksum; retained cache identity and row-count checks do not claim byte-equivalent serialization."]
    complete = {"schema": "strategy_review_append_only_completion/v3", "run_id": output.name,
        "generated_at": datetime.now(timezone.utc).isoformat(), "historical_batch": str(batch),
        "historical_completion_sha256": sha(batch / "completion.json"), "original_batch_modified": False,
        "engineering_status": combine_status([*checks.values(), *meta_checks.values()]),
        "delivery_status": "pass" if all(item["status"] == "pass" for item in required.values()) else "insufficient",
        "evidence_pipeline_complete": all(value == "pass" for value in [*checks.values(), *meta_checks.values()]),
        "checks": checks, "meta_checks": meta_checks, "required_deliverables": required,
        "research_status": original["research"]["retrospective_assessment"], "research": original["research"],
        "admission": original["admission"], "limitations": limitations,
        "new_execution": read(output / "control_replay_identity.json") if (output / "control_replay_identity.json").exists() else {"policy_replays": 0}}
    write(output / "completion.json", complete)
    write(output / "acceptance.json", {"task_id": "SYS-10", **complete})
    write(output / "required_deliverables.json", required)
    write(output / "delivery_receipt.json", complete)
    sources = {str(batch / name): sha(batch / name) for name in ("completion.json", "revised_manifest.json", "baseline_manifest.json", "review_protocol.json",
        "experiment_registry.json", "cost_attribution_summary.csv", "meta_review/identity.json", "meta_export_resume_amendment.json")}
    sources.update({str(meta / name): item["sha256"] for name, item in source_files.items()})
    sources.update({record["cache"]["file"]: record["cache"]["sha256"] for record in (off, on)})
    sources.update({item["evidence"]: sha(Path(item["evidence"])) for item in required.values()
                    if Path(item["evidence"]).is_file() and not Path(item["evidence"]).is_relative_to(output)})
    print(json.dumps({"checks": checks, "meta_checks": meta_checks, "engineering_status": complete["engineering_status"],
        "delivery_status": complete["delivery_status"], "research_status": complete["research_status"]}), flush=True)
    write(output / "evidence_manifest.json", {"sources": sources, "recovery_driver_sha256": sha(__file__),
        "frozen_producer": {"runner_sha256": identity["runner"], "identity_sha256": sha(meta / "identity.json"),
                            "source_manifest_sha256": sha(batch / "revised_manifest.json"), "protocol_sha256": identity["protocol"]},
        "outputs": {str(path.relative_to(output)): {"sha256": sha(path), "bytes": path.stat().st_size}
                    for path in sorted(output.rglob("*")) if path.is_file() and path.name != "evidence_manifest.json"}})


if __name__ == "__main__":
    main()
