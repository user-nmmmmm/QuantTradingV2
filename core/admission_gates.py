"""Live-trading admission gates: shadow, paper, reconciliation, and scale governance.

Implements the Phase 6 roadmap; the name says what the module does rather
than which roadmap phase asked for it.

The module turns the Phase 6 roadmap into a fail-closed evidence contract.  It
does not pretend that an 8-12 week paper run or a real-money observation has
happened: a gate remains failed until the corresponding dated evidence exists.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence


RECONCILIATION_LAYERS = (
    "signals",
    "orders",
    "fills",
    "positions",
    "costs",
    "pnl",
)
MONITORING_DIMENSIONS = (
    "lifecycle",
    "costs",
    "drawdown",
    "regime",
    "data_quality",
)
EXPANSION_DIMENSIONS = (
    "capital",
    "symbols",
    "exchanges",
    "strategies",
    "leverage",
    "unattended_hours",
)
_IGNORED_COMPARISON_FIELDS = {
    "observed_at",
    "recorded_at",
    "received_at",
    "source",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _passed(report: Any) -> bool:
    return isinstance(report, Mapping) and report.get("passed") is True


def _records_by_id(
    records: Sequence[Mapping[str, Any]], *, layer: str,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    issues: list[str] = []
    for number, record in enumerate(records, 1):
        record_id = str(record.get("record_id", "")).strip()
        if not record_id:
            issues.append(f"{layer}:record_{number}:missing_record_id")
            continue
        if record_id in indexed:
            issues.append(f"{layer}:{record_id}:duplicate_record_id")
            continue
        indexed[record_id] = record
    return indexed, issues


def _values_equal(expected: Any, actual: Any, tolerance: float) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isfinite(float(expected)) and math.isfinite(float(actual)) and abs(
            float(expected) - float(actual)
        ) <= tolerance
    return expected == actual


def reconcile_lifecycle(
    expected: Mapping[str, Sequence[Mapping[str, Any]]],
    actual: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    numeric_tolerance: float = 1e-8,
) -> dict[str, Any]:
    """Reconcile signal/order/fill/position/cost/PnL records one by one."""
    if numeric_tolerance < 0:
        raise ValueError("numeric_tolerance cannot be negative")
    issues: list[str] = []
    layer_reports: dict[str, Any] = {}
    total_expected = 0
    total_matched = 0
    for layer in RECONCILIATION_LAYERS:
        expected_rows, expected_issues = _records_by_id(
            list(expected.get(layer, [])), layer=f"expected.{layer}",
        )
        actual_rows, actual_issues = _records_by_id(
            list(actual.get(layer, [])), layer=f"actual.{layer}",
        )
        issues.extend(expected_issues)
        issues.extend(actual_issues)
        missing = sorted(set(expected_rows) - set(actual_rows))
        unexpected = sorted(set(actual_rows) - set(expected_rows))
        mismatches: list[dict[str, Any]] = []
        matched = 0
        for record_id in sorted(set(expected_rows) & set(actual_rows)):
            expected_record = expected_rows[record_id]
            actual_record = actual_rows[record_id]
            differing_fields = []
            for field, expected_value in expected_record.items():
                if field == "record_id" or field in _IGNORED_COMPARISON_FIELDS:
                    continue
                if field not in actual_record or not _values_equal(
                    expected_value, actual_record.get(field), numeric_tolerance,
                ):
                    differing_fields.append(field)
            if differing_fields:
                mismatches.append({
                    "record_id": record_id,
                    "fields": sorted(differing_fields),
                })
            else:
                matched += 1
        expected_count = len(expected_rows)
        coverage = matched / expected_count if expected_count else 0.0
        layer_passed = bool(
            expected_count > 0
            and coverage == 1.0
            and not missing
            and not unexpected
            and not mismatches
            and not expected_issues
            and not actual_issues
        )
        if not layer_passed:
            issues.append(f"{layer}:reconciliation_not_complete")
        total_expected += expected_count
        total_matched += matched
        layer_reports[layer] = {
            "passed": layer_passed,
            "expected_count": expected_count,
            "actual_count": len(actual_rows),
            "matched_count": matched,
            "coverage": coverage,
            "missing_record_ids": missing,
            "unexpected_record_ids": unexpected,
            "mismatches": mismatches,
        }
    overall_coverage = total_matched / total_expected if total_expected else 0.0
    return {
        "schema_version": 1,
        "task": "T-6.3",
        "generated_at": _utc_now(),
        "passed": bool(overall_coverage == 1.0 and all(
            item["passed"] for item in layer_reports.values()
        )),
        "coverage": overall_coverage,
        "expected_record_count": total_expected,
        "matched_record_count": total_matched,
        "layers": layer_reports,
        "issues": sorted(set(issues)),
    }


def evaluate_shadow(
    backtest_signals: Sequence[Mapping[str, Any]],
    shadow_signals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify signal identity, bar time, action, strategy, and rule version."""
    expected = {layer: [] for layer in RECONCILIATION_LAYERS}
    actual = {layer: [] for layer in RECONCILIATION_LAYERS}
    expected["signals"] = list(backtest_signals)
    actual["signals"] = list(shadow_signals)
    report = reconcile_lifecycle(expected, actual)
    signal = report["layers"]["signals"]
    rule_versions_present = bool(backtest_signals) and all(
        str(item.get("rule_version", "")).strip() for item in backtest_signals
    )
    issues = []
    if not rule_versions_present:
        issues.append("signal_rule_version_missing")
    if not signal["passed"]:
        issues.append("shadow_signal_mismatch")
    return {
        "schema_version": 1,
        "task": "T-6.1",
        "generated_at": _utc_now(),
        "passed": signal["passed"] and rule_versions_present,
        "signal_count": signal["expected_count"],
        "matched_count": signal["matched_count"],
        "coverage": signal["coverage"],
        "details": signal,
        "issues": issues,
    }


