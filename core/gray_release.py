"""Fail-closed R8 small-capital release and rollback controls."""

from __future__ import annotations

import json
import os
import hashlib
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.live_safety import SafetyConfigurationError, StartupSafetyPolicy, verify_live_permissions
from core.sqlite_backup import validate_database
from core.admission_gates import evaluate_phase6


def canonical_bundle_sha256(value: Mapping[str, Any]) -> str:
    """Digest the raw bundle approved by the operator, without report summaries."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read_pinned_admission_evidence(path: str, expected_sha256: str) -> dict:
    """Read one strict JSON snapshot bound to an externally provided byte digest."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SafetyConfigurationError("R8 externally pinned evidence SHA-256 is required")
    try:
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("evidence SHA-256 mismatch")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(_):
            raise ValueError("nonfinite JSON")

        def finite_float(token):
            number = float(token)
            if not math.isfinite(number):
                raise ValueError("nonfinite JSON number")
            return number

        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_constant, parse_float=finite_float)
        if not isinstance(value, dict) or value.get("schema_version") != "r8-admission-evidence/v1":
            raise ValueError("raw r8-admission-evidence/v1 required; summary reports are not admission evidence")
        if set(value) != {"schema_version", "identity", "scope", "approval", "phase6_bundle"}:
            raise ValueError("R8 evidence envelope fields do not match the schema")
        return value
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise SafetyConfigurationError("invalid R8 evidence: " + str(exc)) from exc


def _timestamp(value, name):
    if not isinstance(value, str):
        raise ValueError(name + " requires an aware timestamp")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError(name + " requires an aware timestamp")
    return point.astimezone(timezone.utc)


def _positive(value, name):
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be finite and positive")
    return float(value)


