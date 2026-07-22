import unittest

import pandas as pd

from core.portfolio import Portfolio
from core.state import MarketState
from strategies.trend_breakout import TrendBreakdownStrategy


class TestTrendBreakdownStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = TrendBreakdownStrategy(entry_window=3, exit_window=2)
        self.portfolio = Portfolio(initial_capital=10000.0)

    def test_donchian_levels_are_shifted(self):
        df = pd.DataFrame(
            {
                "open": [10, 9, 8, 7, 6],
                "high": [11, 10, 9, 8, 7],
                "low": [9, 8, 7, 6, 5],
                "close": [10, 9, 8, 7, 6],
                "volume": [100] * 5,
            },
            index=pd.date_range("2024-01-01", periods=5, freq="D"),
        )

        self.strategy._ensure_indicators(df)

        self.assertTrue(pd.isna(df["LOW_MIN_3"].iloc[2]))
        self.assertEqual(df["LOW_MIN_3"].iloc[3], 7)
        self.assertEqual(df["HIGH_MAX_2"].iloc[3], 10)

    def test_short_entry_uses_prior_channel_and_stop(self):
        df = pd.DataFrame(
            {
                "open": [10, 9, 8, 6.5],
                "high": [11, 10, 9, 7],
                "low": [9, 8, 7, 6],
                "close": [10, 9, 8, 6.5],
                "volume": [100] * 4,
            },
            index=pd.date_range("2024-01-01", periods=4, freq="D"),
        )

        signal = self.strategy.should_enter(
            "BTC/USDT",
            3,
            df,
            MarketState.TREND_DOWN,
            self.portfolio,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["action"], "short")
        self.assertEqual(signal["price"], 6.5)
        self.assertGreater(signal["stop_loss"], signal["price"])

    def test_cover_exit_on_opposite_channel_break(self):
        df = pd.DataFrame(
            {
                "open": [10, 9, 8, 9.5],
                "high": [11, 10, 9, 11.5],
                "low": [9, 8, 7, 9.0],
                "close": [10, 9, 8, 11.0],
                "volume": [100] * 4,
            },
            index=pd.date_range("2024-01-01", periods=4, freq="D"),
        )
        self.portfolio.update_position("BTC/USDT", qty_delta=-1.0, price=8.0)
        self.strategy.context["BTC/USDT"] = {"entry_price": 8.0}

        signal = self.strategy.should_exit(
            "BTC/USDT",
            3,
            df,
            MarketState.TREND_DOWN,
            self.portfolio,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["action"], "cover")
        self.assertIn("Above High2", signal["reason"])


if __name__ == "__main__":
    unittest.main()
