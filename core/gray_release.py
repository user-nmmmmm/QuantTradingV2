"""Fail-closed R8 small-capital release and rollback controls."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.live_safety import SafetyConfigurationError, StartupSafetyPolicy, verify_live_permissions


@dataclass(frozen=True)
class GrayReleasePolicy:
    exchange: str
    symbol: str
    max_order_notional: float
    max_daily_new_risk: float
    r7_evidence_path: str
    rollback_snapshot: str
    approval_env: str = "QUANT_R8_APPROVED"

    def validate(self, startup: StartupSafetyPolicy, exchange_client: Any) -> None:
        if startup.sandbox:
            raise SafetyConfigurationError("R8 gray release requires explicit live mode")
        if os.getenv(self.approval_env, "").lower() not in {"1", "true", "approved"}:
            raise SafetyConfigurationError("R8 operator approval is absent")
        evidence = json.loads(Path(self.r7_evidence_path).read_text(encoding="utf-8"))
        if not evidence.get("passed"):
            raise SafetyConfigurationError("R7 acceptance evidence did not pass")
        if startup.exchange_id != self.exchange or startup.symbols != (self.symbol,):
            raise SafetyConfigurationError("R8 starts with exactly one approved exchange and symbol")
        if startup.max_order_notional > self.max_order_notional:
            raise SafetyConfigurationError("startup order limit exceeds R8 approval")
        if startup.max_daily_new_risk > self.max_daily_new_risk:
            raise SafetyConfigurationError("startup daily risk exceeds R8 approval")
        if not Path(self.rollback_snapshot).is_file():
            raise SafetyConfigurationError("validated rollback snapshot is missing")
        verify_live_permissions(exchange_client, startup.account_type)


def write_release_record(path: str, policy: GrayReleasePolicy, evidence: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": asdict(policy),
        "evidence": dict(evidence),
    }
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temp, target)
