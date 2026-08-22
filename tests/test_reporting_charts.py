"""New backtest report charts: monthly heatmap, rolling Sharpe/drawdown, PnL
distribution. Verifies the files are produced when data is sufficient and
skipped (not crashed) when it isn't."""
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from backtest.reporting import ReportGenerator


def _long_equity_curve(periods: int = 120, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.001, scale=0.01, size=periods)
    equity = 10000.0 * np.cumprod(1 + steps)
    index = pd.date_range("2026-01-01", periods=periods, freq="D", tz="UTC")
    return pd.DataFrame({"equity": equity, "cash": equity}, index=index)


def _closed_trade_producing_trades():
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
        {
            "symbol": "BTC/USDT", "side": "buy", "qty": 1.0, "fill_price": 10300.0,
            "commission": 10.0, "slip": 5.0, "strategy_id": "TrendBreakout",
            "fill_time": pd.Timestamp("2026-01-10", tz="UTC"),
        },
        {
            "symbol": "BTC/USDT", "side": "sell", "qty": 1.0, "fill_price": 10100.0,
            "commission": 11.0, "slip": 5.0, "strategy_id": "TrendBreakout",
            "fill_time": pd.Timestamp("2026-01-15", tz="UTC"),
        },
    ]


class TestReportingCharts(unittest.TestCase):
    def test_charts_produced_with_sufficient_data(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = ReportGenerator(directory)
            generator.generate(
                _closed_trade_producing_trades(),
                _long_equity_curve(),
            )

            for name in (
                "equity.png",
                "monthly_returns_heatmap.png",
                "rolling_metrics.png",
                "pnl_distribution.png",
            ):
                path = os.path.join(directory, name)
                self.assertTrue(os.path.exists(path), f"expected {name} to be generated")
                self.assertGreater(os.path.getsize(path), 0)

    def test_charts_skip_gracefully_with_insufficient_data(self):
        """Short equity curves / no trades must not crash generate()."""
        with tempfile.TemporaryDirectory() as directory:
            generator = ReportGenerator(directory)
            equity_curve = pd.DataFrame(
                {
                    "equity": [10000.0, 10100.0, 9950.0],
                    "cash": [10000.0, 10100.0, 9950.0],
                },
                index=pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC"),
            )

            metrics = generator.generate([], equity_curve)

            self.assertIsInstance(metrics, dict)
            self.assertTrue(os.path.exists(os.path.join(directory, "equity.png")))
            # Insufficient samples for these -> skipped, not crashed, not written.
            self.assertFalse(
                os.path.exists(os.path.join(directory, "monthly_returns_heatmap.png"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(directory, "rolling_metrics.png"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(directory, "pnl_distribution.png"))
            )


if __name__ == "__main__":
    unittest.main()
