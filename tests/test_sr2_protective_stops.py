"""SR2 protective-stop tests (docs/current_strategy_remediation_roadmap.md §13.2).

Covers the hand-computed stop fixtures, the no-lookahead contract, the
Chandelier monotonicity property, and the post-fill risk recheck that turns a
gapped entry into a named resize instead of silent over-risk.
"""

from __future__ import annotations

import random
import unittest

import pandas as pd

from core.indicators import Indicators
from core.protective_stops import (
    EntryRiskPolicy,
    ProtectiveStopPolicy,
    evaluate_fill_risk,
    plan_initial_stop,
    update_trailing_stop,
)
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.trend_breakout import TrendBreakoutStrategy


class TestInitialStopPlan(unittest.TestCase):
    def test_hybrid_takes_the_tighter_of_donchian_and_atr_for_a_long(self):
        # entry 100, Donchian low 90, ATR 3 with k=2 -> ATR stop 94.
        # max(90, 94) = 94: the tighter (higher) long stop wins.
        policy = ProtectiveStopPolicy(use_atr_initial_stop=True, initial_atr_multiple=2.0)
        plan = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=90.0,
            atr=3.0, policy=policy,
        )
        self.assertTrue(plan.accepted)
        self.assertAlmostEqual(plan.stop_price, 94.0)
        self.assertEqual(plan.method, "hybrid_max")
        self.assertAlmostEqual(plan.structural_stop, 90.0)
        self.assertAlmostEqual(plan.atr_stop, 94.0)

    def test_hybrid_keeps_donchian_when_atr_stop_is_further_away(self):
        policy = ProtectiveStopPolicy(use_atr_initial_stop=True, initial_atr_multiple=5.0)
        plan = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=95.0,
            atr=3.0, policy=policy,
        )
        self.assertAlmostEqual(plan.stop_price, 95.0)

    def test_short_side_is_mirrored(self):
        # entry 100, Donchian high 112, ATR 3, k=2 -> ATR stop 106.
        # min(112, 106) = 106.
        policy = ProtectiveStopPolicy(use_atr_initial_stop=True, initial_atr_multiple=2.0)
        plan = plan_initial_stop(
            side="short", reference_price=100.0, structural_stop=112.0,
            atr=3.0, policy=policy,
        )
        self.assertAlmostEqual(plan.stop_price, 106.0)

    def test_no_implicit_five_percent_fallback(self):
        """An unusable structural stop is rejected, not quietly replaced."""
        plan = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=float("nan"),
            atr=None, policy=ProtectiveStopPolicy(),
        )
        self.assertFalse(plan.accepted)
        self.assertEqual(plan.reject_reason, "no_valid_stop_level")
        self.assertIsNone(plan.stop_price)

        inverted = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=105.0,
            atr=None, policy=ProtectiveStopPolicy(),
        )
        self.assertFalse(inverted.accepted)

    def test_too_close_signal_is_rejected_and_too_far_is_clamped(self):
        policy = ProtectiveStopPolicy(
            min_stop_distance_pct=0.01, max_stop_distance_pct=0.20,
        )
        near = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=99.8,
            atr=None, policy=policy,
        )
        self.assertFalse(near.accepted)
        self.assertIn("stop_too_close", near.reject_reason)

        far = plan_initial_stop(
            side="buy", reference_price=100.0, structural_stop=50.0,
            atr=None, policy=policy,
        )
        self.assertTrue(far.accepted)
        self.assertAlmostEqual(far.stop_price, 80.0)
        self.assertEqual(far.method, "clamped_max_distance")


