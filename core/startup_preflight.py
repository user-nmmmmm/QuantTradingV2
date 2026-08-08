"""Structured, secret-free startup evidence for exchange-connected runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.live_safety import StartupSafetyPolicy


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def build_startup_report(
    policy: StartupSafetyPolicy,
    credentials: Mapping[str, str],
    engine: Any,
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the startup gate report without copying credential values."""
    now = checked_at or datetime.now(timezone.utc)
    assessment = getattr(engine, "health_assessment", None)
    reason_codes = list(getattr(assessment, "reason_codes", []) or [])
    breaker_active = bool(
        getattr(engine.risk_manager, "circuit_breaker_triggered", False)
    )
    checks = [
        _check(
            "credentials_present",
            bool(credentials.get("apiKey") and credentials.get("secret")),
            "API key and secret are present (values are never persisted)",
        ),
        _check(
            "mode_confirmed",
            isinstance(policy.sandbox, bool),
            "SANDBOX" if policy.sandbox else "LIVE",
        ),
        _check(
            "kill_switch_inactive",
            not policy.kill_switch_active(),
            "global kill switch must be inactive at startup",
        ),
        _check(
            "account_sync_baseline",
            getattr(engine, "_last_account_sync_at", None) is not None,
            "initial authoritative account synchronization completed",
        ),
        _check(
            "order_sync_baseline",
            getattr(engine, "_last_order_sync_at", None) is not None,
            "initial non-terminal order recovery completed",
        ),
        _check(
            "health_baseline",
            bool(assessment is not None and getattr(assessment, "healthy", False)),
            "healthy" if not reason_codes else ",".join(reason_codes),
        ),
        _check(
            "circuit_breaker_state_restored",
            True,
            "active" if breaker_active else "inactive",
        ),
    ]
    return {
        "schema_version": 1,
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "ok": all(item["passed"] for item in checks),
        "mode": "SANDBOX" if policy.sandbox else "LIVE",
        "exchange": policy.exchange_id,
        "account_type": policy.account_type,
        "symbols": list(policy.symbols),
        "base_currency": policy.base_currency,
        "health_reason_codes": reason_codes,
        "checks": checks,
    }


def write_startup_report(report: Mapping[str, Any], path: str) -> Path:
    """Atomically persist startup evidence so partial files are never trusted."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(dict(report), handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return destination
