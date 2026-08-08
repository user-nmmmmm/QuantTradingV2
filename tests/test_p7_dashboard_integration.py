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
from core.domain import SyncResult
from core.health import HealthAssessment, HealthReason
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

    def test_state_export_is_atomic_with_schema_version(self):
        if os.path.exists(self.engine.state_file):
            os.remove(self.engine.state_file)

        self.engine._export_state()

        # No leftover temp file after a clean write.
        self.assertFalse(os.path.exists(f"{self.engine.state_file}.tmp"))
        with open(self.engine.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], 1)

    def test_state_export_survives_crash_mid_write(self):
        # A prior half-written file must never be visible: a crash between
        # writing the tmp file and the atomic rename either leaves the old
        # file intact or the new one complete, never a half-written blob.
        with open(self.engine.state_file, "w") as f:
            f.write('{"schema_version": 0, "stale": true}')

        original_dump = json.dump

        def crash_after_write(*args, **kwargs):
            original_dump(*args, **kwargs)
            raise OSError("simulated crash before rename")

        with patch("live_trading.engine.json.dump", side_effect=crash_after_write):
            self.engine._export_state()

        with open(self.engine.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data, {"schema_version": 0, "stale": True})


class TestAlertingAndFaultTolerance(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager()
        self.mock_broker = MagicMock()
        self.mock_broker.portfolio = self.portfolio
        self.mock_broker.portfolio.positions = {}
        self.mock_broker.portfolio.cash = 10000.0
        self.mock_broker.market_type = "spot"
        self.mock_broker.recover_open_orders.return_value = {}
        self.mock_broker.has_unresolved_unknown.return_value = False
        self.mock_broker.sync.return_value = SyncResult(True, pd.Timestamp.utcnow())

        self.alert_sink = MagicMock()
        self.engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=self.mock_broker,
            risk_manager=self.risk_manager,
            state_file=os.path.join(self.temp_dir.name, "live_status.json"),
            alert_sink=self.alert_sink,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_alert_sink_notified_on_health_unhealthy(self):
        self.engine._assess_health(
            pd.Timestamp.utcnow().to_pydatetime(),
            HealthReason("TEST_REASON", "test", "test", "forced unhealthy"),
        )
        self.alert_sink.notify.assert_called_once()
        level, event, context = self.alert_sink.notify.call_args[0]
        self.assertEqual(level, "critical")
        self.assertEqual(event, "health_unhealthy")
        self.assertIn("TEST_REASON", context["reason_codes"])

    def test_alert_sink_not_re_notified_for_same_reason(self):
        reason = HealthReason("TEST_REASON", "test", "test", "forced unhealthy")
        now = pd.Timestamp.utcnow().to_pydatetime()
        self.engine._assess_health(now, reason)
        self.engine._assess_health(now, reason)
        self.assertEqual(self.alert_sink.notify.call_count, 1)

    def test_tick_survives_market_data_refresh_exception(self):
        self.engine.market_data_adapter.refresh = MagicMock(
            side_effect=RuntimeError("boom")
        )
        # Must not raise: an unexpected data-refresh failure should degrade
        # to a health halt, not kill the process.
        self.engine._tick()
        self.assertFalse(self.engine._healthy)
        self.assertIn(
            "MARKET_DATA_REFRESH_FAILED",
            self.engine.health_assessment.reason_codes,
        )
        self.alert_sink.notify.assert_called()


if __name__ == "__main__":
    unittest.main()
