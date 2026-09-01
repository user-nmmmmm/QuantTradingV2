import os
import unittest
from unittest.mock import MagicMock, patch

from core.live_safety import OrderSafetyGuard, StartupSafetyPolicy
from core.portfolio import Portfolio
from core.live_broker.safe import SafeLiveBroker


class TestSafeLiveBroker(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_active_kill_switch_never_calls_exchange_create_order(self, mock_ccxt):
        exchange = MagicMock()
        mock_ccxt.binance.return_value = exchange
        policy = StartupSafetyPolicy(
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
        broker = SafeLiveBroker(
            Portfolio(),
            safety_guard=OrderSafetyGuard(policy),
            sandbox=True,
        )

        with patch.dict(os.environ, {"QUANT_KILL_SWITCH": "1"}):
            broker.submit_order("BTC/USDT", "buy", 0.01, 50_000, order_type="limit")

        exchange.create_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
