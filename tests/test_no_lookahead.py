import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import BacktestEngine
from core.state import MarketState
from router.router import Router
from strategies.base import Strategy


class MockStrategy(Strategy):
    def __init__(self):
        super().__init__("MockStrategy", {MarketState.SIDEWAYS, MarketState.TREND_UP, MarketState.TREND_DOWN})
        self.triggered = False

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == 50 and not self.triggered:
            self.triggered = True
            current_price = df["close"].iloc[i]
            return {"action": "buy", "stop_loss": current_price * 0.95}
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


class TestNoLookahead(unittest.TestCase):
    def test_execution_timing(self):
        dates = pd.date_range(start="2024-01-01", periods=100, freq="D")
        prices = np.arange(100) + 100.0
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices + 2.0,
                "low": prices - 2.0,
                "close": prices + 0.5,
                "volume": [2000] * 100,
            },
            index=dates,
        )

        strategy = MockStrategy()

        def mock_build_router(strategies, log_path=None):
            router = MagicMock()
            router.route.side_effect = (
                lambda symbol, i, data, state, portfolio, broker, risk_manager, prices:
                    strategies["Mock"].on_bar(
                        symbol, i, data, MarketState.SIDEWAYS, portfolio, broker, risk_manager, prices
                    )
            )
            return router
        engine = BacktestEngine(initial_capital=10000.0, slippage=0.0, warmup_period=20)

        risk_manager = MagicMock()
        risk_manager.circuit_breaker_triggered = False
        risk_manager.check_circuit_breaker.return_value = False
        risk_manager.calculate_position_size.return_value = 1.0
        risk_manager.check_entry_risk.return_value = True
        # Pass sizing through unclamped: this test is about execution timing,
        # not risk caps.
        risk_manager.clamp_entry_qty.side_effect = lambda *a, **k: a[2]

        with patch("backtest.engine.build_risk_manager", return_value=risk_manager), \
             patch("backtest.engine.build_strategy_registry", return_value={"Mock": strategy}), \
             patch("backtest.engine.build_router", side_effect=mock_build_router):
            with tempfile.TemporaryDirectory() as temp_dir:
                results = engine.run(
                    {"TEST": df},
                    routing_log_path=os.path.join(temp_dir, "routing_log.csv"),
                )
        trades = results["trades"]
        self.assertTrue(len(trades) > 0, "No trades generated")

        trade = trades[0]
        self.assertEqual(trade["signal_time"], dates[50])
        self.assertEqual(trade["fill_time"], dates[51])
        self.assertEqual(trade["fill_price"], 151.0)


if __name__ == "__main__":
    unittest.main()
