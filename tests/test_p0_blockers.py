import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from config.config import config
from core.alerting import JsonlAlertSink
from core.accounts import AccountMode
from core.broker import Broker, Order, OrderType
from core.domain import OrderErrorCode, OrderStatus, SyncResult
from core.live_broker import LiveBroker
from core.risk.persistent_guard import PersistentOrderSafetyGuard
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
                risk_manager=RiskManager(), configuration=config, clock=lambda: NOW,
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

    @patch('core.live_broker.ccxt')
    def test_unknown_can_be_manually_resolved_with_durable_audit(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError('lost response')
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange, retry_max_attempts=1, alert_sink=alerts,
            )
            unknown = broker.submit_order('BTC/USDT', 'buy', 1, 100)
            self.assertEqual(unknown.status, OrderStatus.UNKNOWN)
            self.assertTrue(broker.has_unresolved_unknown())

            resolved = broker.confirm_order_not_submitted(
                unknown.client_order_id,
                confirmed_by='operator@example.com',
                reason='verified absent in exchange order history',
            )

            self.assertEqual(resolved.status, OrderStatus.EXPIRED_UNSUBMITTED)
            self.assertFalse(broker.has_unresolved_unknown())
            audit = broker.order_store.resolutions_for(unknown.client_order_id)
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]['previous_status'], OrderStatus.UNKNOWN.value)
            self.assertEqual(audit[0]['confirmed_by'], 'operator@example.com')
            self.assertEqual(
                audit[0]['resolution'], OrderStatus.EXPIRED_UNSUBMITTED.value,
            )
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertIn('unknown_order_manually_resolved', events)
            broker.close()

            reopened = OrderStore(os.path.join(directory, 'orders.db'))
            self.assertEqual(
                reopened.resolutions_for(unknown.client_order_id)[0]['reason'],
                'verified absent in exchange order history',
            )
            reopened.close()

    @patch('core.live_broker.ccxt')
    def test_stale_unattempted_submitting_expires_without_exchange_call(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory,
                exchange,
                submitting_ttl=timedelta(minutes=5),
                alert_sink=alerts,
            )
            intent = broker._build_intent(
                'BTC/USDT', 'buy', 1, 100, 'market', NOW, 'S',
                None, None, None, 0,
            )
            broker.order_store.create_intent(
                intent, (NOW - timedelta(minutes=6)).isoformat(),
            )

            recovered = broker.recover_open_orders()[intent.client_order_id]

            self.assertEqual(recovered.status, OrderStatus.EXPIRED_UNSUBMITTED)
            exchange.create_order.assert_not_called()
            self.assertEqual(broker.order_store.list_non_terminal(), [])
            audit = broker.order_store.resolutions_for(intent.client_order_id)
            self.assertEqual(audit[0]['confirmed_by'], 'system:submitting_ttl')
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertIn('stale_submitting_expired', events)
            broker.close()

    @patch('core.live_broker.ccxt')
    def test_confirmed_absent_unknown_expires_after_ttl(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError('lost response')
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange,
                submitting_ttl=timedelta(minutes=5), alert_sink=alerts,
            )
            unknown = broker.submit_order('BTC/USDT', 'buy', 1, 100)
            self.assertEqual(unknown.status, OrderStatus.UNKNOWN)

            # Simulate a prior poll that independently confirmed the
            # exchange has no record of this order, over 5 minutes ago.
            # updated_at is deliberately backdated (not "now") to model the
            # TTL clock starting when the order first went UNKNOWN, not
            # resetting on every reconfirmation poll.
            broker.order_store.update(
                unknown.client_order_id,
                error_code=OrderErrorCode.UNKNOWN.value,
                error_message='order_not_found_by_client_id',
                updated_at=(NOW - timedelta(minutes=6)).isoformat(),
            )

            recovered = broker.recover_open_orders()[unknown.client_order_id]

            self.assertEqual(recovered.status, OrderStatus.EXPIRED_UNSUBMITTED)
            self.assertFalse(broker.has_unresolved_unknown())
            audit = broker.order_store.resolutions_for(unknown.client_order_id)
            self.assertEqual(audit[-1]['confirmed_by'], 'system:unknown_ttl')
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertIn('stale_unknown_confirmed_absent_expired', events)
            broker.close()

    @patch('core.live_broker.ccxt')
    def test_confirmed_absent_unknown_within_ttl_does_not_expire(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError('lost response')
        exchange.fetch_order_by_client_order_id.return_value = None
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange,
                submitting_ttl=timedelta(minutes=5), alert_sink=alerts,
            )
            unknown = broker.submit_order('BTC/USDT', 'buy', 1, 100)
            self.assertEqual(unknown.status, OrderStatus.UNKNOWN)

            # A poll within the TTL window confirms the order absent but
            # must not auto-expire it yet.
            recovered = broker.recover_open_orders()[unknown.client_order_id]

            self.assertEqual(recovered.status, OrderStatus.UNKNOWN)
            self.assertTrue(broker.has_unresolved_unknown())
            record = broker.order_store.get(unknown.client_order_id)
            self.assertEqual(record['error_message'], 'order_not_found_by_client_id')
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertNotIn('stale_unknown_confirmed_absent_expired', events)
            broker.close()

    @patch('core.live_broker.ccxt')
    def test_transient_error_unknown_never_auto_expires(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        exchange.create_order.side_effect = TimeoutError('lost response')
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange,
                submitting_ttl=timedelta(minutes=5), alert_sink=alerts,
            )
            unknown = broker.submit_order('BTC/USDT', 'buy', 1, 100)
            self.assertEqual(unknown.status, OrderStatus.UNKNOWN)

            # error_message is still the original transient-error label
            # (never independently confirmed absent), even though it is
            # long past the TTL — this must require manual resolution.
            broker.order_store.update(
                unknown.client_order_id,
                updated_at=(NOW - timedelta(minutes=6)).isoformat(),
            )
            exchange.fetch_order_by_client_order_id.side_effect = TimeoutError('still down')

            recovered = broker.recover_open_orders()[unknown.client_order_id]

            self.assertEqual(recovered.status, OrderStatus.UNKNOWN)
            self.assertTrue(broker.has_unresolved_unknown())
            events = [call.args[1] for call in alerts.notify.call_args_list]
            self.assertNotIn('stale_unknown_confirmed_absent_expired', events)
            broker.close()

    def test_outer_tick_guard_alerts_and_applies_bounded_exponential_backoff(self):
        broker = MagicMock()
        broker.exchange_id = 'binance'
        broker.account_id = 'spot'
        broker.market_type = 'spot'
        broker.portfolio = Portfolio()
        broker.has_unresolved_unknown.return_value = False
        alerts = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, 'state.db'))
            engine = LiveTradingEngine(
                symbols=[], strategies={}, broker=broker,
                risk_manager=RiskManager(), configuration=config, clock=lambda: NOW,
                state_file=os.path.join(directory, 'status.json'),
                state_store=store, alert_sink=alerts,
                failure_backoff_base_seconds=2,
                failure_backoff_max_seconds=3,
            )
            engine._tick_once = MagicMock(side_effect=[
                RuntimeError('first'), RuntimeError('second'), None,
            ])

            self.assertFalse(engine._tick())
            self.assertEqual(engine._next_retry_delay, 2)
            self.assertFalse(engine._tick())
            self.assertEqual(engine._next_retry_delay, 3)
            self.assertTrue(engine._tick())
            self.assertEqual(engine._next_retry_delay, 0)
            self.assertEqual(engine._consecutive_tick_crashes, 0)
            crash_alerts = [
                call for call in alerts.notify.call_args_list
                if call.args[1] == 'tick_crashed'
            ]
            self.assertEqual(len(crash_alerts), 2)
            self.assertEqual(
                crash_alerts[-1].args[2]['retry_delay_seconds'], 3,
            )
            store.close()

    @patch('core.live_broker.ccxt')
    def test_retry_is_connected_to_prepare_reconcile_and_sync(self, ccxt_mock):
        ccxt_mock.binance.return_value = MagicMock()
        exchange = ccxt_mock.binance.return_value
        open_payload = {
            'id': 'e1', 'status': 'open', 'amount': 1,
            'filled': 0, 'remaining': 1,
        }
        exchange.create_order.return_value = open_payload
        exchange.fetch_balance.side_effect = [
            TimeoutError('balance blip'),
            {'free': {'USDT': 10000}, 'total': {'USDT': 10000}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            broker = self.make_live_broker(
                directory, exchange, retry_max_attempts=2,
            )
            intent = broker._build_intent(
                'BTC/USDT', 'buy', 1, 100, 'market', NOW, 'S',
                None, None, None, 0,
            )
            original_prepare = broker.exchange_boundary.prepare
            prepared = original_prepare(intent, reference_price=100)
            broker.exchange_boundary.prepare = MagicMock(side_effect=[
                TimeoutError('metadata blip'), prepared,
            ])

            submitted = broker.submit_intent(intent)
            self.assertEqual(submitted.status, OrderStatus.ACCEPTED)
            self.assertEqual(broker.exchange_boundary.prepare.call_count, 2)
            exchange.create_order.assert_called_once()

            exchange.fetch_order.side_effect = [
                TimeoutError('query blip'), open_payload,
            ]
            reconciled = broker.reconcile_order(submitted.client_order_id)
            self.assertEqual(reconciled.status, OrderStatus.ACCEPTED)
            self.assertEqual(exchange.fetch_order.call_count, 2)

            synced = broker.sync()
            self.assertTrue(synced.ok)
            self.assertEqual(exchange.fetch_balance.call_count, 2)
            broker.close()

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
                risk_manager=RiskManager(), configuration=config, clock=lambda: NOW,
                state_file=path, state_store=store, alert_sink=MagicMock(),
            )
            engine._export_state()
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["schema_version"], 1)
            self.assertFalse(os.path.exists(f"{path}.{os.getpid()}.tmp"))
            store.close()

    def test_limit_fills_never_cross_limit_after_slippage(self):
        broker = Broker(
            Portfolio(
                account_mode=AccountMode.SPOT_MARGIN,
                initial_margin_rate=0.20,
                maintenance_margin_rate=0.10,
            ),
            slippage=0.10,
            commission_rate=0,
        )
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