class TestTrailingStopMonotonicity(unittest.TestCase):
    def test_long_stop_never_falls_under_random_atr_paths(self):
        policy = ProtectiveStopPolicy(use_trailing_stop=True, trailing_atr_multiple=3.0)
        rng = random.Random(20260901)
        for _ in range(200):
            stop = 90.0
            initial = 90.0
            extreme = 100.0
            for _ in range(50):
                extreme = max(extreme, extreme * (1 + rng.uniform(-0.05, 0.08)))
                atr = rng.uniform(0.1, 25.0)  # includes violent ATR expansion
                new_stop = update_trailing_stop(
                    side="buy", current_stop=stop, initial_stop=initial,
                    extreme_since_fill=extreme, atr=atr, policy=policy,
                )
                self.assertIsNotNone(new_stop)
                self.assertGreaterEqual(new_stop + 1e-12, stop)
                stop = new_stop

    def test_short_stop_never_rises(self):
        policy = ProtectiveStopPolicy(use_trailing_stop=True, trailing_atr_multiple=3.0)
        rng = random.Random(7)
        stop = 110.0
        for _ in range(200):
            extreme = rng.uniform(50.0, 100.0)
            atr = rng.uniform(0.1, 30.0)
            new_stop = update_trailing_stop(
                side="short", current_stop=stop, initial_stop=110.0,
                extreme_since_fill=extreme, atr=atr, policy=policy,
            )
            self.assertLessEqual(new_stop - 1e-12, stop)
            stop = new_stop

    def test_chandelier_level_is_hand_checkable(self):
        policy = ProtectiveStopPolicy(use_trailing_stop=True, trailing_atr_multiple=3.0)
        # highest high 130, ATR 4 -> 130 - 12 = 118, above the old stop of 95.
        self.assertAlmostEqual(
            update_trailing_stop(
                side="buy", current_stop=95.0, initial_stop=90.0,
                extreme_since_fill=130.0, atr=4.0, policy=policy,
            ),
            118.0,
        )
        # A wider ATR would imply 130 - 45 = 85; the stop holds at 95 instead.
        self.assertAlmostEqual(
            update_trailing_stop(
                side="buy", current_stop=95.0, initial_stop=90.0,
                extreme_since_fill=130.0, atr=15.0, policy=policy,
            ),
            95.0,
        )

    def test_breakeven_only_applies_after_the_registered_r_and_adds_cost(self):
        policy = ProtectiveStopPolicy(
            use_trailing_stop=False, breakeven_after_r=2.0,
            breakeven_cost_buffer=0.5,
        )
        # entry 100, initial stop 90 -> 1R = 10. At 115 the trade is +1.5R.
        self.assertAlmostEqual(
            update_trailing_stop(
                side="buy", current_stop=90.0, initial_stop=90.0,
                extreme_since_fill=115.0, atr=None, policy=policy,
                entry_price=100.0,
            ),
            90.0,
        )
        # At 121 it is +2.1R, so the stop moves to entry + cost buffer.
        self.assertAlmostEqual(
            update_trailing_stop(
                side="buy", current_stop=90.0, initial_stop=90.0,
                extreme_since_fill=121.0, atr=None, policy=policy,
                entry_price=100.0,
            ),
            100.5,
        )


class TestNoLookahead(unittest.TestCase):
    def _frame(self, bars: int = 60) -> pd.DataFrame:
        index = pd.date_range("2021-01-01", periods=bars, freq="D", tz="UTC")
        closes = [100.0 + i for i in range(bars)]
        return pd.DataFrame(
            {
                "open": closes,
                "high": [value + 2 for value in closes],
                "low": [value - 2 for value in closes],
                "close": closes,
                "volume": [1000.0] * bars,
            },
            index=index,
        )

    def test_stop_at_bar_i_does_not_change_when_later_bars_change(self):
        strategy = TrendBreakoutStrategy()
        strategy.configure_stop_policy(
            ProtectiveStopPolicy(use_atr_initial_stop=True, use_trailing_stop=True)
        )
        frame = self._frame()
        i = 45
        signal = strategy.should_enter(
            "BTC/USDT", i, frame.iloc[: i + 1].copy(), MarketState.TREND_UP, Portfolio()
        )
        mutated = frame.copy()
        mutated.iloc[i + 1:, :] *= 5.0
        strategy_two = TrendBreakoutStrategy()
        strategy_two.configure_stop_policy(
            ProtectiveStopPolicy(use_atr_initial_stop=True, use_trailing_stop=True)
        )
        signal_two = strategy_two.should_enter(
            "BTC/USDT", i, mutated.iloc[: i + 1].copy(), MarketState.TREND_UP, Portfolio()
        )
        self.assertEqual(signal is None, signal_two is None)
        if signal is not None:
            self.assertAlmostEqual(signal["stop_loss"], signal_two["stop_loss"])

    def test_atr_only_uses_completed_bars(self):
        frame = self._frame()
        atr_full = Indicators.ATR(frame, 14)
        atr_prefix = Indicators.ATR(frame.iloc[:40], 14)
        self.assertAlmostEqual(float(atr_full.iat[39]), float(atr_prefix.iat[39]))


