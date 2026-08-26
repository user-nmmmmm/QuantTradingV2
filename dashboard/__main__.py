"""CLI dashboard for live status, health reasons, and recent alerts."""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any


def recent_alerts(path: str, limit: int = 10) -> list[dict[str, Any]]:
    alerts: deque[dict[str, Any]] = deque(maxlen=limit)
    alert_path = Path(path)
    if not alert_path.exists():
        return []
    with alert_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    alerts.append(record)
            except (json.JSONDecodeError, TypeError):
                continue
    return list(alerts)


def _load_phase6_monitoring(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("tasks"), dict):
        payload = payload["tasks"].get("T-6.5") or {}
    return payload if isinstance(payload, dict) and payload.get("task") == "T-6.5" else None


def _invalid_dashboard(
    alerts_path: str, alert_limit: int, phase6_path: str | None = None,
) -> dict[str, Any]:
    """Return no financial facts when the status snapshot is untrustworthy."""
    return {
        "status_valid": False,
        "timestamp": None,
        "equity": None,
        "cash": None,
        "positions": {},
        "healthy": False,
        "operational_state": "RISK_HALTED",
        "health_reason_codes": ["STATUS_FILE_INVALID"],
        "health_reasons": [{
            "code": "STATUS_FILE_INVALID",
            "subject": "live_status.json",
            "message": "status snapshot is missing, malformed, or has an invalid schema",
        }],
        "recent_alerts": recent_alerts(alerts_path, alert_limit),
        "phase6_monitoring": _load_phase6_monitoring(phase6_path),
    }


def load_dashboard(
    status_path: str,
    alerts_path: str,
    *,
    alert_limit: int = 10,
    phase6_path: str | None = None,
) -> dict[str, Any]:
    try:
        with open(status_path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return _invalid_dashboard(alerts_path, alert_limit, phase6_path)
    if not isinstance(status, dict) or not isinstance(status.get("healthy"), bool):
        return _invalid_dashboard(alerts_path, alert_limit, phase6_path)
    health = status.get("health_assessment") or {}
    if not isinstance(health, dict):
        return _invalid_dashboard(alerts_path, alert_limit, phase6_path)
    return {
        "status_valid": True,
        "timestamp": status.get("last_update") or status.get("timestamp"),
        "equity": status.get("equity"),
        "cash": status.get("cash"),
        "positions": status.get("positions") or {},
        "healthy": status.get("healthy"),
        "operational_state": status.get("operational_state"),
        "health_reason_codes": status.get("health_reason_codes") or [],
        "health_reasons": health.get("reasons") or [],
        "recent_alerts": recent_alerts(alerts_path, alert_limit),
        "phase6_monitoring": _load_phase6_monitoring(phase6_path),
    }


def render_text(data: dict[str, Any]) -> str:
    lines = [
        "QuantTrading live operations",
        f"Updated: {data['timestamp'] or '-'}",
        f"Equity: {data['equity']}",
        f"Cash: {data['cash']}",
        f"Health: {'HEALTHY' if data['healthy'] else 'UNHEALTHY'} "
        f"({data['operational_state'] or '-'})",
        "",
        "Positions:",
    ]
    if data["positions"]:
        for symbol, position in data["positions"].items():
            lines.append(
                f"  {symbol}: qty={position.get('qty')} "
                f"avg_price={position.get('avg_price')}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Health reasons:")
    if data["health_reasons"]:
        for reason in data["health_reasons"]:
            lines.append(
                f"  [{reason.get('code')}] {reason.get('subject')}: "
                f"{reason.get('message')}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Recent alerts:")
    if data["recent_alerts"]:
        for alert in data["recent_alerts"]:
            lines.append(
                f"  {alert.get('timestamp', '-')} "
                f"[{str(alert.get('level', '')).upper()}] "
                f"{alert.get('event')}: "
                f"{json.dumps(alert.get('context', {}), sort_keys=True)}"
            )
    else:
        lines.append("  (none)")
    phase6 = data.get("phase6_monitoring")
    lines.append("")
    lines.append("Phase 6 monitoring:")
    if phase6:
        lines.append(f"  Gate: {'PASS' if phase6.get('passed') else 'FAIL'}")
        latest = phase6.get("latest_snapshot") or {}
        for dimension in ("lifecycle", "costs", "drawdown", "regime", "data_quality"):
            section = latest.get(dimension) or {}
            state = "OK" if section.get("ok") is True else "ALERT"
            reason = section.get("reason")
            lines.append(f"  {dimension}: {state}" + (f" ({reason})" if reason else ""))
    else:
        lines.append("  (no Phase 6 evidence report)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live operations dashboard")
    parser.add_argument("--status", default="reports/live_status.json")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--phase6-report", default="reports/phase6/phase6_report.json")
    parser.add_argument("--alert-limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = load_dashboard(
        args.status,
        args.alerts,
        alert_limit=args.alert_limit,
        phase6_path=args.phase6_report,
    )
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else render_text(data))
    return 0 if data["status_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
