import copy
import unittest
from datetime import datetime
from unittest.mock import MagicMock

from config.config import config
from core.system_factory import (
    build_risk_manager,
    build_router,
    build_state_machine,
    build_strategy_registry,
)
from live_trading.engine import LiveTradingEngine


class TestSystemFactory(unittest.TestCase):
    def setUp(self):
        self.original_config = copy.deepcopy(config._config)

    def tearDown(self):
        config._config = self.original_config

    def test_build_risk_manager_uses_config_limits(self):
        config._config["risk"]["liquidity_limit_pct"] = 0.03
        config._config["risk"]["max_pos_size_pct"] = 0.35

        risk_manager = build_risk_manager()

        self.assertEqual(risk_manager.liquidity_limit_pct, 0.03)
        self.assertEqual(risk_manager.max_pos_size_pct, 0.35)

    def test_build_router_maps_short_to_cash_when_shorting_disabled(self):
        router = build_router(build_strategy_registry(), allow_short=False)
        self.assertEqual(router.regime_map["TREND_DOWN"], "Cash")

    def test_missing_routing_section_fails_closed(self):
        del config._config["routing"]
        with self.assertRaisesRegex(Exception, "routing"):
            build_router(build_strategy_registry())

    def test_live_engine_rejects_naive_clock_values(self):
        engine = object.__new__(LiveTradingEngine)
        engine.clock = MagicMock()
        engine.clock.now.return_value = datetime(2026, 8, 13, 12, 0, 0)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            engine._now()

    def test_live_engine_uses_shared_router_and_state_machine_config(self):
        config._config["state"]["stability_period"] = 5
        config._config["router"]["cooldown_bars"] = 7
        config._config["routing"]["TREND_DOWN"] = "TrendBreakdown"

        broker = MagicMock()
        broker.market_type = "future"
        broker.portfolio = MagicMock()
        broker.portfolio.positions = {}
        broker.portfolio.cash = 10000.0
        broker.sync.return_value = None

        engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies=build_strategy_registry(),
            broker=broker,
            risk_manager=build_risk_manager(),
            data_fetcher=MagicMock(),
        )

        expected_router = build_router(build_strategy_registry(), allow_short=True)
        expected_state_machine = build_state_machine()

        self.assertEqual(engine.router.regime_map, expected_router.regime_map)
        self.assertEqual(engine.router.cooldown_bars, expected_router.cooldown_bars)
        self.assertEqual(engine.state_machine.stability_period, expected_state_machine.stability_period)


if __name__ == "__main__":
    unittest.main()
