import unittest
from unittest.mock import MagicMock, patch
import json
import os
import sys
import tempfile
import pandas as pd

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading.engine import LiveTradingEngine
from core.portfolio import Portfolio
from core.risk import RiskManager


class TestDashboardIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager()
        self.mock_broker = MagicMock()
        self.mock_broker.portfolio = self.portfolio

        # Mock Data
        self.mock_broker.portfolio.positions = {
            "BTC/USDT": {"qty": 1.0, "avg_price": 45000.0}
        }
        self.mock_broker.portfolio.cash = 10000.0

        self.engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=self.mock_broker,
            risk_manager=self.risk_manager,
            state_file=os.path.join(self.temp_dir.name, "live_status.json"),
        )

        # Mock Data Map
        self.engine.data_map["BTC/USDT"] = pd.DataFrame(
            {"close": [50000.0]}, index=[pd.Timestamp.now()]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_state_export(self):
        # Clean up
        if os.path.exists(self.engine.state_file):
            os.remove(self.engine.state_file)

        # Run Export
        self.engine._export_state()

        # Verify File Exists
        self.assertTrue(os.path.exists(self.engine.state_file))

        # Verify Content
        with open(self.engine.state_file, "r") as f:
            data = json.load(f)

        self.assertIn("timestamp", data)
        self.assertIn("equity", data)
        self.assertIn("positions", data)
        self.assertEqual(data["positions"]["BTC/USDT"]["qty"], 1.0)
        # Equity = 10000 + 1.0 * 50000 = 60000
        self.assertEqual(data["equity"], 60000.0)


if __name__ == "__main__":
    unittest.main()
