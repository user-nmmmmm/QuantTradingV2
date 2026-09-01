"""Durable JSON export of the live engine's operational/critical state.

Split out of live_trading/engine.py (A4) — see docs/architecture_review.md.
See live_trading/recovery.py's module docstring for why this is a mixin
rather than a standalone collaborator object.
"""
from __future__ import annotations

import json
import os

from core.logger import get_logger

# Same logger name as live_trading.engine (logging.getLogger caches by name,
# so this is the identical object) -- tests patch "live_trading.engine.logger"
# and must keep catching exceptions logged from this mixin too.
logger = get_logger("live_trading.engine")


class StateExportMixin:
    """Atomic (temp-file + os.replace) state export with fsync-on-transition.

    Expects ``self`` to carry ``_snapshot``, ``_healthy``,
    ``_operational_state``, ``health_assessment``, ``_reconciliation_status``,
    ``_consecutive_strategy_failures``, ``_last_strategy_error``, ``broker``,
    ``symbols``, ``data_map``, ``state_file``, ``_tick_count``,
    ``_last_export_tick``, ``_last_exported_critical_state``, ``_now()``, and
    ``_has_unresolved_unknown()`` (from ``RecoveryMixin``).
    """

    def _strategy_health(self):
        """SR1-4: per-strategy lifecycle state, exported like any other fact."""
        health = {}
        for name, strategy in getattr(self, "strategies", {}).items():
            snapshot = getattr(strategy, "health_snapshot", None)
            if callable(snapshot):
                value = snapshot()
                if value:
                    health[name] = value
        return health

    def _protective_order_state(self):
        """Latest protective-order state per symbol (SR2-5 audit surface)."""
        manager = getattr(self, "_protective_order_manager", None)
        if manager is None:
            return {}
        latest = {}
        for row in manager.audit:
            latest[row["symbol"]] = {
                "state": row.get("state"),
                "effective_stop": row.get("effective_stop"),
                "protected_qty": row.get("protected_qty"),
                "last_action": row.get("action"),
                "last_reason": row.get("reason"),
            }
        return latest

    def _critical_state_signature(self):
        return (
            self._healthy,
            self._operational_state,
            self._has_unresolved_unknown(),
            tuple(
                self.health_assessment.reason_codes
                if self.health_assessment else ()
            ),
            # A health transition (ACTIVE -> COOLDOWN -> ...) is critical
            # state: it must force an fsync'd export, not wait for the next
            # periodic tick.
            tuple(sorted(
                (name, entry.get("status"))
                for name, entry in self._strategy_health().items()
            )),
        )

    def _maybe_export_state(self, *, force: bool = False) -> bool:
        critical_state = self._critical_state_signature()
        transition = critical_state != self._last_exported_critical_state
        due = (
            self._last_export_tick is None
            or self._tick_count - self._last_export_tick
            >= self.state_export_interval_ticks
        )
        if not (force or transition or due):
            return False
        if self._export_state():
            self._last_export_tick = self._tick_count
            self._last_exported_critical_state = critical_state
            return True
        return False

    def _export_state(self):
        tmp_path = f"{self.state_file}.{os.getpid()}.tmp"
        try:
            if self._snapshot is not None:
                # Reuse the authoritative valuation already produced this
                # tick instead of re-deriving equity from raw bar closes,
                # which can disagree with the price rule risk decisions used.
                cash = self._snapshot.cash
                equity = self._snapshot.equity
            else:
                current_prices = {
                    symbol: frame["close"].iloc[-1]
                    for symbol, frame in self.data_map.items()
                    if not frame.empty
                }
                cash = self.broker.portfolio.cash
                equity = self.broker.portfolio.get_equity(current_prices)
            state_data = {
                "schema_version": 1,
                "timestamp": self._now().strftime("%Y-%m-%d %H:%M:%S"),
                "cash": cash,
                "equity": equity,
                "positions": self.broker.portfolio.positions,
                "symbols": self.symbols,
                "last_update": self._now().isoformat(),
                "healthy": self._healthy,
                "operational_state": self._operational_state,
                "unresolved_unknown_order": self._has_unresolved_unknown(),
                "reconciliation": dict(self._reconciliation_status),
                "consecutive_strategy_failures": self._consecutive_strategy_failures,
                "last_strategy_error": self._last_strategy_error,
                "health_reason_codes": (
                    self.health_assessment.reason_codes
                    if self.health_assessment else []
                ),
                "health_assessment": (
                    self.health_assessment.to_dict()
                    if self.health_assessment else None
                ),
                "strategy_health": self._strategy_health(),
                # SR2-5: what protection currently exists, per symbol.
                "protective_orders": self._protective_order_state(),
                "fill_risk_audit": list(
                    getattr(self, "_live_fill_risk_audit", [])
                ),
            }
            critical_state = (
                state_data["healthy"],
                state_data["operational_state"],
                state_data["unresolved_unknown_order"],
                tuple(state_data["health_reason_codes"]),
                tuple(sorted(
                    (name, entry.get("status"))
                    for name, entry in state_data["strategy_health"].items()
                )),
            )
            needs_fsync = critical_state != self._last_exported_critical_state
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state_data, handle, indent=2)
                handle.flush()
                if needs_fsync:
                    os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_file)
            self._last_exported_critical_state = critical_state
            return True
        except Exception:
            logger.exception("Failed to export state")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                logger.warning("Failed to remove incomplete state file: %s", tmp_path)
            return False
