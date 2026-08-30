"""CLI dashboard for live status, health reasons, and recent alerts."""

from __future__ import annotations

import argparse
import json
from typing import Any

from core.status_snapshot import load_dashboard, recent_alerts


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
