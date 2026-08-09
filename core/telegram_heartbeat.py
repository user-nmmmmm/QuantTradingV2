"""Periodic (not event-triggered) status summary posted to Telegram.

core/alerting.py's TelegramAlertSink fires on operational events (halts,
breaker trips). This is the complementary "still alive and here's the
state" report meant to be invoked on a schedule (cron/systemd timer),
independent of whether anything noteworthy happened.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict

from core.alerting import send_telegram_message
from dashboard.__main__ import load_dashboard


def build_heartbeat_message(dashboard: Dict[str, Any]) -> str:
    if not dashboard.get("status_valid"):
        return (
            "QuantTrading heartbeat\n"
            "STATUS FILE INVALID — no financial facts available.\n"
            "Treat as RISK_HALTED until the status snapshot recovers."
        )
    lines = [
        "QuantTrading heartbeat",
        f"Updated: {dashboard.get('timestamp') or '-'}",
        f"Health: {'HEALTHY' if dashboard.get('healthy') else 'UNHEALTHY'} "
        f"({dashboard.get('operational_state') or '-'})",
        f"Equity: {dashboard.get('equity')}",
        f"Cash: {dashboard.get('cash')}",
    ]
    positions = dashboard.get("positions") or {}
    if positions:
        lines.append("Positions:")
        for symbol, position in positions.items():
            lines.append(f"  {symbol}: qty={position.get('qty')}")
    else:
        lines.append("Positions: (none)")
    reason_codes = dashboard.get("health_reason_codes") or []
    if reason_codes:
        lines.append(f"Health reasons: {', '.join(reason_codes)}")
    recent_alerts = dashboard.get("recent_alerts") or []
    if recent_alerts:
        lines.append(f"Recent alerts ({len(recent_alerts)}):")
        for alert in recent_alerts[-5:]:
            lines.append(f"  [{alert.get('level', '?').upper()}] {alert.get('event')}")
    return "\n".join(lines)


def send_heartbeat(
    status_path: str, alerts_path: str, bot_token: str, chat_id: str,
    *, alert_limit: int = 10,
) -> Dict[str, Any]:
    dashboard = load_dashboard(status_path, alerts_path, alert_limit=alert_limit)
    send_telegram_message(bot_token, chat_id, build_heartbeat_message(dashboard))
    return dashboard


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post a periodic live-status summary to Telegram"
    )
    parser.add_argument("--status", default="reports/live_status.json")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--alert-limit", type=int, default=10)
    args = parser.parse_args()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        parser.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment")

    dashboard = send_heartbeat(
        args.status, args.alerts, bot_token, chat_id, alert_limit=args.alert_limit,
    )
    print(json.dumps({"sent": True, "status_valid": dashboard["status_valid"]}))
    return 0 if dashboard["status_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
