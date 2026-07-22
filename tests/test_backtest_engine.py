import copy
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from backtest.engine import BacktestEngine
from config.config import config
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.base import Strategy


class EntryProbeStrategy(Strategy):
    def __init__(self):
        super().__init__("Probe", {MarketState.SIDEWAYS})

    def should_enter(self, symbol, i, df, state, portfolio):
        return {"action": "buy", "stop_loss": df["close"].iloc[i] * 0.95}

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


class TestBacktestEngine(unittest.TestCase):
    def test_resets_daily_breaker_on_each_new_day(self):
        mock_risk_manager = MagicMock()
        mock_risk_manager.circuit_breaker_triggered = False
        mock_risk_manager.check_circuit_breaker.return_value = False

        df = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1000.0, 1000.0],
            },
            index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
        )

        engine = BacktestEngine(initial_capital=10000.0, slippage=0.0, warmup_period=0)
        with patch("backtest.engine.build_risk_manager", return_value=mock_risk_manager):
            with tempfile.TemporaryDirectory() as temp_dir:
                engine.run(
                    {"TEST": df},
                    strategies={},
                    routing_log_path=os.path.join(temp_dir, "routing_log.csv"),
                )

        self.assertEqual(mock_risk_manager.reset_daily_breaker.call_count, 2)

    def test_uses_config_slippage_when_omitted(self):
        original_config = copy.deepcopy(config._config)
        try:
            config._config["execution"]["slippage_bps"] = 12
            engine = BacktestEngine(initial_capital=10000.0, slippage=None)
            self.assertAlmostEqual(engine.slippage, 0.0012)
        finally:
            config._config = original_config

    def test_strategy_entry_risk_receives_bar_volume(self):
        strategy = EntryProbeStrategy()
        portfolio = Portfolio(initial_capital=10000.0)
        broker = MagicMock()
        risk_manager = MagicMock()
        risk_manager.calculate_position_size.return_value = 1.0
        risk_manager.check_entry_risk.return_value = False

        df = pd.DataFrame(
            {
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [4321.0],
            },
            index=[pd.Timestamp("2024-01-01")],
        )

        strategy.on_bar(
            "TEST",
            0,
            df,
            MarketState.SIDEWAYS,
            portfolio,
            broker,
            risk_manager,
            {"TEST": 100.0},
        )

        self.assertEqual(risk_manager.check_entry_risk.call_args.kwargs["current_volume"], 4321.0)

    def test_uses_shared_factories_for_default_components(self):
        df = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1000.0, 1000.0],
            },
            index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
        )

        mock_router = MagicMock()
        mock_state_machine = MagicMock()
        mock_state_machine.get_state.return_value = MarketState.SIDEWAYS
        mock_risk_manager = MagicMock()
        mock_risk_manager.circuit_breaker_triggered = False
        mock_risk_manager.check_circuit_breaker.return_value = False

        engine = BacktestEngine(initial_capital=10000.0, slippage=0.0, warmup_period=0)
        with patch("backtest.engine.build_strategy_registry", return_value={}) as mock_build_strategies:
            with patch("backtest.engine.build_router", return_value=mock_router) as mock_build_router:
                with patch("backtest.engine.build_state_machine", return_value=mock_state_machine) as mock_build_state:
                    with patch("backtest.engine.build_risk_manager", return_value=mock_risk_manager) as mock_build_risk:
                        with tempfile.TemporaryDirectory() as temp_dir:
                            engine.run(
                                {"TEST": df},
                                routing_log_path=os.path.join(temp_dir, "routing_log.csv"),
                            )

        mock_build_strategies.assert_called_once()
        mock_build_router.assert_called_once()
        mock_build_state.assert_called_once()
        mock_build_risk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