def _check_observation_times(value, cutoff):
    time_fields = {"timestamp", "bar_time", "observed_at", "received_at", "recorded_at",
                   "occurred_at", "available_at", "approved_at", "evaluated_at", "checked_at",
                   "generated_at", "paper_required_start", "paper_required_end"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in time_fields and _timestamp(item, key) > cutoff:
                raise ValueError("evidence observation occurs after operator approval: " + key)
            _check_observation_times(item, cutoff)
    elif isinstance(value, list):
        for item in value:
            _check_observation_times(item, cutoff)


@dataclass(frozen=True)
class GrayReleasePolicy:
    exchange: str
    symbol: str
    max_order_notional: float
    max_daily_new_risk: float
    r7_evidence_path: str
    rollback_snapshot: str
    approval_env: str = "QUANT_R8_APPROVED"
    runtime_identity: Any = None
    evidence_sha256: str | None = None
    source_sha256: str | None = None
    config_sha256: str | None = None
    strategy: str | None = None

    def validate_evidence(self, startup: StartupSafetyPolicy, *, checked_at=None) -> dict:
        """Recompute admission from pinned observations and independently expected identity.

        The pin and operator approval are supplied outside this file. Matching
        bytes and successful arithmetic never certify the truth of observations.
        """
        evidence = read_pinned_admission_evidence(self.r7_evidence_path, self.evidence_sha256)
        try:
            if startup.sandbox or self.runtime_identity is None:
                raise ValueError("live runtime account identity is required")
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (self.source_sha256, self.config_sha256)):
                raise ValueError("current source/configuration identities are required")
            if not isinstance(self.strategy, str) or not self.strategy.strip() or self.strategy == "Cash":
                raise ValueError("exactly one routed non-Cash strategy is required")
            runtime = asdict(self.runtime_identity)
            if (runtime["environment"] != "live" or runtime["exchange"] != startup.exchange_id
                    or runtime["market_type"] != startup.account_type):
                raise ValueError("runtime identity does not match startup policy")
            identity = {"source_sha256": self.source_sha256, "config_sha256": self.config_sha256,
                        "runtime": runtime, "strategy": self.strategy}
            scope = {"exchange": self.exchange, "symbol": self.symbol, "strategy": self.strategy,
                     "base_currency": startup.base_currency,
                     "max_order_notional": _positive(self.max_order_notional, "approved order cap"),
                     "max_daily_new_risk": _positive(self.max_daily_new_risk, "approved daily risk cap")}
            if startup.exchange_id != self.exchange or startup.symbols != (self.symbol,):
                raise ValueError("R8 starts with exactly one approved exchange and symbol")
            if (_positive(startup.max_order_notional, "startup order cap") > scope["max_order_notional"]
                    or _positive(startup.max_daily_new_risk, "startup daily cap") > scope["max_daily_new_risk"]):
                raise ValueError("startup risk caps exceed approved scope")
            for field in ("max_order_notional", "max_daily_new_risk"):
                _positive(evidence["scope"].get(field), "evidence scope " + field)
                _positive(evidence["approval"]["scope"].get(field), "operator scope " + field)
            if evidence["identity"] != identity or evidence["scope"] != scope:
                raise ValueError("R8 evidence identity or scope does not match this run")
            approval, bundle = evidence["approval"], evidence["phase6_bundle"]
            if not isinstance(approval, dict) or not isinstance(bundle, dict):
                raise ValueError("operator approval and raw Phase 6 bundle are required")
            now = checked_at or datetime.now(timezone.utc)
            if now.tzinfo is None:
                raise ValueError("checked_at must be timezone aware")
            approved_at = _timestamp(approval.get("approved_at"), "approved_at")
            expires_at = _timestamp(approval.get("expires_at"), "expires_at")
            if not approved_at <= now < expires_at:
                raise ValueError("operator approval is future-dated or expired")
            if (approval.get("approved") is not True
                    or not isinstance(approval.get("operator"), str) or not approval["operator"].strip()
                    or approval.get("identity") != identity or approval.get("scope") != scope):
                raise ValueError("operator approval is absent or bound to a different run")
            bundle_hash = canonical_bundle_sha256(bundle)
            if approval.get("phase6_bundle_sha256") != bundle_hash:
                raise ValueError("operator approval does not bind the raw Phase 6 bundle")
            signed = bundle.get("admission_approval", {})
            if any(signed.get(key) != approval.get(key) for key in ("approved", "operator", "approved_at")):
                raise ValueError("raw admission approval differs from the pinned operator approval")
            _check_observation_times(bundle, approved_at)
            holdout = bundle.get("holdout_report", {})
            for key, expected in (("source_sha256", self.source_sha256),
                                  ("config_sha256", self.config_sha256), ("strategy", self.strategy)):
                if holdout.get(key) != expected:
                    raise ValueError("holdout decision is not bound to the current candidate: " + key)
            _timestamp(holdout.get("evaluated_at"), "holdout.evaluated_at")
            for field in ("backtest_signals", "shadow_signals"):
                if not isinstance(bundle.get(field), list) or not bundle[field]:
                    raise ValueError("raw signal observations are required")
                for row in bundle[field]:
                    if row.get("strategy") != self.strategy or row.get("symbol") != self.symbol:
                        raise ValueError("raw signal observations contain another strategy or symbol")
            report = evaluate_phase6(bundle)
            if report.get("admission_passed") is not True or report["tasks"]["T-6.6"].get("passed") is not True:
                raise ValueError("raw Phase 6 admission failed: " + ", ".join(report["tasks"]["T-6.6"]["issues"]))
            return {"schema_version": "r8-admission-verification/v1", "passed": True,
                    "identity": identity, "scope": scope, "evidence_sha256": self.evidence_sha256,
                    "phase6_bundle_sha256": bundle_hash, "operator": approval["operator"],
                    "approved_at": approved_at.isoformat(), "expires_at": expires_at.isoformat(),
                    "checked_at": now.isoformat(), "admission": report["tasks"]["T-6.6"],
                    "paper": report["tasks"]["T-6.2"], "independent_source_authentication": "external_responsibility"}
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise SafetyConfigurationError("R8 admission rejected: " + str(exc)) from exc

    def validate(self, startup: StartupSafetyPolicy, exchange_client: Any, *, checked_at=None) -> dict:
        if startup.sandbox:
            raise SafetyConfigurationError("R8 gray release requires explicit live mode")
        if os.getenv(self.approval_env, "").lower() not in {"1", "true", "approved"}:
            raise SafetyConfigurationError("R8 operator approval is absent")
        verification = self.validate_evidence(startup, checked_at=checked_at)
        if startup.exchange_id != self.exchange or startup.symbols != (self.symbol,):
            raise SafetyConfigurationError("R8 starts with exactly one approved exchange and symbol")
        if startup.max_order_notional > self.max_order_notional:
            raise SafetyConfigurationError("startup order limit exceeds R8 approval")
        if startup.max_daily_new_risk > self.max_daily_new_risk:
            raise SafetyConfigurationError("startup daily risk exceeds R8 approval")
        if not Path(self.rollback_snapshot).is_file():
            raise SafetyConfigurationError("validated rollback snapshot is missing")
        if self.runtime_identity is None:
            raise SafetyConfigurationError("rollback snapshot account identity is required")
        try:
            validate_database(self.rollback_snapshot, expected_identity=self.runtime_identity)
        except Exception as exc:
            raise SafetyConfigurationError("rollback snapshot failed integrity, identity or schema validation") from exc
        verify_live_permissions(exchange_client, startup.account_type)
        return verification


class AdmissionBoundOrderGuard:
    """Recheck approval/identity before reserving new risk; retain protective exits."""

    def __init__(self, guard, policy: GrayReleasePolicy, current_identity):
        self._guard, self._admission, self._current_identity = guard, policy, current_identity

    def __getattr__(self, name):
        return getattr(self._guard, name)

    def assert_order_allowed(self, symbol, side, qty, price):
        if side.lower() in {"buy", "short"}:
            expected = {"source_sha256": self._admission.source_sha256,
                        "config_sha256": self._admission.config_sha256, "strategy": self._admission.strategy}
            if self._current_identity() != expected:
                raise SafetyConfigurationError("R8 runtime source/configuration identity changed")
            if os.getenv(self._admission.approval_env, "").lower() not in {"1", "true", "approved"}:
                raise SafetyConfigurationError("R8 operator approval is absent")
            self._admission.validate_evidence(self._guard.policy)
        return self._guard.assert_order_allowed(symbol, side, qty, price)


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
