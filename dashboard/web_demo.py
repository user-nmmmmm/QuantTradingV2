"""Clearly labelled, isolated sample data for previewing the web dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def demo_snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    history = [
        97480, 97840, 97570, 98320, 98740, 98560, 99210, 99480,
        99150, 100080, 100420, 100860, 100610, 101240, 101860,
    ]
    return {
        "mode": "demo",
        "server_time": now.isoformat(),
        "status_valid": True,
        "timestamp": now.isoformat(),
        "snapshot_age_seconds": 0,
        "equity": 101860.42,
        "cash": 42180.16,
        "positions": {
            "BTC/USDT": {"qty": 0.42, "avg_price": 68320.50},
            "ETH/USDT": {"qty": 8.75, "avg_price": 3098.20},
            "SOL/USDT": {"qty": 124.0, "avg_price": 31.52},
        },
        "healthy": True,
        "operational_state": "HEALTHY",
        "health_reason_codes": [],
        "health_reasons": [],
        "recent_alerts": [
            {
                "timestamp": (now - timedelta(minutes=41)).isoformat(),
                "level": "info", "event": "reconciliation_complete",
                "context": {"checked_count": 12, "discrepancy_count": 0},
            },
            {
                "timestamp": (now - timedelta(minutes=18)).isoformat(),
                "level": "warning", "event": "market_data_delay",
                "context": {"symbol": "SOL/USDT", "delay_seconds": 42},
            },
            {
                "timestamp": (now - timedelta(minutes=4)).isoformat(),
                "level": "info", "event": "state_exported",
                "context": {"message": "状态快照已更新"},
            },
        ],
        "phase6_monitoring": None,
        "details": {
            "account_entry_gate": {"allows_new_risk": True, "reason": "verified"},
            "reconciliation": {
                "ok": True, "last_run_at": (now - timedelta(minutes=3)).isoformat(),
                "checked_count": 12, "discrepancy_count": 0,
            },
            "portfolio_breaker": {"action": "normal", "last_drawdown": 0.023},
            "unresolved_unknown_order": False,
            "consecutive_strategy_failures": 0,
            "strategy_health": {
                "Trend Breakout": {"status": "ACTIVE", "raw_setup_count": 86},
                "Volatility Reversion": {"status": "ACTIVE", "raw_setup_count": 42},
                "Range Strategy": {"status": "COOLDOWN", "raw_setup_count": 28},
            },
            "protective_orders": {
                "BTC/USDT": {"state": "ACTIVE", "effective_stop": 65200.0},
                "ETH/USDT": {"state": "ACTIVE", "effective_stop": 2910.0},
            },
        },
        "history": [
            {
                "timestamp": (now - timedelta(minutes=(len(history) - index - 1) * 5)).isoformat(),
                "equity": value,
            }
            for index, value in enumerate(history)
        ],
    }
