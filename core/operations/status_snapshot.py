"""Read-only loading of live status and alert snapshots.

This module is shared by presentation adapters such as the CLI dashboard and
Telegram heartbeat.  Keeping the loader in core prevents operational core code
from depending on a particular dashboard implementation.
"""

from __future__ import annotations

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
