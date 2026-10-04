import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import json
import os
import sys
import tempfile
import pandas as pd

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.config import config
from live_trading.engine import LiveTradingEngine
from core.domain import PortfolioSnapshot
from core.order_latency import OrderLatencyRecorder
from core.portfolio import Portfolio
from core.risk import RiskManager


class TestLiveStatusExport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager()
        self.mock_broker = MagicMock()
        self.mock_broker.portfolio = self.portfolio
        self.mock_broker.market_type = "spot"
        self.mock_broker.order_latency = OrderLatencyRecorder()

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
            configuration=config,
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
        self.assertTrue(self.engine._export_state())

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
        self.assertIsInstance(data["performance"]["market_data"], dict)
        self.assertIsInstance(data["temporal_data"]["audit"], dict)
        self.assertEqual(data["order_request_observations"], self.mock_broker.order_latency.summary())

    def test_state_export_preserves_real_request_observations(self):
        ticks = iter((10.0, 10.125))
        recorder = OrderLatencyRecorder(clock=lambda: next(ticks))
        response = {"id": "order-1", "status": "open", "filled": 0}
        self.assertEqual(recorder.call("create_order", lambda: response), response)
        self.mock_broker.order_latency = recorder

        self.assertTrue(self.engine._export_state())
        with open(self.engine.state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        observed = state["order_request_observations"]
        self.assertEqual(observed, recorder.summary())
        self.assertEqual(observed["total_records"], 1)
        self.assertEqual(observed["statistics"]["create_order"]["p50_seconds"], 0.125)
        self.assertEqual(observed["records"][0]["outcome"], "ack")
        self.assertFalse(observed["records"][0]["terminal_observed"])

    def test_state_export_supports_broker_without_request_recorder(self):
        del self.mock_broker.order_latency
        self.assertTrue(self.engine._export_state())
        with open(self.engine.state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertIsNone(state["order_request_observations"])

    def test_portfolio_transition_forces_export_without_waiting_for_interval(self):
        self.engine._tick_count = 1
        self.assertTrue(self.engine._maybe_export_state())
        self.assertFalse(self.engine._maybe_export_state())
        self.risk_manager.current_transition_id = "epoch-0-recovery-2"
        with patch("live_trading.state_export.os.fsync") as sync:
            self.assertTrue(self.engine._maybe_export_state())
            sync.assert_called_once()
        with open(self.engine.state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["portfolio_breaker"]["current_transition_id"], "epoch-0-recovery-2")

    def test_state_export_reuses_authoritative_snapshot(self):
        # Once a tick has produced a valuation snapshot, the exported
        # status must match it rather than re-deriving equity from raw
        # bar closes under a different price rule.
        if os.path.exists(self.engine.state_file):
            os.remove(self.engine.state_file)

        self.engine._snapshot = PortfolioSnapshot(
            cash=10000.0,
            equity=51000.0,
            gross_exposure=51000.0,
            net_exposure=51000.0,
            prices={"BTC/USDT": 41000.0},
            price_times={"BTC/USDT": datetime.now(timezone.utc)},
            synced_at=datetime.now(timezone.utc),
        )

        self.assertTrue(self.engine._export_state())

        with open(self.engine.state_file, "r") as f:
            data = json.load(f)

        # Not 60000.0 (the data_map-derived value from setUp) — the
        # snapshot's own cash/equity must win.
        self.assertEqual(data["cash"], 10000.0)
        self.assertEqual(data["equity"], 51000.0)


if __name__ == "__main__":
    unittest.main()
