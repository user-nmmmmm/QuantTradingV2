import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from backtest.engine import BacktestEngine
from core.broker import BacktestOrderStatus, Broker
from core.portfolio import Portfolio
from core.state import MarketState


def frame(dates):
    values = list(range(len(dates)))
    return pd.DataFrame(
        {
            "open": [100.0 + x for x in values],
            "high": [101.0 + x for x in values],
            "low": [99.0 + x for x in values],
            "close": [100.5 + x for x in values],
            "volume": [10.0] * len(values),
        },
        index=pd.to_datetime(dates),
    )


class TestEventTimeline(unittest.TestCase):
    def test_union_timeline_routes_only_symbols_with_real_bars(self):
        data = {
            "A": frame(["2024-01-01", "2024-01-02"]),
            "B": frame(["2024-01-02", "2024-01-03"]),
        }
        router = MagicMock()
        state_machine = MagicMock()
        state_machine.get_state.return_value = MarketState.SIDEWAYS
        risk = MagicMock()
        risk.check_circuit_breaker.return_value = False

        with patch("backtest.engine.build_router", return_value=router), \
             patch("backtest.engine.build_state_machine", return_value=state_machine), \
             patch("backtest.engine.build_risk_manager", return_value=risk):
            with tempfile.TemporaryDirectory() as tmp:
                result = BacktestEngine(warmup_period=0, slippage=0).run(
                    data, strategies={}, routing_log_path=os.path.join(tmp, "routing.csv")
                )

        self.assertEqual(
            list(result["equity_curve"].index),
            list(pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])),
        )
        routed = [(call.args[0], call.args[2].index[call.args[1]]) for call in router.route.call_args_list]
        self.assertEqual(
            routed,
            [
                ("A", pd.Timestamp("2024-01-01")),
                ("A", pd.Timestamp("2024-01-02")),
                ("B", pd.Timestamp("2024-01-02")),
                ("B", pd.Timestamp("2024-01-03")),
            ],
        )


class TestPartialFillsAndTIF(unittest.TestCase):
    def setUp(self):
        self.portfolio = Portfolio(10000)
        self.broker = Broker(
            self.portfolio, commission_rate=0, commission_rate_maker=0,
            max_participation_rate=0.1,
        )
        self.t0 = pd.Timestamp("2024-01-01")
        self.t1 = pd.Timestamp("2024-01-02")

    def bar(self, timestamp, volume=10):
        return pd.Series(
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": volume},
            name=timestamp,
        )

    def test_missing_bar_cannot_fill(self):
        order = self.broker.submit_order("A", "buy", 1, timestamp=self.t0)
        self.assertEqual(self.broker.process_orders({"B": self.bar(self.t1)}), [])
        self.assertEqual(order.status, BacktestOrderStatus.SUBMITTED)
        trades = self.broker.process_orders({"A": self.bar(self.t1)})
        self.assertEqual(len(trades), 1)

    def test_partial_fill_preserves_remaining_quantity(self):
        order = self.broker.submit_order("A", "buy", 2, timestamp=self.t0)
        trades = self.broker.process_orders({"A": self.bar(self.t1)})
        self.assertEqual(trades[0]["qty"], 1)
        self.assertEqual(order.filled_qty, 1)
        self.assertEqual(order.remaining_qty, 1)
        self.assertEqual(order.status, BacktestOrderStatus.PARTIALLY_FILLED)

    def test_orders_share_symbol_volume_budget(self):
        first = self.broker.submit_order("A", "buy", 1, timestamp=self.t0)
        second = self.broker.submit_order("A", "buy", 1, timestamp=self.t0)
        trades = self.broker.process_orders({"A": self.bar(self.t1)})
        self.assertEqual(sum(t["qty"] for t in trades), 1)
        self.assertEqual(first.status, BacktestOrderStatus.FILLED)
        self.assertEqual(second.status, BacktestOrderStatus.SUBMITTED)

    def test_ioc_cancels_unfilled_remainder(self):
        order = self.broker.submit_order("A", "buy", 2, timestamp=self.t0, time_in_force="IOC")
        trades = self.broker.process_orders({"A": self.bar(self.t1)})
        self.assertEqual(trades[0]["qty"], 1)
        self.assertEqual(order.status, BacktestOrderStatus.CANCELED)
        self.assertEqual(order.remaining_qty, 1)

    def test_market_limit_and_stop_never_fill_on_signal_bar(self):
        cases = [
            ("market", None),
            ("limit", 100),
            ("stop", 100),
        ]
        for order_type, price in cases:
            with self.subTest(order_type=order_type):
                broker = Broker(Portfolio(10000), commission_rate=0, commission_rate_maker=0)
                order = broker.submit_order(
                    "A", "buy", 1, price=price, order_type=order_type, timestamp=self.t0
                )
                self.assertEqual(broker.process_orders({"A": self.bar(self.t0)}), [])
                self.assertEqual(order.status, BacktestOrderStatus.SUBMITTED)
                trades = broker.process_orders({"A": self.bar(self.t1)})
                self.assertEqual(len(trades), 1)
                self.assertGreater(trades[0]["fill_time"], trades[0]["signal_time"])

    def test_day_order_expires_before_next_trading_day(self):
        order = self.broker.submit_order(
            "A", "buy", 1, timestamp=self.t0, time_in_force="DAY"
        )
        self.assertEqual(self.broker.process_orders({"A": self.bar(self.t1)}), [])
        self.assertEqual(order.status, BacktestOrderStatus.EXPIRED)

    def test_explicit_expiry_prevents_late_fill(self):
        order = self.broker.submit_order(
            "A", "buy", 1, timestamp=self.t0, expire_time=self.t0
        )
        self.assertEqual(self.broker.process_orders({"A": self.bar(self.t1)}), [])
        self.assertEqual(order.status, BacktestOrderStatus.EXPIRED)
    def test_buy_and_short_require_sufficient_cash(self):
        for side in ("buy", "short"):
            with self.subTest(side=side):
                portfolio = Portfolio(50)
                broker = Broker(
                    portfolio,
                    commission_rate=0,
                    commission_rate_maker=0,
                )
                order = broker.submit_order("A", side, 1, timestamp=self.t0)
                self.assertEqual(broker.process_orders({"A": self.bar(self.t1, volume=100)}), [])
                self.assertEqual(order.status, BacktestOrderStatus.REJECTED)
                self.assertEqual(portfolio.cash, 50)
                self.assertEqual(portfolio.get_position("A")["qty"], 0)

    def test_no_position_to_close_is_not_a_rejection(self):
        order = self.broker.submit_order("A", "sell", 1, timestamp=self.t0)
        self.assertEqual(self.broker.process_orders({"A": self.bar(self.t1)}), [])
        self.assertEqual(order.status, BacktestOrderStatus.NO_POSITION)
        self.assertNotEqual(order.status, BacktestOrderStatus.REJECTED)

    def test_fok_rejects_partial_execution(self):
        order = self.broker.submit_order("A", "buy", 2, timestamp=self.t0, time_in_force="FOK")
        self.assertEqual(self.broker.process_orders({"A": self.bar(self.t1)}), [])
        self.assertEqual(order.status, BacktestOrderStatus.CANCELED)
        self.assertEqual(self.portfolio.get_position("A")["qty"], 0)


if __name__ == "__main__":
    unittest.main()