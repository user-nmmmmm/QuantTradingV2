import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.domain import OrderErrorCode, OrderIntent, OrderStatus
from core.live_broker import LiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class TestG2LiveOrders(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "orders.db")
        self.exchange = MagicMock()
        self.exchange.fetch_balance.return_value = {
            "free": {"USDT": 10000.0}, "total": {"USDT": 10000.0}
        }
        self.exchange.fetch_order_by_client_order_id = MagicMock()

    def tearDown(self):
        self.directory.cleanup()

    def broker(self):
        store = OrderStore(self.db_path)
        broker = LiveBroker(
            Portfolio(), order_store=store, clock=lambda: NOW,
            account_id="sandbox-spot",
        )
        broker.set_bar_context("1m", NOW)
        return broker

    @patch("core.live_broker.ccxt")
    def test_open_response_is_structured_and_not_recorded_as_fill(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1
        }
        broker = self.broker()

        result = broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")

        self.assertEqual(result.status, OrderStatus.ACCEPTED)
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(result.remaining_qty, 1)
        self.assertEqual(broker.order_store.fills_for(result.client_order_id), [])
        self.assertEqual(broker.trades, [])
        self.assertIn("clientOrderId", self.exchange.create_order.call_args.kwargs["params"])
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_same_signal_consumed_100_times_creates_one_exchange_order(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        payload = {"id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1}
        self.exchange.create_order.return_value = payload
        self.exchange.fetch_order_by_client_order_id.return_value = payload
        broker = self.broker()

        ids = {
            broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S").client_order_id
            for _ in range(100)
        }

        self.assertEqual(len(ids), 1)
        self.exchange.create_order.assert_called_once()
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_open_order_blocks_same_direction_entry_on_later_bar(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1
        }
        broker = self.broker()
        first = broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")
        broker.set_bar_context("1m", datetime(2026, 8, 2, 12, 1, tzinfo=timezone.utc))

        second = broker.submit_order("BTC/USDT", "buy", 1, 101, strategy_id="S")

        self.assertEqual(first.status, OrderStatus.ACCEPTED)
        self.assertEqual(second.status, OrderStatus.REJECTED)
        self.assertEqual(second.error_code, OrderErrorCode.SAFETY_POLICY)
        self.exchange.create_order.assert_called_once()
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_partial_order_remaining_notional_is_reserved(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 2, "filled": 0.5,
            "remaining": 1.5, "average": 100,
        }
        broker = self.broker()

        result = broker.submit_order("BTC/USDT", "buy", 2, 100, strategy_id="S")
        reserved = broker.pending_open_notional({"BTC/USDT": 110})
        broker.close()

        self.assertEqual(result.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(reserved, {"BTC/USDT": 165.0})

    @patch("core.live_broker.ccxt")
    def test_exchange_accepts_but_client_times_out_then_queries_without_retry(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.side_effect = TimeoutError("response lost")
        broker = self.broker()
        first = broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")
        self.assertEqual(first.status, OrderStatus.UNKNOWN)

        self.exchange.fetch_order_by_client_order_id.return_value = {
            "id": "e1", "clientOrderId": first.client_order_id,
            "status": "open", "amount": 1, "filled": 0, "remaining": 1,
        }
        recovered = broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")

        self.assertEqual(recovered.status, OrderStatus.ACCEPTED)
        self.exchange.create_order.assert_called_once()
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_pre_submit_crash_record_can_resume(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1
        }
        broker = self.broker()
        intent = broker._build_intent(
            "BTC/USDT", "buy", 1, 100, "market", NOW, "S", None, None, None, 0
        )
        broker.order_store.create_intent(intent, NOW.isoformat())

        result = broker.submit_order("BTC/USDT", "buy", 1, 100, timestamp=NOW, strategy_id="S")

        self.assertEqual(result.status, OrderStatus.ACCEPTED)
        self.exchange.create_order.assert_called_once()
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_partial_then_filled_persists_individual_fills(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        partial = {
            "id": "e1", "status": "open", "amount": 2, "filled": 1, "remaining": 1,
            "average": 100,
            "trades": [{"id": "f1", "amount": 1, "price": 100, "timestamp": 1}],
        }
        filled = {
            "id": "e1", "status": "closed", "amount": 2, "filled": 2, "remaining": 0,
            "average": 105,
            "trades": [
                {"id": "f1", "amount": 1, "price": 100, "timestamp": 1},
                {"id": "f2", "amount": 1, "price": 110, "timestamp": 2},
            ],
        }
        self.exchange.create_order.return_value = partial
        self.exchange.fetch_order.return_value = filled
        broker = self.broker()

        first = broker.submit_order("BTC/USDT", "buy", 2, 100, strategy_id="S")
        final = broker.submit_order("BTC/USDT", "buy", 2, 100, strategy_id="S")

        self.assertEqual(first.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(final.status, OrderStatus.FILLED)
        self.assertEqual(final.remaining_qty, 0)
        fills = broker.order_store.fills_for(final.client_order_id)
        self.assertEqual([fill["fill_id"] for fill in fills], ["f1", "f2"])
        self.assertEqual(sum(fill["qty"] for fill in fills), 2)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_cancel_fill_race_uses_exchange_fact(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 1, "filled": 0, "remaining": 1
        }
        self.exchange.cancel_order.side_effect = RuntimeError("cancel rejected")
        self.exchange.fetch_order.return_value = {
            "id": "e1", "status": "closed", "amount": 1, "filled": 1,
            "remaining": 0, "average": 100,
        }
        broker = self.broker()
        submitted = broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")

        final = broker.cancel_order(submitted.client_order_id)

        self.assertEqual(final.status, OrderStatus.FILLED)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_restart_recovers_unknown_by_client_id(self, ccxt_mock):
        ccxt_mock.binance.return_value = self.exchange
        self.exchange.create_order.side_effect = TimeoutError("lost")
        first_broker = self.broker()
        unknown = first_broker.submit_order("BTC/USDT", "buy", 1, 100, strategy_id="S")
        first_broker.close()

        self.exchange.create_order.reset_mock()
        self.exchange.create_order.side_effect = None
        self.exchange.fetch_order_by_client_order_id.return_value = {
            "id": "e1", "clientOrderId": unknown.client_order_id,
            "status": "open", "amount": 1, "filled": 0, "remaining": 1,
        }
        restarted = self.broker()
        recovery = restarted.recover_open_orders()

        self.assertEqual(recovery[unknown.client_order_id].status, OrderStatus.ACCEPTED)
        self.exchange.create_order.assert_not_called()
        restarted.close()

    def test_client_order_id_binds_every_identity_dimension(self):
        base = dict(
            exchange="binance", account="spot-a", symbol="BTC/USDT", timeframe="1m",
            bar_time=NOW.isoformat(), strategy_id="S", action="buy", sequence=0,
            requested_qty=1,
        )
        original = OrderIntent(**base).client_order_id
        variants = {
            "exchange": "okx", "account": "spot-b", "symbol": "ETH/USDT",
            "timeframe": "5m", "bar_time": "2026-08-02T12:01:00+00:00",
            "strategy_id": "T", "action": "sell", "sequence": 1,
        }
        for field, value in variants.items():
            changed = dict(base)
            changed[field] = value
            with self.subTest(field=field):
                self.assertNotEqual(OrderIntent(**changed).client_order_id, original)


if __name__ == "__main__":
    unittest.main()
