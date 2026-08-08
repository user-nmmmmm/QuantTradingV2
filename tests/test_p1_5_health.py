import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from core.domain import OrderStatus
from core.health import (
    DataHealthMonitor,
    DataHealthPolicy,
    HealthAssessment,
    HealthReason,
)
from core.live_broker import LiveBroker
from core.portfolio import Portfolio
from core.risk import RiskManager
from live_trading.engine import LiveTradingEngine


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def frame(*timestamps):
    return pd.DataFrame(
        {
            "open": [100.0] * len(timestamps),
            "high": [101.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": [100.0] * len(timestamps),
            "volume": [1000.0] * len(timestamps),
        },
        index=pd.to_datetime(list(timestamps), utc=True),
    )


class TestDataHealthPolicy(unittest.TestCase):
    def test_healthy_symbol_timeframe_and_sync_facts(self):
        assessment = DataHealthMonitor().assess(
            now=NOW,
            symbols=["BTC/USDT"],
            timeframe="1d",
            data_map={"BTC/USDT": frame("2026-08-01T00:00:00Z")},
            account_synced_at=NOW,
            order_synced_at=NOW,
        )

        self.assertTrue(assessment.healthy)
        self.assertTrue(assessment.allows_new_risk)
        self.assertEqual(assessment.to_dict()["reason_codes"], [])

    def test_missing_gap_future_stale_and_sync_freshness_are_structured(self):
        policy = DataHealthPolicy(
            account_sync_max_age=timedelta(seconds=30),
            order_sync_max_age=timedelta(seconds=30),
        )
        assessment = DataHealthMonitor(policy).assess(
            now=NOW,
            symbols=["MISSING", "GAP", "STALE", "FUTURE"],
            timeframe="1d",
            data_map={
                "GAP": frame("2026-07-29T00:00:00Z", "2026-08-01T00:00:00Z"),
                "STALE": frame("2026-07-20T00:00:00Z"),
                "FUTURE": frame("2026-08-03T00:00:00Z"),
            },
            account_synced_at=NOW - timedelta(minutes=5),
            order_synced_at=None,
        )

        self.assertFalse(assessment.allows_new_risk)
        self.assertTrue({
            "MARKET_DATA_MISSING", "MARKET_DATA_GAP", "MARKET_DATA_STALE",
            "MARKET_DATA_FUTURE", "ACCOUNT_SYNC_STALE", "ORDER_SYNC_MISSING",
        }.issubset(set(assessment.reason_codes)))
        payload = assessment.to_dict()
        self.assertIsInstance(payload["reasons"], list)
        self.assertIn("subject", payload["reasons"][0])

    def test_timestamp_regression_is_detected_across_observations(self):
        monitor = DataHealthMonitor()
        common = dict(
            now=NOW, symbols=["BTC/USDT"], timeframe="1d",
            account_synced_at=NOW, order_synced_at=NOW,
        )
        monitor.assess(
            data_map={"BTC/USDT": frame("2026-08-01T00:00:00Z")}, **common
        )
        regressed = monitor.assess(
            data_map={"BTC/USDT": frame("2026-07-31T00:00:00Z")}, **common
        )
        self.assertIn("MARKET_DATA_REGRESSION", regressed.reason_codes)


class TestHealthFailClosed(unittest.TestCase):
    def setUp(self):
        self.unhealthy = HealthAssessment(
            NOW,
            (HealthReason(
                "MARKET_DATA_STALE", "market_data", "BTC/USDT@1d", "stale"
            ),),
        )

    def test_risk_manager_rejects_new_risk_when_health_is_stale(self):
        risk = RiskManager()
        portfolio = Portfolio(10000)
        risk.set_health_assessment(self.unhealthy)

        self.assertEqual(risk.calculate_position_size_fixed_pct(10000, 100), 0)
        self.assertFalse(risk.check_entry_risk(portfolio, "BTC/USDT", 1, 100))

    @patch("core.live_broker.ccxt")
    def test_execution_boundary_blocks_open_but_allows_reduce(self, ccxt_mock):
        exchange = MagicMock()
        exchange.create_order.return_value = {
            "id": "reduce-1", "status": "open", "amount": 1,
            "filled": 0, "remaining": 1,
        }
        ccxt_mock.binance.return_value = exchange
        portfolio = Portfolio(10000)
        portfolio.positions = {"BTC/USDT": {"qty": 1.0, "avg_price": 100.0}}
        broker = LiveBroker(portfolio, clock=lambda: NOW)
        broker.set_health_assessment(self.unhealthy)

        opening = broker.submit_order("BTC/USDT", "buy", 1, 100)
        self.assertEqual(opening.status, OrderStatus.REJECTED)
        exchange.create_order.assert_not_called()

        reducing = broker.submit_order("BTC/USDT", "sell", 1, 100)
        self.assertEqual(reducing.status, OrderStatus.ACCEPTED)
        exchange.create_order.assert_called_once()
        broker.close()

    def test_dashboard_exports_explainable_health_reasons(self):
        broker = MagicMock()
        broker.market_type = "spot"
        broker.portfolio = Portfolio(10000)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "status.json")
            engine = LiveTradingEngine(
                symbols=["BTC/USDT"], strategies={}, broker=broker,
                risk_manager=RiskManager(), data_fetcher=MagicMock(),
                clock=lambda: NOW, state_file=path,
            )
            engine._set_health_assessment(self.unhealthy)
            engine._export_state()
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(payload["health_reason_codes"], ["MARKET_DATA_STALE"])
        self.assertFalse(payload["health_assessment"]["allows_new_risk"])
        self.assertEqual(
            payload["health_assessment"]["reasons"][0]["subject"],
            "BTC/USDT@1d",
        )


if __name__ == "__main__":
    unittest.main()
