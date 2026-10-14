"""Order recovery and reconciliation-due checks for the live trading engine.

Split out of live_trading/engine.py (A4) — see docs/architecture_review.md.

``Recovery`` is a collaborator held by ``LiveTradingEngine`` (``_recovery``, B4, ADR 0018): it reads and writes
the shared state through its ``host`` reference and calls back into the engine's own methods, so overrides
and ``patch.object`` on the engine still intercept it.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from live_trading.engine import LiveTradingEngine


def supports_account_reconciliation(broker) -> bool:
    """Inspect the class contract; dynamic Mock attributes are not interfaces."""
    return callable(getattr(type(broker), "reconcile_full_account", None))


def balance_sync_succeeded(broker, result) -> bool:
    """Balance facts can be usable while full-account comparisons block entry."""
    if isinstance(result, bool):
        return result
    return bool(getattr(result, "ok", result is None)) or (
        supports_account_reconciliation(broker)
        and getattr(result, "error", None) == "account_reconciliation_failed"
    )


def account_new_risk_gate(broker, now, *, persistence_failed=False) -> dict:
    """Re-evaluate account evidence freshness at every entry decision."""
    if not supports_account_reconciliation(broker):
        return {"required": False, "allows_new_risk": True,
                "reason": "broker_has_no_full_account_interface"}
    denied = {"required": True, "allows_new_risk": False}
    if persistence_failed:
        return {**denied, "reason": "account_reconciliation_persistence_failed"}
    report = getattr(broker, "account_reconciliation_report", None)
    if not isinstance(report, dict) or any(report.get(key) is not True for key in (
            "ok", "allows_new_risk", "production_account_source_verified")):
        return {**denied, "reason": "independent_account_facts_unverified"}
    if any(not isinstance(report.get(key), list) or report[key] for key in ("issues", "differences")):
        return {**denied, "reason": "account_report_has_unresolved_discrepancies"}
    try:
        age_limit = report["maximum_snapshot_age_seconds"]
        if isinstance(age_limit, bool):
            raise ValueError()
        age_limit = float(age_limit)
        if not math.isfinite(age_limit) or age_limit <= 0:
            raise ValueError()
        source = report["source"]
        if source.get("evidence_kind") != "independent_export":
            return {**denied, "reason": "account_source_is_not_independent"}
        if not source.get("sha256") or not source.get("source_id"):
            raise ValueError()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError()
        for raw in (source["captured_at"], report["checked_at"]):
            timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError()
            if not 0 <= (now - timestamp).total_seconds() <= age_limit:
                return {**denied, "reason": "account_facts_stale_or_future"}
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return {**denied, "reason": "account_facts_freshness_unavailable"}
    return {"required": True, "allows_new_risk": True, "reason": "independent_account_facts_current"}


class Recovery:
    """Non-terminal order recovery and periodic reconciliation.

    Needs from the host: ``broker``, ``reconciliation_interval_seconds``, and ``_alert``
    (from ``LiveTradingEngine`` itself).

    It owns the reconciliation state (B6, ADR 0030); ``LiveTradingEngine`` keeps every name as a ``Delegated``
    attribute. ``_account_reconciliation_persistence_failed`` is bound by the first account reconciliation and is
    deliberately not set in ``__init__``: callers probe it with ``getattr(host, …, False)``.
    """

    _account_reconciliation_persistence_failed: bool

    def __init__(self, host: "LiveTradingEngine") -> None:
        self._host = host
        self._unresolved_unknown_cache: Optional[bool] = None
        self._last_reconciliation_at: Optional[datetime] = None
        self._last_order_sync_at: Optional[datetime] = None
        self._reconciliation_status: Dict[str, Any] = {
            "last_run_at": None,
            "checked_count": 0,
            "discrepancy_count": 0,
            "ok": None,
        }

    def _recover_orders(self) -> Dict:
        host = self._host
        recover = getattr(host.broker, "recover_open_orders", None)
        return recover() if callable(recover) else {}

    def _has_unresolved_unknown(self, *, refresh: bool = False) -> bool:
        host = self._host
        if refresh or self._unresolved_unknown_cache is None:
            checker = getattr(host.broker, "has_unresolved_unknown", None)
            self._unresolved_unknown_cache = (
                bool(checker()) if callable(checker) else False
            )
        return self._unresolved_unknown_cache

    def _run_reconciliation_if_due(
        self, now: datetime, *, force: bool = False,
    ) -> Dict:
        host = self._host
        due = (
            force
            or self._last_reconciliation_at is None
            or (now - self._last_reconciliation_at).total_seconds()
            >= host.reconciliation_interval_seconds
        )
        if not due:
            gate = account_new_risk_gate(host.broker, now,
                persistence_failed=getattr(self, "_account_reconciliation_persistence_failed", False))
            self._reconciliation_status["account_entry_gate"] = gate
            self._reconciliation_status["allows_new_risk"] = (
                self._reconciliation_status.get("ok") is True and gate["allows_new_risk"])
            return dict(self._reconciliation_status)

        recovered = dict(host._recover_orders() or {})
        unresolved = [
            client_order_id
            for client_order_id, result in recovered.items()
            if str(getattr(getattr(result, "status", None), "value", ""))
            == "unknown"
        ]
        account_report = None
        if supports_account_reconciliation(host.broker):
            try:
                account_report = host.broker.reconcile_full_account(checked_at=now)
                if not isinstance(account_report, dict):
                    raise TypeError("account reconciliation must return a report")
            except Exception as exc:
                account_report = {
                    "schema_version": 1, "scope": "independent_normalized_account_snapshots",
                    "checked_at": now.isoformat(), "ok": False, "allows_new_risk": False,
                    "production_account_source_verified": False,
                    "issues": ["account_reconciliation_failed:" + type(exc).__name__],
                    "differences": [], "equity_bridges": {},
                }
            host.broker.account_reconciliation_report = deepcopy(account_report)
            try:
                host._persist_account_reconciliation(account_report, now)
                self._account_reconciliation_persistence_failed = False
            except Exception as exc:
                self._account_reconciliation_persistence_failed = True
                host._alert("error", "account_reconciliation_persistence_failed", {
                    "error": type(exc).__name__,
                })
        gate = account_new_risk_gate(host.broker, now,
            persistence_failed=getattr(self, "_account_reconciliation_persistence_failed", False))
        self._last_reconciliation_at = now
        self._reconciliation_status = {
            "schema_version": 3,
            "scope": "order_and_account_reconciliation" if account_report is not None else "order_recovery",
            "last_run_at": now.isoformat(),
            "checked_count": len(recovered),
            "discrepancy_count": len(unresolved) + (len(account_report.get("issues", []))
                + len(account_report.get("differences", [])) if account_report is not None else 0),
            "ok": not unresolved and (account_report is None or account_report.get("ok") is True),
            "allows_new_risk": not unresolved and gate["allows_new_risk"],
            "account_entry_gate": gate,
            "account_reconciliation": account_report if account_report is not None else {
                "status": "unverified",
                "reason": "independent_opening_capital_cashflow_and_valuation_inputs_required",
            },
        }
        if not host._has_unresolved_unknown(refresh=True):
            self._last_order_sync_at = now
        if unresolved:
            host._alert("error", "reconcile_discrepancy", {
                "discrepancy_count": len(unresolved),
                "client_order_ids": unresolved,
            })
        return dict(self._reconciliation_status)

    def _persist_account_reconciliation(self, report, now):
        """Retain the same normalized report for periodic and daily consumers.

        A daily file is the last actual observation made on that UTC day. It
        does not claim a complete EOD export or backfill missed calendar days.
        Continuous-run acceptance still has to inspect source coverage/time.
        """
        host = self._host
        day = now.astimezone(timezone.utc).date().isoformat()
        folder = Path(host.state_file).resolve().parent / "account_reconciliation"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (day + ".json")
        temporary = folder / ("." + target.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        state_store = getattr(host, "state_store", None)
        if state_store is not None:
            state_store.set_many({"account_reconciliation:last": report,
                                  "account_reconciliation:" + day: report})
