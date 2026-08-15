"""ReportGenerator.generate() actually wires the previously-dead analytics
functions and the MetricResult contract into its output (M-02/M-06/M-07)."""
import tempfile
import unittest

import jsonschema
import pandas as pd

from backtest.reporting import ReportGenerator
from core.metric_result import MetricResult


def _equity_curve():
    return pd.DataFrame(
        {
            "equity": [10000.0, 10100.0, 9950.0, 10300.0, 10250.0],
            "cash": [10000.0, 10100.0, 9950.0, 10300.0, 10250.0],
        },
        index=pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC"),
    )


def _trades():
    return [
        {
            "symbol": "BTC/USDT", "side": "buy", "qty": 1.0, "fill_price": 10000.0,
            "commission": 10.0, "slip": 5.0, "strategy_id": "TrendBreakout",
            "fill_time": pd.Timestamp("2026-01-01", tz="UTC"),
        },
        {
            "symbol": "BTC/USDT", "side": "sell", "qty": 1.0, "fill_price": 10300.0,
            "commission": 11.0, "slip": 5.0, "strategy_id": "TrendBreakout",
            "fill_time": pd.Timestamp("2026-01-04", tz="UTC"),
        },
    ]


class TestReportingExtendedAnalytics(unittest.TestCase):
    def test_generate_includes_extended_analytics_and_metric_results(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(_trades(), _equity_curve())

        self.assertIn("ExtendedAnalytics", metrics)
        extended = metrics["ExtendedAnalytics"]
        for key in ("trade_quality", "attribution", "r_multiple", "cost_sensitivity",
                    "drawdown_events"):
            self.assertIn(key, extended)

        # The closed trade actually made it into the previously-unused
        # attribution/trade_quality functions (not just an empty stub).
        self.assertEqual(extended["trade_quality"]["sample_size"], 1)
        self.assertEqual(extended["attribution"]["by_symbol"]["BTC/USDT"], 279.0)
        self.assertTrue(len(extended["drawdown_events"]) >= 1)

    def test_metric_results_conform_to_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                _trades(), _equity_curve(), metrics_only=True,
            )

        self.assertIn("MetricResults", metrics)
        results = metrics["MetricResults"]
        self.assertTrue(len(results) >= 1)
        schema = MetricResult.json_schema()
        names = set()
        for entry in results:
            jsonschema.validate(entry, schema)
            names.add(entry["name"])
        self.assertIn("ProfitFactor", names)
        self.assertIn("SharpeRatio", names)

    def test_empty_trades_still_produce_valid_extended_analytics(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                [], _equity_curve(), metrics_only=True,
            )

        self.assertIn("ExtendedAnalytics", metrics)
        self.assertEqual(metrics["ExtendedAnalytics"]["trade_quality"]["status"], "insufficient")
        schema = MetricResult.json_schema()
        for entry in metrics["MetricResults"]:
            jsonschema.validate(entry, schema)

    def test_metrics_only_skips_disk_artifacts(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            ReportGenerator(directory).generate(_trades(), _equity_curve(), metrics_only=True)
            self.assertFalse(os.path.exists(os.path.join(directory, "report.txt")))
            self.assertFalse(os.path.exists(os.path.join(directory, "trades.csv")))


if __name__ == "__main__":
    unittest.main()