def audit_paper_run(
    observations: Sequence[Mapping[str, Any]],
    *,
    minimum_days: int = 56,
    minimum_regimes: int = 2,
) -> dict[str, Any]:
    """Audit elapsed duration, regime coverage, and unresolved incidents."""
    if minimum_days < 1 or minimum_regimes < 1:
        raise ValueError("minimum_days and minimum_regimes must be positive")
    issues: list[str] = []
    timestamps: list[datetime] = []
    regimes: set[str] = set()
    unresolved = 0
    for number, row in enumerate(observations, 1):
        try:
            timestamps.append(_parse_timestamp(row.get("timestamp")))
        except (TypeError, ValueError):
            issues.append(f"observation_{number}:invalid_timestamp")
        regime = str(row.get("regime", "")).strip()
        if regime:
            regimes.add(regime)
        if row.get("incident_status") not in (None, "resolved", "explained"):
            unresolved += 1
    elapsed_days = 0
    if timestamps:
        elapsed_days = (max(timestamps).date() - min(timestamps).date()).days + 1
    if elapsed_days < minimum_days:
        issues.append(f"paper_duration_too_short:{elapsed_days}<{minimum_days}")
    if len(regimes) < minimum_regimes:
        issues.append(f"market_regime_coverage_too_low:{len(regimes)}<{minimum_regimes}")
    if unresolved:
        issues.append(f"unresolved_incidents:{unresolved}")
    if not observations:
        issues.append("paper_observations_missing")
    return {
        "schema_version": 1,
        "task": "T-6.2",
        "generated_at": _utc_now(),
        "passed": not issues,
        "observation_count": len(observations),
        "start": min(timestamps).isoformat() if timestamps else None,
        "end": max(timestamps).isoformat() if timestamps else None,
        "elapsed_days": elapsed_days,
        "minimum_days": minimum_days,
        "regimes": sorted(regimes),
        "minimum_regimes": minimum_regimes,
        "unresolved_incidents": unresolved,
        "issues": issues,
    }


