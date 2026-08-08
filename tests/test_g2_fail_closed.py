import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.domain import OrderErrorCode, OrderStatus
from core.live_broker import LiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio


class AuthenticationError(Exception):
    pass


class TestG2FailClosed(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_unknown_blocks_additional_same_direction_risk(self, ccxt_mock):
        exchange = MagicMock()
        exchange.create_order.side_effect = TimeoutError("lost")
        ccxt_mock.binance.return_value = exchange
        with tempfile.TemporaryDirectory() as directory:
            broker = LiveBroker(
                Portfolio(), order_store=OrderStore(os.path.join(directory, "orders.db")),
                clock=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            broker.set_bar_context("1m", "2026-08-02T00:00:00+00:00")
            first = broker.submit_order("BTC/USDT", "buy", 1, 100, sequence=0)
            second = broker.submit_order("BTC/USDT", "buy", 1, 100, sequence=1)

            self.assertEqual(first.status, OrderStatus.UNKNOWN)
            self.assertEqual(second.status, OrderStatus.REJECTED)
            self.assertEqual(second.error_code, OrderErrorCode.SAFETY_POLICY)
            self.assertEqual(exchange.create_order.call_count, 3)
            broker.close()

    @patch("core.live_broker.ccxt")
    def test_authentication_rejection_is_classified_and_safely_persisted(self, ccxt_mock):
        exchange = MagicMock()
        exchange.create_order.side_effect = AuthenticationError("invalid key")
        ccxt_mock.binance.return_value = exchange
        broker = LiveBroker(
            Portfolio(), order_store=OrderStore(":memory:"),
            clock=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
        result = broker.submit_order("BTC/USDT", "buy", 1, 100)

        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertEqual(result.error_code, OrderErrorCode.AUTHENTICATION)
        self.assertTrue(result.safe_to_complete_bar)
        broker.close()


if __name__ == "__main__":
    unittest.main()
