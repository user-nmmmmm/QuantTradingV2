import unittest

from core.broker import BacktestOrderStatus, Broker, Order
from core.portfolio import Portfolio


class TestBacktestExecutionResult(unittest.TestCase):
    def test_broker_submit_order_returns_rejected_object_not_none(self):
        broker = Broker(Portfolio())

        result = broker.submit_order("BTC/USDT", "buy", -1)

        self.assertIsInstance(result, Order)
        self.assertEqual(result.status, BacktestOrderStatus.REJECTED)
        self.assertFalse(result.accepted)
        self.assertEqual(broker.pending_orders, [])

    def test_missing_limit_price_returns_rejected_object(self):
        broker = Broker(Portfolio())

        result = broker.submit_order("BTC/USDT", "buy", 1, order_type="limit")

        self.assertEqual(result.status, BacktestOrderStatus.REJECTED)
        self.assertFalse(result.accepted)
        self.assertEqual(broker.pending_orders, [])


if __name__ == "__main__":
    unittest.main()
