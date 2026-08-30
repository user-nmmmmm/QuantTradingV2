import unittest

import pandas as pd

from core.broker import BacktestOrderStatus, Broker
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state import MarketState
from router.router import Router
from strategies.base import Strategy


class PassiveStrategy(Strategy):
    def should_enter(self, symbol, i, df, state, portfolio):
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


class TestRouterSwitchCleanup(unittest.TestCase):
    def test_switch_does_not_flatten_position_owned_by_opening_strategy(self):
        portfolio = Portfolio(initial_capital=10000.0)
        portfolio.update_position("BTC/USDT", qty_delta=2.0, price=100.0)
        broker = Broker(portfolio)
        risk_manager = RiskManager()

        trend_strategy = PassiveStrategy("TrendUp", {MarketState.TREND_UP})
        range_strategy = PassiveStrategy("RangeMeanReversion", {MarketState.SIDEWAYS})
        trend_strategy.context["BTC/USDT"] = {"stop_loss": 95.0, "entry_bar": 1}

        router = Router(
            {
                "TrendUp": trend_strategy,
                "RangeMeanReversion": range_strategy,
            },
            regime_map={
                "TREND_UP": "TrendUp",
                "TREND_DOWN": "Cash",
                "SIDEWAYS": "RangeMeanReversion",
                "VOLATILE": "Cash",
            },
            cooldown_bars=2,
            log_path="routing.csv",
        )
        router.symbol_states["BTC/USDT"] = MarketState.TREND_UP

        broker.submit_order(
            "BTC/USDT",
            "buy",
            0.5,
            price=90.0,
            order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        bar = pd.Series(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1000.0,
            },
            name=pd.Timestamp("2024-01-01"),
        )
        broker.process_orders({"BTC/USDT": bar})
        stale_order = broker.active_orders[0]

        df = pd.DataFrame(
            {"close": [101.0]},
            index=[pd.Timestamp("2024-01-02")],
        )
        candidate = router.collect_candidate(
            "BTC/USDT",
            0,
            df,
            MarketState.SIDEWAYS,
            portfolio,
            broker,
            risk_manager,
            {"BTC/USDT": 101.0},
        )
        router.allocator.allocate(
            [candidate] if candidate is not None else [],
            portfolio=portfolio,
            broker=broker,
            risk_manager=risk_manager,
            current_prices={"BTC/USDT": 101.0},
        )

        self.assertEqual(stale_order.status, BacktestOrderStatus.SUBMITTED)
        self.assertEqual(
            trend_strategy.context["BTC/USDT"],
            {"stop_loss": 95.0, "entry_bar": 1},
            "opening context must survive until the flatten fill is consumed",
        )
        self.assertNotIn("BTC/USDT", router.cooldowns)
        self.assertEqual(len(broker.active_orders), 1)
        self.assertEqual(len(broker.pending_orders), 0)
        self.assertEqual(router.log_buffer[-1]["route_event"], "position_exit_control")
        self.assertFalse(router.log_buffer[-1]["strategy_changed"])

    def test_regime_change_with_same_strategy_does_not_flatten_or_cooldown(self):
        portfolio = Portfolio(initial_capital=10000.0)
        broker = Broker(portfolio)
        risk_manager = RiskManager()

        breakout_strategy = PassiveStrategy(
            "TrendBreakout",
            {MarketState.TREND_UP, MarketState.VOLATILE},
        )
        breakout_strategy.context["BTC/USDT"] = {"stop_loss": 95.0, "entry_bar": 1}

        router = Router(
            {"TrendBreakout": breakout_strategy},
            regime_map={
                "TREND_UP": "TrendBreakout",
                "VOLATILE": "TrendBreakout",
                "TREND_DOWN": "Cash",
                "SIDEWAYS": "Cash",
            },
            cooldown_bars=2,
            log_path="routing.csv",
        )
        router.symbol_states["BTC/USDT"] = MarketState.TREND_UP

        broker.submit_order(
            "BTC/USDT",
            "buy",
            0.5,
            price=90.0,
            order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        bar = pd.Series(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1000.0,
            },
            name=pd.Timestamp("2024-01-01"),
        )
        broker.process_orders({"BTC/USDT": bar})
        stale_order = broker.active_orders[0]

        df = pd.DataFrame(
            {"close": [101.0]},
            index=[pd.Timestamp("2024-01-02")],
        )
        candidate = router.collect_candidate(
            "BTC/USDT",
            0,
            df,
            MarketState.VOLATILE,
            portfolio,
            broker,
            risk_manager,
            {"BTC/USDT": 101.0},
        )
        router.allocator.allocate(
            [candidate] if candidate is not None else [],
            portfolio=portfolio,
            broker=broker,
            risk_manager=risk_manager,
            current_prices={"BTC/USDT": 101.0},
        )

        self.assertEqual(stale_order.status, BacktestOrderStatus.SUBMITTED)
        self.assertEqual(breakout_strategy.context["BTC/USDT"]["stop_loss"], 95.0)
        self.assertNotIn("BTC/USDT", router.cooldowns)
        self.assertEqual(len(broker.pending_orders), 0)
        self.assertEqual(router.log_buffer[-1]["strategy"], "TrendBreakout")
        self.assertEqual(router.log_buffer[-1]["route_event"], "candidate")
        self.assertFalse(router.log_buffer[-1]["strategy_changed"])


if __name__ == "__main__":
    unittest.main()