class TestFillRiskRecheck(unittest.TestCase):
    def _assess(self, fill_price: float, stop: float, qty: float, **kwargs):
        return evaluate_fill_risk(
            symbol="BTC/USDT", lot_id="LOT-1", side="long",
            fill_price=fill_price, protective_stop=stop, filled_qty=qty,
            equity_at_fill=100_000.0, base_risk_per_trade=0.02,
            policy=EntryRiskPolicy(**kwargs),
        )

    def test_within_budget_needs_no_action(self):
        # risk/unit = 5, qty 300 -> 1500 vs budget 2000.
        assessment = self._assess(105.0, 100.0, 300.0)
        self.assertFalse(assessment.breached)
        self.assertEqual(assessment.action, "none")
        self.assertAlmostEqual(assessment.actual_total_risk, 1500.0)
        self.assertAlmostEqual(assessment.risk_budget, 2000.0)

    def test_gap_through_the_open_triggers_a_named_resize(self):
        # The signal sized on close=105 with stop 100 (5/unit). The fill gapped
        # to 120, so risk/unit is 20 and 300 units risk 6000 vs a 2000 budget.
        assessment = self._assess(120.0, 100.0, 300.0)
        self.assertTrue(assessment.breached)
        self.assertEqual(assessment.action, "resize")
        self.assertEqual(assessment.reason, "gap_risk_resize")
        self.assertAlmostEqual(assessment.affordable_qty, 100.0)
        self.assertAlmostEqual(assessment.resize_qty, 200.0)

    def test_extreme_gap_closes_the_whole_lot_instead_of_leaving_dust(self):
        assessment = self._assess(1000.0, 100.0, 300.0)
        self.assertEqual(assessment.reason, "gap_risk_close_all")
        self.assertAlmostEqual(assessment.resize_qty, 300.0)

    def test_audit_only_records_the_breach_without_trading(self):
        assessment = self._assess(120.0, 100.0, 300.0, action="audit_only")
        self.assertTrue(assessment.breached)
        self.assertEqual(assessment.action, "audit_only")
        self.assertEqual(assessment.resize_qty, 0.0)

    def test_health_multiplier_shrinks_the_budget(self):
        assessment = evaluate_fill_risk(
            symbol="BTC/USDT", lot_id="LOT-1", side="long",
            fill_price=105.0, protective_stop=100.0, filled_qty=300.0,
            equity_at_fill=100_000.0, base_risk_per_trade=0.02,
            health_risk_multiplier=0.25,
        )
        self.assertAlmostEqual(assessment.risk_budget, 500.0)
        self.assertTrue(assessment.breached)

    def test_tolerance_absorbs_the_cost_wedge(self):
        # 2100 of risk against a 2000 budget is inside the 10% tolerance.
        assessment = self._assess(107.0, 100.0, 300.0)
        self.assertAlmostEqual(assessment.actual_total_risk, 2100.0)
        self.assertFalse(assessment.breached)


class TestEngineEmitsGapRiskResize(unittest.TestCase):
    def test_over_budget_fill_produces_a_named_reduce_order(self):
        from backtest.engine import BacktestEngine
        from backtest.execution_adapter import SimulatedExecutionAdapter
        from core.broker import Broker
        from core.risk import RiskManager

        portfolio = Portfolio(initial_capital=100_000.0)
        broker = Broker(portfolio)
        execution = SimulatedExecutionAdapter(broker)
        risk_manager = RiskManager(risk_per_trade=0.02)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")

        # A breakout sized on a 5-wide stop, filled 20 wide after a gap.
        portfolio.update_position(
            "BTC/USDT", qty_delta=300.0, price=120.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="ORD-1", stop_price=100.0,
        )

        engine = BacktestEngine()
        audit: list = []
        engine._recheck_entry_risk(
            portfolio=portfolio,
            execution=execution,
            strategies={},
            risk_manager=risk_manager,
            equity=100_000.0,
            prices={"BTC/USDT": 120.0},
            timestamp=timestamp,
            bar_index=7,
            policy=EntryRiskPolicy(),
            checked_lot_ids=set(),
            audit=audit,
        )

        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["breached"])
        self.assertEqual(audit[0]["action"], "resize")
        reduce_orders = [
            order for order in broker.pending_orders + broker.active_orders
            if order.exit_reason == "GapRiskResize"
        ]
        self.assertEqual(len(reduce_orders), 1)
        self.assertAlmostEqual(reduce_orders[0].qty, 200.0)
        self.assertEqual(reduce_orders[0].side, "sell")

    def test_each_lot_is_only_rechecked_once(self):
        from backtest.engine import BacktestEngine
        from backtest.execution_adapter import SimulatedExecutionAdapter
        from core.broker import Broker
        from core.risk import RiskManager

        portfolio = Portfolio(initial_capital=100_000.0)
        broker = Broker(portfolio)
        execution = SimulatedExecutionAdapter(broker)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        portfolio.update_position(
            "BTC/USDT", qty_delta=300.0, price=120.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="ORD-1", stop_price=100.0,
        )
        engine = BacktestEngine()
        audit: list = []
        seen: set = set()
        for _ in range(3):
            engine._recheck_entry_risk(
                portfolio=portfolio, execution=execution, strategies={},
                risk_manager=RiskManager(risk_per_trade=0.02),
                equity=100_000.0, prices={"BTC/USDT": 120.0},
                timestamp=timestamp, bar_index=7, policy=EntryRiskPolicy(),
                checked_lot_ids=seen, audit=audit,
            )
        self.assertEqual(len(audit), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
