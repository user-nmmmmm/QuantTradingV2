import unittest
from unittest.mock import MagicMock

from analysis.optimize import build_optimization_strategies
from config.config import config
from core.broker import Broker
from core.domain import OrderStatus
from core.lots import CloseEvent
from core.portfolio import Portfolio
from strategies.trend_breakout import TrendBreakoutStrategy


class TestS0StrategyCleanup(unittest.TestCase):
    def test_external_flatten_is_delivered_once_to_opening_strategy(self):
        strategy = TrendBreakoutStrategy()
        strategy.context["BTC/USDT"] = {
            "entry_price": 100.0,
            "_entry_fill_qty": 1.0,
        }
        portfolio = Portfolio()
        broker = MagicMock()
        # Router closes the position (T-1.3/T-1.4): the CloseEvent still
        # carries the opening strategy's id even though Router triggered it.
        broker.close_events = [
            CloseEvent(
                close_event_id="LOT-000000001:1",
                position_id="POS-000000001",
                lot_id="LOT-000000001",
                symbol="BTC/USDT",
                opening_strategy_id=strategy.name,
                exit_reason="StateSwitch",
                qty=1.0,
                exit_price=90.0,
                theoretical_exit_price=90.0,
                realized_pnl=-11.0,
                timestamp=None,
                is_position_fully_closed=True,
            )
        ]

        strategy._consume_execution_trades("BTC/USDT", 12, portfolio, broker)
        strategy._consume_execution_trades("BTC/USDT", 13, portfolio, broker)

        self.assertEqual(strategy.observed_close_events, 1)
        self.assertEqual(strategy.health_stats["total_trades"], 1)
        self.assertEqual(strategy.health_stats["rolling_pnl"], [-11.0])
        self.assertEqual(len(strategy._consumed_close_event_ids), 1)

    def test_six_consecutive_losses_trigger_health_gate(self):
        strategy = TrendBreakoutStrategy()
        for index in range(6):
            strategy.on_trade_closed("BTC/USDT", -1.0, {}, index)

        self.assertFalse(strategy.check_health())
        self.assertEqual(strategy.health_stats["consecutive_losses"], 6)
        self.assertEqual(strategy.health_stats["scope"], "cross_symbol_aggregate")

    def test_canceled_entry_releases_pending_latch(self):
        strategy = TrendBreakoutStrategy()
        strategy.context["BTC/USDT"] = {"entry_pending": True}
        portfolio = Portfolio()
        broker = Broker(portfolio)
        order = broker.submit_order(
            "BTC/USDT", "buy", 1.0, 100.0,
            order_type="limit", strategy_id=strategy.name,
        )
        broker.cancel_symbol_orders("BTC/USDT")

        strategy._consume_execution_trades("BTC/USDT", 1, portfolio, broker)

        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertNotIn("entry_pending", strategy.get_context("BTC/USDT"))

    def test_optimizer_registry_resolves_every_routing_target(self):
        strategies = build_optimization_strategies(20, 10)
        routed = {
            name for name in config.require("routing").values() if name != "Cash"
        }
        self.assertTrue(routed.issubset(strategies))
        self.assertEqual(strategies["TrendBreakout"].entry_window, 20)
        self.assertEqual(strategies["TrendBreakout"].exit_window, 10)
        changed = build_optimization_strategies(50, 20)
        self.assertEqual(changed["TrendBreakout"].entry_window, 50)
        self.assertEqual(changed["TrendBreakout"].exit_window, 20)


if __name__ == "__main__":
    unittest.main()
