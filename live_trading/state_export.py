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

    def _critical_state_signature(self):
        return (
            self._healthy,
            self._operational_state,
            self._has_unresolved_unknown(),
            tuple(
                self.health_assessment.reason_codes
                if self.health_assessment else ()
            ),
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
            }
            critical_state = (
                state_data["healthy"],
                state_data["operational_state"],
                state_data["unresolved_unknown_order"],
                tuple(state_data["health_reason_codes"]),
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
