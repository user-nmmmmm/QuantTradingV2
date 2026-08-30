import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from config.config import config
from core.domain import SyncResult
from core.live_safety import StartupSafetyPolicy
from core.order_store import OrderStore
from core.persistent_risk_guard import PersistentOrderSafetyGuard
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.sqlite_utils import DatabaseIntegrityError
from core.state_store_v2 import StateStore
from dashboard.__main__ import load_dashboard
from live_trading.engine import LiveTradingEngine


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def sandbox_policy() -> StartupSafetyPolicy:
    return StartupSafetyPolicy(
        sandbox=True,
        exchange_id="binance",
        account_type="spot",
        symbols=("BTC/USDT",),
        allowed_exchanges=("binance",),
        allowed_account_types=("spot",),
        allowed_symbols=("BTC/USDT",),
        base_currency="USDT",
        max_order_notional=1000.0,
        max_daily_new_risk=5000.0,
    )


class StateCorruptionFaultInjectionTests(unittest.TestCase):
    def test_every_live_sqlite_store_fails_closed_on_corrupt_file(self):
        constructors = {
            "engine_state": lambda path: StateStore(path),
            "orders": lambda path: OrderStore(path),
            "daily_risk": lambda path: PersistentOrderSafetyGuard(
                sandbox_policy(), path,
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, constructor in constructors.items():
                with self.subTest(store=name):
                    path = os.path.join(directory, f"{name}.db")
                    Path(path).write_bytes(b"not a sqlite database")
                    with self.assertRaises(
                        (DatabaseIntegrityError, sqlite3.DatabaseError)
                    ):
                        constructor(path)

    def test_malformed_live_status_returns_no_dirty_financial_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "live_status.json"
            alerts = Path(directory) / "live_alerts.jsonl"
            status.write_text('{"healthy": true, "equity": ', encoding="utf-8")

            dashboard = load_dashboard(str(status), str(alerts))

            self.assertFalse(dashboard["status_valid"])
            self.assertFalse(dashboard["healthy"])
            self.assertEqual(dashboard["operational_state"], "RISK_HALTED")
            self.assertEqual(dashboard["health_reason_codes"], ["STATUS_FILE_INVALID"])
            self.assertIsNone(dashboard["equity"])
            self.assertIsNone(dashboard["cash"])
            self.assertEqual(dashboard["positions"], {})


class CircuitBreakerRecoveryFaultInjectionTests(unittest.TestCase):
    @staticmethod
    def _frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [1000.0],
            },
            index=pd.to_datetime(["2026-08-07T00:00:00Z"]),
        )

    def _engine(self, store, risk, now=NOW):
        broker = MagicMock()
        broker.exchange_id = "binance"
        broker.account_id = "sandbox-spot"
        broker.market_type = "spot"
        broker.portfolio = Portfolio(7000.0)
        broker.recover_open_orders.return_value = {}
        broker.has_unresolved_unknown.return_value = False
        broker.sync.return_value = SyncResult(True, now)
        fetcher = MagicMock()
        fetcher.fetch_ccxt.return_value = self._frame()
        engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=broker,
            risk_manager=risk,
            configuration=config,
            data_fetcher=fetcher,
            clock=lambda: now,
            state_file=str(Path(store.path).with_suffix(".json")),
            state_store=store,
            close_grace_seconds=0,
            alert_sink=MagicMock(),
        )
        engine.data_map = {"BTC/USDT": self._frame()}
        return engine

    def test_halt_persists_restart_stays_halted_and_next_day_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live_state.db")
            first_store = StateStore(path)
            first_store.set("daily_start_equity:2026-08-08", 10000.0)
            first_risk = RiskManager(max_drawdown_limit=0.20)
            first = self._engine(first_store, first_risk)

            first._tick()

            self.assertTrue(first_risk.circuit_breaker_triggered)
            self.assertTrue(first_store.get("circuit_breaker"))
            self.assertEqual(first_store.get("circuit_breaker_day"), "2026-08-08")
            self.assertEqual(first._operational_state, "RISK_HALTED")
            first_store.close()

            restarted_store = StateStore(path)
            restarted_risk = RiskManager(max_drawdown_limit=0.20)
            restarted = self._engine(restarted_store, restarted_risk)
            restarted.initialize()

            self.assertTrue(restarted_risk.circuit_breaker_triggered)
            self.assertEqual(restarted._operational_state, "RISK_HALTED")
            self.assertEqual(
                restarted_risk.calculate_position_size(7000, 100, 90), 0.0,
            )

            restarted._reset_daily_risk_if_needed(NOW + timedelta(days=1))

            self.assertFalse(restarted_risk.circuit_breaker_triggered)
            self.assertFalse(restarted_store.get("circuit_breaker"))
            self.assertEqual(
                restarted_store.get("circuit_breaker_day"), "2026-08-09",
            )
            restarted_store.close()

    def test_breaker_trip_alerts_once_not_every_tick(self):
        # Equity moves every tick, which would defeat HysteresisAlertSink's
        # dedup key if it were included in every circuit_breaker_triggered
        # alert context. Only the trip itself (state transition) should
        # page; staying halted must not re-alert on each subsequent tick.
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live_state.db")
            store = StateStore(path)
            store.set("daily_start_equity:2026-08-08", 10000.0)
            risk = RiskManager(max_drawdown_limit=0.20)
            engine = self._engine(store, risk)

            engine._tick()
            engine._tick()
            engine._tick()

            self.assertTrue(risk.circuit_breaker_triggered)
            trip_calls = [
                call for call in engine.alert_sink.notify.call_args_list
                if call.args[1] == "circuit_breaker_triggered"
            ]
            self.assertEqual(len(trip_calls), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
