"""Order recovery and reconciliation-due checks for the live trading engine.

Split out of live_trading/engine.py (A4) — see docs/architecture_review.md.

This is a mixin, not a standalone collaborator object: ``LiveTradingEngine``
combines ``RecoveryMixin``, ``TickOrchestratorMixin``, and
``StateExportMixin`` via inheritance so every method still reads/writes the
same ``self`` attributes it always has. That keeps the split mechanical and
behavior-identical.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict


class RecoveryMixin:
    """Non-terminal order recovery and periodic reconciliation.

    Expects ``self`` to carry ``broker``, ``_unresolved_unknown_cache``,
    ``_last_reconciliation_at``, ``reconciliation_interval_seconds``,
    ``_reconciliation_status``, ``_last_order_sync_at``, and ``_alert``
    (from ``LiveTradingEngine`` itself).
    """

    def _recover_orders(self) -> Dict:
        recover = getattr(self.broker, "recover_open_orders", None)
        return recover() if callable(recover) else {}

    def _has_unresolved_unknown(self, *, refresh: bool = False) -> bool:
        if refresh or self._unresolved_unknown_cache is None:
            checker = getattr(self.broker, "has_unresolved_unknown", None)
            self._unresolved_unknown_cache = (
                bool(checker()) if callable(checker) else False
            )
        return self._unresolved_unknown_cache

    def _run_reconciliation_if_due(
        self, now: datetime, *, force: bool = False,
    ) -> Dict:
        due = (
            force
            or self._last_reconciliation_at is None
            or (now - self._last_reconciliation_at).total_seconds()
            >= self.reconciliation_interval_seconds
        )
        if not due:
            return dict(self._reconciliation_status)

        recovered = dict(self._recover_orders() or {})
        unresolved = [
            client_order_id
            for client_order_id, result in recovered.items()
            if str(getattr(getattr(result, "status", None), "value", ""))
            == "unknown"
        ]
        self._last_reconciliation_at = now
        self._reconciliation_status = {
            "last_run_at": now.isoformat(),
            "checked_count": len(recovered),
            "discrepancy_count": len(unresolved),
            "ok": not unresolved,
        }
        if not self._has_unresolved_unknown(refresh=True):
            self._last_order_sync_at = now
        if unresolved:
            self._alert("error", "reconcile_discrepancy", {
                "discrepancy_count": len(unresolved),
                "client_order_ids": unresolved,
            })
        return dict(self._reconciliation_status)
