import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from config.config import config
from core.live_broker import LiveBroker
from live_trading.engine import LiveTradingEngine
from core.portfolio import Portfolio
from core.risk import RiskManager


class TestLiveTrading(unittest.TestCase):
    def setUp(self):
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager()

        self.mock_exchange = MagicMock()
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 10000.0, "BTC": 0.2},
            "free": {"USDT": 9500.0, "BTC": 0.2},
        }
        self.mock_exchange.fetch_positions.return_value = []
        self.mock_exchange.create_order.return_value = {
            "id": "12345",
            "status": "closed",
            "average": 50000.0,
        }

    @patch("core.live_broker.ccxt")
    def test_broker_sync_spot_mode_uses_balances(self, mock_ccxt):
        mock_ccxt.binance.return_value = self.mock_exchange

        broker = LiveBroker(
            self.portfolio,
            exchange_id="binance",
            market_type="spot",
        )
        broker.sync()

        self.assertEqual(self.portfolio.cash, 10000.0)
        self.assertEqual(self.portfolio.positions["BTC/USDT"]["qty"], 0.2)
        self.assertEqual(self.portfolio.positions["BTC/USDT"]["avg_price"], 0.0)
        self.mock_exchange.fetch_positions.assert_not_called()

    @patch("core.live_broker.ccxt")
    def test_broker_sync_futures_mode_prefers_fetch_positions(self, mock_ccxt):
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 15000.0},
            "free": {"USDT": 12000.0},
        }
        self.mock_exchange.fetch_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "contracts": "0.25",
                "side": "long",
                "entryPrice": "50000",
            }
        ]
        mock_ccxt.binance.return_value = self.mock_exchange

        broker = LiveBroker(
            self.portfolio,
            exchange_id="binance",
            market_type="future",
        )
        broker.sync()

        self.assertEqual(self.portfolio.cash, 15000.0)
        self.assertEqual(self.portfolio.positions["BTC/USDT"]["qty"], 0.25)
        self.assertEqual(self.portfolio.positions["BTC/USDT"]["avg_price"], 50000.0)
        self.mock_exchange.fetch_positions.assert_called_once()
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_broker_sync_futures_applies_short_side_to_unsigned_contracts(self, mock_ccxt):
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 15000.0}, "free": {"USDT": 12000.0}
        }
        self.mock_exchange.fetch_positions.return_value = [{
            "symbol": "BTC/USDT", "contracts": "0.25", "side": "short",
            "entryPrice": "50000",
        }]
        mock_ccxt.binance.return_value = self.mock_exchange
        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="future")

        result = broker.sync()

        self.assertTrue(result.ok)
        self.assertEqual(self.portfolio.positions["BTC/USDT"]["qty"], -0.25)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_broker_sync_futures_fails_closed_when_contract_direction_is_unknown(self, mock_ccxt):
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 15000.0}, "free": {"USDT": 12000.0}
        }
        self.mock_exchange.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "contracts": "0.25", "entryPrice": "50000"}
        ]
        mock_ccxt.binance.return_value = self.mock_exchange
        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="future")

        result = broker.sync()

        self.assertFalse(result.ok)
        self.assertNotIn("BTC/USDT", self.portfolio.positions)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_broker_sync_futures_fails_closed_for_two_position_legs(self, mock_ccxt):
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 15000.0}, "free": {"USDT": 12000.0}
        }
        self.mock_exchange.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "contracts": "0.25", "side": "long"},
            {"symbol": "BTC/USDT", "contracts": "0.10", "side": "short"},
        ]
        mock_ccxt.binance.return_value = self.mock_exchange
        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="future")

        result = broker.sync()

        self.assertFalse(result.ok)
        self.assertNotIn("BTC/USDT", self.portfolio.positions)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_broker_sync_futures_fails_closed_when_no_position_source_exists(self, mock_ccxt):
        self.mock_exchange.fetch_balance.return_value = {
            "total": {"USDT": 15000.0}, "free": {"USDT": 12000.0}
        }
        del self.mock_exchange.fetch_positions
        mock_ccxt.binance.return_value = self.mock_exchange
        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="future")

        result = broker.sync()

        self.assertFalse(result.ok)
        self.assertNotIn("BTC/USDT", self.portfolio.positions)
        broker.close()

    @patch("core.live_broker.ccxt")
    def test_broker_submit_order(self, mock_ccxt):
        mock_ccxt.binance.return_value = self.mock_exchange

        broker = LiveBroker(self.portfolio, exchange_id="binance")
        broker.submit_order("BTC/USDT", "buy", 0.1, 50000.0, "limit")

        call = self.mock_exchange.create_order.call_args
        self.assertEqual(call.kwargs["symbol"], "BTC/USDT")
        self.assertEqual(call.kwargs["type"], "limit")
        self.assertEqual(call.kwargs["side"], "buy")
        self.assertEqual(call.kwargs["amount"], 0.1)
        self.assertEqual(call.kwargs["price"], 50000.0)
        self.assertTrue(call.kwargs["params"]["clientOrderId"].startswith("qt_"))
        self.assertEqual(len(broker.trades), 1)

    @patch("core.live_broker.ccxt")
    def test_broker_rejects_spot_short_orders(self, mock_ccxt):
        mock_ccxt.binance.return_value = self.mock_exchange

        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="spot")
        broker.submit_order("BTC/USDT", "short", 0.1, 50000.0, "limit")

        self.mock_exchange.create_order.assert_not_called()
        self.assertEqual(len(broker.trades), 0)

    @patch("core.live_broker.ccxt")
    def test_engine_initialization_uses_injected_fetcher(self, mock_ccxt):
        mock_ccxt.binance.return_value = self.mock_exchange
        df = pd.DataFrame(
            {
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
                "volume": [1000, 1000],
            },
            index=pd.to_datetime(["2021-01-01", "2021-01-02"]),
        )
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_ccxt.return_value = df

        broker = LiveBroker(self.portfolio, exchange_id="binance")
        engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=broker,
            risk_manager=self.risk_manager,
            configuration=config,
            data_fetcher=mock_fetcher,
            lookback_days=2,
        )

        engine.initialize()

        self.assertIn("BTC/USDT", engine.data_map)
        self.assertEqual(len(engine.data_map["BTC/USDT"]), 2)
        mock_fetcher.fetch_ccxt.assert_called_with(
            "BTC/USDT",
            timeframe="1d",
            limit=100,
        )

    @patch("core.live_broker.ccxt")
    def test_engine_maps_trend_down_to_cash_for_spot(self, mock_ccxt):
        mock_ccxt.binance.return_value = self.mock_exchange
        broker = LiveBroker(self.portfolio, exchange_id="binance", market_type="spot")

        engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=broker,
            risk_manager=self.risk_manager,
            configuration=config,
            data_fetcher=MagicMock(),
        )

        self.assertEqual(engine.router.regime_map["TREND_DOWN"], "Cash")


if __name__ == "__main__":
    unittest.main()