def calibrate_execution_model(
    observations: Sequence[Mapping[str, Any]],
    *,
    tolerances: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Compare modeled and observed slippage, spread, rejection, and latency."""
    limits = {
        "slippage_mae_bps": 2.0,
        "spread_mae_bps": 2.0,
        "rejection_rate_error": 0.05,
        "latency_mae_ms": 100.0,
    }
    if tolerances:
        limits.update({key: float(value) for key, value in tolerances.items()})
    if any(value < 0 for value in limits.values()):
        raise ValueError("calibration tolerances cannot be negative")
    issues: list[str] = []
    errors = {
        "slippage_mae_bps": [],
        "spread_mae_bps": [],
        "latency_mae_ms": [],
    }
    predicted_rejections = 0
    actual_rejections = 0
    valid_rows = 0
    for number, row in enumerate(observations, 1):
        try:
            modeled_slippage = float(row["modeled_slippage_bps"])
            actual_slippage = float(row["actual_slippage_bps"])
            modeled_spread = float(row["modeled_spread_bps"])
            actual_spread = float(row["actual_spread_bps"])
            modeled_latency = float(row["modeled_latency_ms"])
            actual_latency = float(row["actual_latency_ms"])
            predicted_rejected = row["modeled_rejected"]
            actual_rejected = row["actual_rejected"]
            if not isinstance(predicted_rejected, bool) or not isinstance(actual_rejected, bool):
                raise TypeError("rejection fields must be booleans")
            numeric = (
                modeled_slippage, actual_slippage, modeled_spread,
                actual_spread, modeled_latency, actual_latency,
            )
            if not all(math.isfinite(value) for value in numeric):
                raise ValueError("calibration values must be finite")
        except (KeyError, TypeError, ValueError):
            issues.append(f"observation_{number}:invalid_calibration_record")
            continue
        errors["slippage_mae_bps"].append(abs(actual_slippage - modeled_slippage))
        errors["spread_mae_bps"].append(abs(actual_spread - modeled_spread))
        errors["latency_mae_ms"].append(abs(actual_latency - modeled_latency))
        predicted_rejections += int(predicted_rejected)
        actual_rejections += int(actual_rejected)
        valid_rows += 1
    metrics: dict[str, float | None] = {
        name: mean(values) if values else None for name, values in errors.items()
    }
    metrics["rejection_rate_error"] = (
        abs(predicted_rejections / valid_rows - actual_rejections / valid_rows)
        if valid_rows else None
    )
    gates = {
        name: value is not None and value <= limits[name]
        for name, value in metrics.items()
    }
    for name, passed in gates.items():
        if not passed:
            issues.append(f"{name}:outside_tolerance")
    if valid_rows == 0:
        issues.append("calibration_observations_missing")
    return {
        "schema_version": 1,
        "task": "T-6.4",
        "generated_at": _utc_now(),
        "passed": valid_rows > 0 and all(gates.values()) and not any(
            issue.endswith("invalid_calibration_record") for issue in issues
        ),
        "sample_size": valid_rows,
        "metrics": metrics,
        "tolerances": limits,
        "gates": gates,
        "issues": issues,
    }


def evaluate_monitoring(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    alert_delivery_verified: bool,
) -> dict[str, Any]:
    """Audit the five required dashboard dimensions and alert delivery."""
    issues: list[str] = []
    alerts: list[dict[str, Any]] = []
    dimension_counts = {name: 0 for name in MONITORING_DIMENSIONS}
    for number, snapshot in enumerate(snapshots, 1):
        timestamp = snapshot.get("timestamp")
        try:
            _parse_timestamp(timestamp)
        except (TypeError, ValueError):
            issues.append(f"snapshot_{number}:invalid_timestamp")
        for dimension in MONITORING_DIMENSIONS:
            section = snapshot.get(dimension)
            if not isinstance(section, Mapping) or not isinstance(section.get("ok"), bool):
                issues.append(f"snapshot_{number}:{dimension}:missing_or_invalid")
                continue
            dimension_counts[dimension] += 1
            if section["ok"] is False:
                alerts.append({
                    "timestamp": timestamp,
                    "dimension": dimension,
                    "reason": section.get("reason") or "health_check_failed",
                })
    if not snapshots:
        issues.append("monitoring_snapshots_missing")
    if not alert_delivery_verified:
        issues.append("critical_alert_delivery_not_verified")
    for dimension, count in dimension_counts.items():
        if count != len(snapshots):
            issues.append(f"{dimension}:coverage_incomplete")
    return {
        "schema_version": 1,
        "task": "T-6.5",
        "generated_at": _utc_now(),
        "passed": bool(snapshots) and alert_delivery_verified and not issues,
        "snapshot_count": len(snapshots),
        "dimension_counts": dimension_counts,
        "alert_delivery_verified": alert_delivery_verified,
        "generated_alerts": alerts,
        "latest_snapshot": dict(snapshots[-1]) if snapshots else None,
        "issues": sorted(set(issues)),
    }


def review_admission(
    *,
    p0_issues: Sequence[Mapping[str, Any]],
    holdout_report: Mapping[str, Any],
    shadow_report: Mapping[str, Any],
    paper_report: Mapping[str, Any],
    reconciliation_report: Mapping[str, Any],
    calibration_report: Mapping[str, Any],
    monitoring_report: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute the T-6.6 fail-closed production admission review."""
    open_p0 = sorted(
        str(item.get("id", "UNKNOWN"))
        for item in p0_issues
        if str(item.get("status", "")).lower() not in {"closed", "resolved"}
    )
    holdout_gates = holdout_report.get("gates")
    holdout_passed = bool(
        holdout_report.get("decision") == "admit"
        and isinstance(holdout_gates, Mapping)
        and holdout_gates
        and all(value is True for value in holdout_gates.values())
    )
    operator_approved = bool(
        approval.get("approved") is True
        and str(approval.get("operator", "")).strip()
        and str(approval.get("approved_at", "")).strip()
    )
    gates = {
        "all_p0_closed": bool(p0_issues) and not open_p0,
        "final_holdout_admitted": holdout_passed,
        "shadow_consistent": _passed(shadow_report),
        "paper_duration_and_regimes": _passed(paper_report),
        "lifecycle_reconciliation_100pct": bool(
            _passed(reconciliation_report)
            and reconciliation_report.get("coverage") == 1.0
        ),
        "execution_model_calibrated": _passed(calibration_report),
        "monitoring_and_alerting_ready": _passed(monitoring_report),
        "independent_operator_approval": operator_approved,
    }
    failed = sorted(name for name, passed in gates.items() if not passed)
    return {
        "schema_version": 1,
        "task": "T-6.6",
        "generated_at": _utc_now(),
        "passed": all(gates.values()),
        "decision": "approve_micro_live" if all(gates.values()) else "reject",
        "gates": gates,
        "open_p0": open_p0,
        "approval": dict(approval),
        "issues": [f"gate_failed:{name}" for name in failed],
    }


def evaluate_micro_live(
    *,
    admission_report: Mapping[str, Any],
    scope: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    reconciliation_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the T-6.7 report without initiating or authorizing trading."""
    issues: list[str] = []
    if not _passed(admission_report):
        issues.append("admission_not_approved")
    for field in ("exchange", "symbol", "strategy"):
        value = scope.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"scope:{field}:must_be_single_nonempty_value")
    try:
        if float(scope.get("capital", 0)) <= 0:
            issues.append("scope:capital:must_be_positive")
        if not 0 < float(scope.get("leverage", 0)) <= 1.0:
            issues.append("scope:leverage:must_be_in_(0,1]")
    except (TypeError, ValueError):
        issues.append("scope:numeric_limits_invalid")
    unexplained = [
        str(item.get("event_id", "UNKNOWN"))
        for item in observations
        if item.get("status") not in {"matched", "resolved", "explained"}
    ]
    if not observations:
        issues.append("micro_live_observations_missing")
    if unexplained:
        issues.append(f"unexplained_events:{len(unexplained)}")
    if not _passed(reconciliation_report) or reconciliation_report.get("coverage") != 1.0:
        issues.append("micro_live_reconciliation_not_complete")
    return {
        "schema_version": 1,
        "task": "T-6.7",
        "generated_at": _utc_now(),
        "passed": not issues,
        "scope": dict(scope),
        "observation_count": len(observations),
        "unexplained_event_ids": unexplained,
        "issues": issues,
    }


def review_expansion(
    *,
    micro_live_report: Mapping[str, Any],
    current_scope: Mapping[str, Any],
    proposed_scope: Mapping[str, Any],
    capacity_review: Mapping[str, Any],
    cost_review: Mapping[str, Any],
    risk_review: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Require capacity, cost, and risk review before every one-step expansion."""
    changed = [
        field for field in EXPANSION_DIMENSIONS
        if current_scope.get(field) != proposed_scope.get(field)
    ]
    gates = {
        "micro_live_passed": _passed(micro_live_report),
        "exactly_one_dimension_changed": len(changed) == 1,
        "capacity_review_passed": _passed(capacity_review),
        "cost_review_passed": _passed(cost_review),
        "risk_review_passed": _passed(risk_review),
        "operator_approved": bool(
            approval.get("approved") is True
            and str(approval.get("operator", "")).strip()
            and str(approval.get("approved_at", "")).strip()
        ),
    }
    failed = sorted(name for name, passed in gates.items() if not passed)
    return {
        "schema_version": 1,
        "task": "T-6.8",
        "generated_at": _utc_now(),
        "passed": all(gates.values()),
        "decision": "approve_expansion" if all(gates.values()) else "reject",
        "changed_dimensions": changed,
        "current_scope": dict(current_scope),
        "proposed_scope": dict(proposed_scope),
        "gates": gates,
        "issues": [f"gate_failed:{name}" for name in failed],
    }


def evaluate_phase6(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one complete Phase 6 evidence bundle in dependency order."""
    shadow = evaluate_shadow(
        bundle.get("backtest_signals", []), bundle.get("shadow_signals", []),
    )
    paper = audit_paper_run(
        bundle.get("paper_observations", []),
        minimum_days=int(bundle.get("minimum_paper_days", 56)),
        minimum_regimes=int(bundle.get("minimum_market_regimes", 2)),
    )
    reconciliation = reconcile_lifecycle(
        bundle.get("expected_lifecycle", {}), bundle.get("actual_lifecycle", {}),
        numeric_tolerance=float(bundle.get("reconciliation_tolerance", 1e-8)),
    )
    calibration = calibrate_execution_model(
        bundle.get("execution_observations", []),
        tolerances=bundle.get("calibration_tolerances"),
    )
    monitoring = evaluate_monitoring(
        bundle.get("monitoring_snapshots", []),
        alert_delivery_verified=bundle.get("alert_delivery_verified") is True,
    )
    admission = review_admission(
        p0_issues=bundle.get("p0_issues", []),
        holdout_report=bundle.get("holdout_report", {}),
        shadow_report=shadow,
        paper_report=paper,
        reconciliation_report=reconciliation,
        calibration_report=calibration,
        monitoring_report=monitoring,
        approval=bundle.get("admission_approval", {}),
    )
    micro = evaluate_micro_live(
        admission_report=admission,
        scope=bundle.get("micro_live_scope", {}),
        observations=bundle.get("micro_live_observations", []),
        reconciliation_report=bundle.get("micro_live_reconciliation", {}),
    )
    expansion = review_expansion(
        micro_live_report=micro,
        current_scope=bundle.get("current_scope", {}),
        proposed_scope=bundle.get("proposed_scope", {}),
        capacity_review=bundle.get("capacity_review", {}),
        cost_review=bundle.get("cost_review", {}),
        risk_review=bundle.get("risk_review", {}),
        approval=bundle.get("expansion_approval", {}),
    )
    reports = {
        report["task"]: report
        for report in (
            shadow, paper, reconciliation, calibration,
            monitoring, admission, micro, expansion,
        )
    }
    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "phase": "Phase 6",
        "passed": all(_passed(report) for report in reports.values()),
        "admission_passed": admission["passed"],
        "tasks": reports,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate Phase 6 evidence fail-closed")
    parser.add_argument("--input", required=True, help="Phase 6 evidence bundle JSON")
    parser.add_argument("--output", default="reports/phase6/phase6_report.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        bundle = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise TypeError("evidence bundle must be a JSON object")
        report = evaluate_phase6(bundle)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "phase": "Phase 6",
            "passed": False,
            "admission_passed": False,
            "issues": [f"invalid_evidence_bundle:{type(exc).__name__}:{exc}"],
        }
    _atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
