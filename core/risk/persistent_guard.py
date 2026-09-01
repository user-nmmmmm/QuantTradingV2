"""Restart-safe enforcement of live-trading notional risk limits."""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from threading import RLock
from typing import Callable, Optional

from core.live_safety import SafetyConfigurationError, StartupSafetyPolicy
from core.sqlite_backup import SQLiteSnapshotManager
from core.sqlite_utils import ensure_schema_version, open_durable_connection


class PersistentOrderSafetyGuard:
    """Reserve daily new risk transactionally before an order is submitted."""

    def __init__(
        self,
        policy: StartupSafetyPolicy,
        path: str = "reports/live_safety_state.db",
        *,
        clock: Callable[[], date] = date.today,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._lock = RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._connection = open_durable_connection(path)
        with self._connection:
            ensure_schema_version(self._connection, "persistent_risk_guard", 1)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS daily_risk "
                "(risk_day TEXT PRIMARY KEY, notional REAL NOT NULL)"
            )
        self._snapshot_manager = (
            None if path == ":memory:" else SQLiteSnapshotManager(path)
        )

    def assert_order_allowed(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float],
    ) -> None:
        if self.policy.kill_switch_active():
            raise SafetyConfigurationError("global kill switch is active")
        if symbol not in self.policy.allowed_symbols or symbol not in self.policy.symbols:
            raise SafetyConfigurationError("order symbol is not allowlisted for this run")
        if price is None or price <= 0:
            raise SafetyConfigurationError("a positive reference price is required for safety limits")
        notional = abs(qty * price)
        if notional > self.policy.max_order_notional:
            raise SafetyConfigurationError("order exceeds maximum notional")
        if side.lower() not in {"buy", "short"}:
            return
        self.snapshot_if_due()

        risk_day = self._clock().isoformat()
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT notional FROM daily_risk WHERE risk_day=?", (risk_day,)
            ).fetchone()
            used = 0.0 if current is None else float(current[0])
            if used + notional > self.policy.max_daily_new_risk:
                raise SafetyConfigurationError("order exceeds daily new-risk limit")
            self._connection.execute(
                "INSERT INTO daily_risk(risk_day, notional) VALUES(?, ?) "
                "ON CONFLICT(risk_day) DO UPDATE SET notional=excluded.notional",
                (risk_day, used + notional),
            )

    def snapshot_if_due(self):
        if self._snapshot_manager is None:
            return None
        return self._snapshot_manager.run_if_due()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
