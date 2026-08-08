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
                alerts.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    return list(alerts)


def load_dashboard(
    status_path: str, alerts_path: str, *, alert_limit: int = 10,
) -> dict[str, Any]:
    with open(status_path, "r", encoding="utf-8") as handle:
        status = json.load(handle)
    health = status.get("health_assessment") or {}
    return {
        "timestamp": status.get("last_update") or status.get("timestamp"),
        "equity": status.get("equity"),
        "cash": status.get("cash"),
        "positions": status.get("positions") or {},
        "healthy": status.get("healthy"),
        "operational_state": status.get("operational_state"),
        "health_reason_codes": status.get("health_reason_codes") or [],
        "health_reasons": health.get("reasons") or [],
        "recent_alerts": recent_alerts(alerts_path, alert_limit),
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
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live operations dashboard")
    parser.add_argument("--status", default="reports/live_status.json")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--alert-limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = load_dashboard(
        args.status, args.alerts, alert_limit=args.alert_limit,
    )
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else render_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
