import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.alerting import JsonlAlertSink
from core.broker import Broker, Order, OrderType
from core.domain import OrderStatus, SyncResult
from core.live_broker import LiveBroker
from core.persistent_risk_guard import PersistentOrderSafetyGuard
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.sqlite_utils import DatabaseIntegrityError
from core.state_store_v2 import StateStore
from live_trading.engine import LiveTradingEngine


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class TestP0Blockers(unittest.TestCase):
    def make_live_broker(self, directory, exchange, **kwargs):
        store = OrderStore(os.path.join(directory, "orders.db"))
        broker = LiveBroker(
            Portfolio(), order_store=store, clock=lambda: NOW,
            retry_base_delay=0, retry_sleep_fn=lambda _delay: None,
            alert_sink=kwargs.pop("alert_sink", MagicMock()), **kwargs,
        )
        broker.exchange = exchange
        broker.set_bar_context("1m", NOW)
        return broker

    @patch("core.live_broker.ccxt")
    def test_submit_network_blip_marks_unknown_then_self_heals_via_reconcile(self, ccxt_mock):
        # create_order is a non-idempotent write: a lost response after a
        # successful submission is indistinguishable from a lost request, so
        # a timeout there must never trigger an automatic resubmission (that
        # could place a duplicate live order). It self-heals only through
        # the idempotent fetch/reconcile path, on a later poll.
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError("network timeout")
        exchange.fetch_order_by_client_order_id.return_value = {
            "id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(directory, exchange)
            result = broker.submit_order("BTC/USDT", "buy", 1, 100)
            self.assertEqual(result.status, OrderStatus.UNKNOWN)
            exchange.create_order.assert_called_once()

            recovered = broker.recover_open_orders()[result.client_order_id]
            self.assertEqual(recovered.status, OrderStatus.ACCEPTED)
            self.assertFalse(broker.has_unresolved_unknown())
            exchange.create_order.assert_called_once()
            broker.close()

    @patch("core.live_broker.ccxt")
    def test_unknown_is_reconciled_by_later_poll(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError("lost response")
        exchange.fetch_order_by_client_order_id.side_effect = [
            TimeoutError("query unavailable"),
            {"id": "e1", "status": "closed", "amount": 1, "filled": 1,
             "remaining": 0, "average": 100},
        ]
        exchange.fetch_balance.return_value = {
            "free": {"USDT": 9900}, "total": {"USDT": 9900}
        }
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange, retry_max_attempts=1,
            )
            unknown = broker.submit_order("BTC/USDT", "buy", 1, 100)
            self.assertEqual(unknown.status, OrderStatus.UNKNOWN)
            broker.recover_open_orders()
            self.assertTrue(broker.has_unresolved_unknown())
            recovered = broker.recover_open_orders()[unknown.client_order_id]
            self.assertEqual(recovered.status, OrderStatus.FILLED)
            self.assertFalse(broker.has_unresolved_unknown())
            broker.close()

    def test_update_data_exception_marks_tick_unhealthy_and_alerts(self):
        broker = MagicMock()
        broker.exchange_id = "binance"
        broker.account_id = "spot"
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.recover_open_orders.return_value = {}
        broker.has_unresolved_unknown.return_value = False
        broker.sync.return_value = SyncResult(True, NOW)
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.db"))
            engine = LiveTradingEngine(
                symbols=["BTC/USDT"], strategies={}, broker=broker,
                risk_manager=RiskManager(), clock=lambda: NOW,
                state_file=os.path.join(directory, "status.json"),
                state_store=store, alert_sink=alerts,
            )
            engine._update_data = MagicMock(side_effect=TimeoutError("data"))
            engine._tick()
            self.assertFalse(engine._healthy)
            self.assertIn("MARKET_DATA_UPDATE_FAILED", engine.health_assessment.reason_codes)
            broker.sync.assert_not_called()
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertIn("tick_unhealthy", events)
            store.close()

    def test_halt_alert_has_durable_jsonl_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "alerts.jsonl")
            JsonlAlertSink(path).notify("critical", "risk_halt", {"reason": "test"})
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.readline())
            self.assertEqual(record["event"], "risk_halt")
            self.assertEqual(record["context"]["reason"], "test")
            self.assertIn("timestamp", record)

    def test_state_export_is_atomic_and_versioned(self):
        broker = MagicMock()
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.has_unresolved_unknown.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "status.json")
            store = StateStore(os.path.join(directory, "state.db"))
            engine = LiveTradingEngine(
                symbols=[], strategies={}, broker=broker,
                risk_manager=RiskManager(), clock=lambda: NOW,
                state_file=path, state_store=store, alert_sink=MagicMock(),
            )
            engine._export_state()
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["schema_version"], 1)
            self.assertFalse(os.path.exists(f"{path}.{os.getpid()}.tmp"))
            store.close()

    def test_limit_fills_never_cross_limit_after_slippage(self):
        broker = Broker(Portfolio(), slippage=0.10, commission_rate=0)
        buy = Order("BTC/USDT", "buy", 1, OrderType.LIMIT, price=100)
        buy_fill = broker._execute_trade(buy, 100, NOW, 1)
        sell = Order("BTC/USDT", "short", 1, OrderType.LIMIT, price=100)
        sell_fill = broker._execute_trade(sell, 100, NOW, 1)
        self.assertLessEqual(buy_fill["fill_price"], 100)
        self.assertGreaterEqual(sell_fill["fill_price"], 100)

    def test_sqlite_stores_enable_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            stores = [
                StateStore(os.path.join(directory, "state.db")),
                OrderStore(os.path.join(directory, "orders.db")),
                PersistentOrderSafetyGuard(MagicMock(), os.path.join(directory, "risk.db")),
            ]
            for store in stores:
                connection = store._connection
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertGreaterEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000
                )
                store.close()

    def test_corrupt_database_fails_closed_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "corrupt.db")
            with open(path, "wb") as handle:
                handle.write(b"not a sqlite database")
            with self.assertRaises((DatabaseIntegrityError, sqlite3.DatabaseError)):
                StateStore(path)


if __name__ == "__main__":
    unittest.main()
