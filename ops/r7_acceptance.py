"""Fail-closed audit of the evidence required for the R7 soak exit gate."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


HALT_EVENTS = {
    "risk_halt",
    "tick_unhealthy",
    "circuit_breaker_triggered",
    "circuit_breaker_restored",
}
REQUIRED_CLOSURES = tuple(f"G{number}" for number in range(11, 17))


def _days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end date must not be before start date")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return records, errors
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise TypeError("record is not an object")
                records.append(record)
            except (json.JSONDecodeError, TypeError):
                errors.append(f"{path}:{number}:invalid_jsonl_record")
    return records, errors


def _event_key(record: dict[str, Any]) -> str:
    return f"{record.get('timestamp', '')}|{record.get('event', '')}"


def audit_r7(
    *,
    start: date,
    end: date,
    reconciliation_dir: str,
    alerts_path: str,
    incidents_path: str,
    closures_path: str,
    account_id: str,
    minimum_days: int = 14,
) -> dict[str, Any]:
    expected_days = _days(start, end)
    issues: list[str] = []
    if minimum_days <= 0:
        issues.append("invalid_minimum_days")
    if len(expected_days) < minimum_days:
        issues.append(
            f"soak_too_short: observed={len(expected_days)} required={minimum_days}"
        )

    safe_account = "".join(
        char if char.isalnum() or char in "-_." else "_" for char in account_id
    )
    reconciliations: list[dict[str, Any]] = []
    for trading_day in expected_days:
        path = Path(reconciliation_dir) / f"{trading_day.isoformat()}_{safe_account}.json"
        if not path.exists():
            issues.append(f"missing_reconciliation:{trading_day.isoformat()}")
            continue
        try:
            payload = _json(path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append(f"invalid_reconciliation:{path}")
            continue
        if not isinstance(payload, dict) or payload.get("trading_day") != trading_day.isoformat():
            issues.append(f"invalid_reconciliation_schema:{path}")
            continue
        reconciliations.append(payload)
        discrepancy_count = payload.get("discrepancy_count")
        if (
            payload.get("ok") is not True
            or type(discrepancy_count) is not int
            or discrepancy_count != 0
        ):
            issues.append(f"reconciliation_difference:{trading_day.isoformat()}")

    alert_file = Path(alerts_path)
    if not alert_file.exists():
        issues.append(f"missing_alert_log:{alert_file}")
    alerts, alert_errors = _jsonl(alert_file)
    incidents, incident_errors = _jsonl(Path(incidents_path))
    issues.extend(alert_errors)
    issues.extend(incident_errors)
    resolution_by_key = {
        str(item.get("event_key")): item
        for item in incidents
        if item.get("event_key")
    }
    halt_events: list[dict[str, Any]] = []
    for alert in alerts:
        if alert.get("event") not in HALT_EVENTS:
            continue
        try:
            occurred = datetime.fromisoformat(
                str(alert.get("timestamp", "")).replace("Z", "+00:00")
            ).date()
        except ValueError:
            issues.append(f"invalid_alert_timestamp:{_event_key(alert)}")
            continue
        if occurred not in expected_days:
            continue
        halt_events.append(alert)
        key = _event_key(alert)
        resolution = resolution_by_key.get(key, {})
        if (
            resolution.get("outcome") not in {"resolved", "explained"}
            or not str(resolution.get("explanation", "")).strip()
        ):
            issues.append(f"unresolved_halt_or_alert:{key}")

    closures: dict[str, Any] = {}
    closure_file = Path(closures_path)
    try:
        closures_payload = _json(closure_file)
        if isinstance(closures_payload, dict):
            closures = closures_payload
        else:
            issues.append(f"invalid_closure_schema:{closure_file}")
    except (OSError, UnicodeError, json.JSONDecodeError):
        issues.append(f"missing_or_invalid_closures:{closure_file}")
    for task_id in REQUIRED_CLOSURES:
        if str(closures.get(task_id, "")).lower() != "closed":
            issues.append(f"p0_p1_not_closed:{task_id}")

    return {
        "schema_version": 1,
        "job": "r7_soak_acceptance",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "minimum_days": minimum_days,
        "observed_days": len(expected_days),
        "reconciliation_reports": len(reconciliations),
        "halt_alert_events": len(halt_events),
        "required_closures": list(REQUIRED_CLOSURES),
        "ok": not issues,
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit the R7 sandbox soak evidence")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--reconciliation-dir", default="reports/reconciliation")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--incidents", default="reports/incident_resolutions.jsonl")
    parser.add_argument("--closures", default="reports/r7_p0_p1_closures.json")
    parser.add_argument("--minimum-days", type=int, default=14)
    parser.add_argument("--output", default="reports/r7_acceptance.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = audit_r7(
        start=args.start,
        end=args.end,
        reconciliation_dir=args.reconciliation_dir,
        alerts_path=args.alerts,
        incidents_path=args.incidents,
        closures_path=args.closures,
        account_id=args.account_id,
        minimum_days=args.minimum_days,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
