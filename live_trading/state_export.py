"""Durable JSON export of the live engine's operational/critical state.

Split out of live_trading/engine.py (A4) — see docs/architecture_review.md.
``StateExport`` is a collaborator held by ``LiveTradingEngine`` (``_state_export``); see
live_trading/recovery.py for how it reaches the shared state.
"""
from __future__ import annotations

import json
import os
from threading import RLock
from time import sleep
from uuid import uuid4

from core.foundation.logger import get_logger
from live_trading.recovery import account_new_risk_gate
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from live_trading.engine import LiveTradingEngine

# Same logger name as live_trading.engine (logging.getLogger caches by name,
# so this is the identical object) -- tests patch "live_trading.engine.logger"
# and must keep catching exceptions logged from this module too.
logger = get_logger("live_trading.engine")
_REPLACE_LOCK = RLock()


def _replace_snapshot(source, target):
    # Windows can briefly deny replacement while another writer/reader closes
    # the destination. Serialize local replacements and bound external retries.
    with _REPLACE_LOCK:
        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                sleep(0.01 * (attempt + 1))


class StateExport:
    """Atomic (temp-file + os.replace) state export with fsync-on-transition.

    Needs from the host: ``_snapshot``, ``_healthy``,
    ``_operational_state``, ``health_assessment``, ``_reconciliation_status``,
    ``_consecutive_strategy_failures``, ``_last_strategy_error``, ``broker``,
    ``symbols``, ``data_map``, ``state_file``, ``_tick_count``, ``_now()``, and
    ``_has_unresolved_unknown()`` (from ``Recovery``).

    It owns the export bookkeeping (B6, ADR 0030): the tick of the last export and the critical state it wrote.
    ``LiveTradingEngine`` keeps both names as ``Delegated`` attributes.
    """

    def __init__(self, host: "LiveTradingEngine") -> None:
        self._host = host
        self._last_export_tick: Optional[int] = None
        self._last_exported_critical_state: Optional[tuple] = None

    def _strategy_health(self):
        """SR1-4: per-strategy lifecycle state, exported like any other fact."""
        host = self._host
        health = {}
        for name, strategy in getattr(host, "strategies", {}).items():
            snapshot = getattr(strategy, "health_snapshot", None)
            if callable(snapshot):
                value = snapshot()
                if value:
                    health[name] = value
        return health

    def _protective_order_state(self):
        """Latest protective-order state per symbol (SR2-5 audit surface)."""
        host = self._host
        manager = getattr(host, "_protective_order_manager", None)
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
        host = self._host
        account_gate = account_new_risk_gate(host.broker, host._now(),
            persistence_failed=getattr(host, "_account_reconciliation_persistence_failed", False))
        return (
            host._healthy,
            host._operational_state,
            host._has_unresolved_unknown(),
            tuple(
                host.health_assessment.reason_codes
                if host.health_assessment else ()
            ),
            # A health transition (ACTIVE -> COOLDOWN -> ...) is critical
            # state: it must force an fsync'd export, not wait for the next
            # periodic tick.
            tuple(sorted(
                (name, entry.get("status"))
                for name, entry in host._strategy_health().items()
            )),
            host.risk_manager.breaker_action.value,
            host.risk_manager.current_transition_id,
            host.risk_manager.blocked_until,
            host.risk_manager.daily_loss_triggered,
            account_gate["allows_new_risk"],
            account_gate["reason"],
        )

    def _maybe_export_state(self, *, force: bool = False) -> bool:
        host = self._host
        critical_state = host._critical_state_signature()
        transition = critical_state != self._last_exported_critical_state
        due = (
            self._last_export_tick is None
            or host._tick_count - self._last_export_tick
            >= host.state_export_interval_ticks
        )
        if not (force or transition or due):
            return False
        if host._export_state():
            self._last_export_tick = host._tick_count
            self._last_exported_critical_state = critical_state
            return True
        return False

    def _export_state(self):
        host = self._host
        tmp_path = f"{host.state_file}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            if host._snapshot is not None:
                # Reuse the authoritative valuation already produced this
                # tick instead of re-deriving equity from raw bar closes,
                # which can disagree with the price rule risk decisions used.
                cash = host._snapshot.cash
                equity = host._snapshot.equity
            else:
                current_prices = {
                    symbol: frame["close"].iloc[-1]
                    for symbol, frame in host.data_map.items()
                    if not frame.empty
                }
                cash = host.broker.portfolio.cash
                equity = host.broker.portfolio.get_equity(current_prices)
            state_data = {
                "schema_version": 1,
                "timestamp": host._now().strftime("%Y-%m-%d %H:%M:%S"),
                "cash": cash,
                "equity": equity,
                "positions": host.broker.portfolio.positions,
                "capital_allocation": dict(getattr(
                    getattr(getattr(host, "router", None), "allocator", None),
                    "last_capital_summary", {"enabled": False})),
                "symbols": host.symbols,
                "last_update": host._now().isoformat(),
                "healthy": host._healthy,
                "operational_state": host._operational_state,
                "unresolved_unknown_order": host._has_unresolved_unknown(),
                "reconciliation": dict(host._reconciliation_status),
                "account_entry_gate": account_new_risk_gate(host.broker, host._now(),
                    persistence_failed=getattr(host, "_account_reconciliation_persistence_failed", False)),
                "consecutive_strategy_failures": host._consecutive_strategy_failures,
                "last_strategy_error": host._last_strategy_error,
                "performance": {
                    "last_tick_seconds": getattr(host, "_last_tick_seconds", None),
                    "poll_interval_seconds": host.interval,
                    "market_data": dict(host.market_data_adapter.refresh_metrics),
                    "runtime_controls_enabled": bool(getattr(getattr(host, "runtime_controls", None), "policy", None)
                        and host.runtime_controls.policy.enabled),
                },
                "temporal_data": {
                    "audit": getattr(host.market_data_adapter, "temporal_audit", {}),
                    "identity": getattr(host.market_data_adapter, "temporal_identity", {})},
                "execution_quote_observation": (host.quote_sampler.health()
                    if getattr(host, "quote_sampler", None) is not None else {"status": "disabled"}),
                "health_reason_codes": (
                    host.health_assessment.reason_codes
                    if host.health_assessment else []
                ),
                "health_assessment": (
                    host.health_assessment.to_dict()
                    if host.health_assessment else None
                ),
                "strategy_health": host._strategy_health(),
                "portfolio_breaker": host.risk_manager.breaker_checkpoint(),
                # SR2-5: what protection currently exists, per symbol.
                "protective_orders": host._protective_order_state(),
                "external_position_actions": (
                    host.state_store.get("external_position_actions", {})
                    if getattr(host, "state_store", None) is not None else {}
                ),
                "fill_risk_audit": list(
                    getattr(host, "_live_fill_risk_audit", [])
                ),
                "order_request_observations": (
                    host.broker.order_latency.summary()
                    if getattr(host.broker, "order_latency", None) is not None else None
                ),
            }
            critical_state = host._critical_state_signature()
            needs_fsync = critical_state != self._last_exported_critical_state
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state_data, handle, indent=2)
                handle.flush()
                if needs_fsync:
                    os.fsync(handle.fileno())
            _replace_snapshot(tmp_path, host.state_file)
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
