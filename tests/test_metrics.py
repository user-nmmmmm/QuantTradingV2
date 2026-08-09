import math
import unittest
from types import SimpleNamespace

import pandas as pd
from pandas.testing import assert_frame_equal

from core.metrics import (
    calculate_drawdown,
    calculate_drawdown_events,
    calculate_equity_metrics,
    calculate_exposure,
    calculate_profit_factor,
    calculate_sharpe,
    calculate_signal_funnel,
    calculate_trade_quality,
    infer_periods_per_year,
    monthly_returns,
)


class TestP0Metrics(unittest.TestCase):
    def test_periods_per_year_for_supported_frequencies_and_irregular_series(self):
        cases = {"1D": 365.25, "4h": 365.25 * 6, "1h": 365.25 * 24,
                 "15min": 365.25 * 96}
        for frequency, expected in cases.items():
            with self.subTest(frequency=frequency):
                index = pd.date_range("2024-01-01", periods=10, freq=frequency, tz="UTC")
                self.assertAlmostEqual(infer_periods_per_year(index), expected)
        irregular = pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-04", "2024-01-05"])
        self.assertAlmostEqual(infer_periods_per_year(irregular), 365.25)

    def test_monthly_return_includes_month_boundary(self):
        equity = pd.Series([100.0, 110.0, 121.0, 133.1], index=pd.to_datetime([
            "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29"]))
        result = monthly_returns(equity)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result.iloc[0], 0.21)

    def test_calculation_does_not_mutate_input(self):
        curve = pd.DataFrame({"equity": [100.0, 101.0, 99.0], "cash": [100, 90, 80]},
                             index=pd.date_range("2024-01-01", periods=3, freq="D"))
        before = curve.copy(deep=True)
        calculate_equity_metrics(curve)
        assert_frame_equal(curve, before)
        self.assertNotIn("month", curve.columns)

    def test_sharpe_distinguishes_insufficient_and_undefined(self):
        self.assertEqual(calculate_sharpe(pd.Series([0.1]), 365.25)["status"], "insufficient")
        constant = calculate_sharpe(pd.Series([0.01, 0.01, 0.01]), 365.25)
        self.assertIsNone(constant["value"])
        self.assertEqual(constant["status"], "undefined")

    def test_profit_factor_never_returns_infinity(self):
        no_losses = calculate_profit_factor([1.0, 2.0])
        self.assertIsNone(no_losses["value"])
        self.assertEqual(no_losses["status"], "undefined")
        low_sample = calculate_profit_factor([2.0, -1.0])
        self.assertEqual(low_sample["status"], "insufficient")
        self.assertEqual(low_sample["loss_count"], 1)
        self.assertTrue(math.isfinite(low_sample["value"]))
        self.assertIsNotNone(low_sample["lower"])
        self.assertIsNotNone(low_sample["upper"])

    def test_drawdown_dates_duration_recovery_and_open_state(self):
        index = pd.date_range("2024-01-01", periods=6, freq="D")
        recovered = calculate_drawdown(pd.Series([100, 120, 90, 100, 120, 130], index=index))
        self.assertEqual(recovered["peak"], index[1])
        self.assertEqual(recovered["trough"], index[2])
        self.assertEqual(recovered["recovery"], index[4])
        self.assertEqual(recovered["duration_periods"], 3)
        self.assertEqual(recovered["recovery_periods"], 2)
        self.assertFalse(recovered["is_open"])
        opened = calculate_drawdown(pd.Series([100, 120, 90, 80], index=index[:4]))
        self.assertTrue(opened["is_open"])
        self.assertIsNone(opened["recovery"])

    def test_drawdown_events_returns_empty_for_monotonic_equity(self):
        index = pd.date_range("2024-01-01", periods=4, freq="D")
        events = calculate_drawdown_events(pd.Series([100, 110, 120, 130], index=index))
        self.assertEqual(events, [])

    def test_drawdown_events_enumerates_every_separate_episode(self):
        index = pd.date_range("2024-01-01", periods=8, freq="D")
        equity = pd.Series([100, 120, 90, 100, 130, 80, 140, 140], index=index)
        events = calculate_drawdown_events(equity)
        self.assertEqual(len(events), 2)

        first, second = events
        self.assertEqual(first["peak"], index[1])
        self.assertEqual(first["trough"], index[2])
        self.assertEqual(first["recovery"], index[4])
        self.assertAlmostEqual(first["depth_pct"], -0.25)
        self.assertFalse(first["is_open"])

        self.assertEqual(second["peak"], index[4])
        self.assertEqual(second["trough"], index[5])
        self.assertEqual(second["recovery"], index[6])
        self.assertAlmostEqual(second["depth_pct"], (80 - 130) / 130)
        self.assertFalse(second["is_open"])

    def test_drawdown_events_last_episode_can_stay_open(self):
        index = pd.date_range("2024-01-01", periods=4, freq="D")
        events = calculate_drawdown_events(pd.Series([100, 120, 90, 80], index=index))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_open"])
        self.assertIsNone(events[0]["recovery"])
        self.assertEqual(events[0]["trough"], index[3])
        self.assertAlmostEqual(events[0]["depth_pct"], -1 / 3)

    def test_drawdown_events_min_depth_filters_shallow_episodes(self):
        index = pd.date_range("2024-01-01", periods=8, freq="D")
        equity = pd.Series([100, 120, 90, 100, 130, 80, 140, 140], index=index)
        deep_only = calculate_drawdown_events(equity, min_depth_pct=0.30)
        self.assertEqual(len(deep_only), 1)
        self.assertEqual(deep_only[0]["peak"], index[4])

    def test_drawdown_events_worst_episode_matches_calculate_drawdown(self):
        index = pd.date_range("2024-01-01", periods=8, freq="D")
        equity = pd.Series([100, 120, 90, 100, 130, 80, 140, 140], index=index)
        summary = calculate_drawdown(equity)
        events = calculate_drawdown_events(equity)
        worst = min(events, key=lambda event: event["depth_pct"])
        self.assertEqual(worst["peak"], summary["peak"])
        self.assertEqual(worst["trough"], summary["trough"])
        self.assertEqual(worst["recovery"], summary["recovery"])
        self.assertAlmostEqual(worst["depth_pct"], summary["max_pct"])

    def test_trade_quality_returns_insufficient_for_no_trades(self):
        result = calculate_trade_quality([])
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["sample_size"], 0)
        self.assertIsNone(result["win_rate"])
        self.assertEqual(result["holding_duration_hours"]["status"], "insufficient")
        self.assertEqual(result["by_strategy"], {})

    def _sample_trades(self):
        return [
            {"net_pnl": 100, "strategy": "A", "symbol": "BTC/USDT",
             "entry_time": "2024-01-01T00:00:00Z", "exit_time": "2024-01-01T06:00:00Z"},
            {"net_pnl": -40, "strategy": "A", "symbol": "BTC/USDT",
             "entry_time": "2024-01-02T00:00:00Z", "exit_time": "2024-01-02T12:00:00Z"},
            {"net_pnl": 50, "strategy": "B", "symbol": "ETH/USDT",
             "entry_time": "2024-01-03T00:00:00Z", "exit_time": "2024-01-03T03:00:00Z"},
            {"net_pnl": -20, "strategy": "B", "symbol": "ETH/USDT"},
        ]

    def test_trade_quality_win_loss_and_expectancy(self):
        result = calculate_trade_quality(self._sample_trades(), minimum_samples=4)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["win_count"], 2)
        self.assertEqual(result["loss_count"], 2)
        self.assertAlmostEqual(result["win_rate"], 0.5)
        self.assertAlmostEqual(result["avg_win"], 75.0)
        self.assertAlmostEqual(result["avg_loss"], -30.0)
        self.assertAlmostEqual(result["expectancy"], 22.5)
        self.assertAlmostEqual(result["profit_factor"], 150 / 60)

    def test_trade_quality_matches_calculate_profit_factor_directly(self):
        trades = self._sample_trades()
        result = calculate_trade_quality(trades, minimum_samples=4)
        direct = calculate_profit_factor(
            [t["net_pnl"] for t in trades], minimum_samples=4,
        )
        self.assertEqual(result["profit_factor"], direct["value"])

    def test_trade_quality_holding_duration_skips_missing_timestamps(self):
        result = calculate_trade_quality(self._sample_trades())
        duration = result["holding_duration_hours"]
        self.assertEqual(duration["status"], "ok")
        # Only 3 of the 4 sample trades carry entry/exit timestamps.
        self.assertEqual(duration["sample_size"], 3)
        self.assertAlmostEqual(duration["mean"], (6 + 12 + 3) / 3)
        self.assertAlmostEqual(duration["min"], 3.0)
        self.assertAlmostEqual(duration["max"], 12.0)

    def test_trade_quality_breaks_down_by_strategy_and_symbol(self):
        result = calculate_trade_quality(self._sample_trades())
        self.assertEqual(set(result["by_strategy"]), {"A", "B"})
        self.assertEqual(result["by_strategy"]["A"]["sample_size"], 2)
        self.assertAlmostEqual(result["by_strategy"]["A"]["net_pnl"], 60.0)
        self.assertEqual(set(result["by_symbol"]), {"BTC/USDT", "ETH/USDT"})
        self.assertAlmostEqual(result["by_symbol"]["ETH/USDT"]["net_pnl"], 30.0)

    def test_exposure_computes_gross_net_and_equity_pct(self):
        t1, t2, t3 = "2024-01-01", "2024-01-02", "2024-01-03"
        positions = {
            t1: {"BTC/USDT": 1.0, "ETH/USDT": -2.0},
            t2: {"BTC/USDT": 0.0, "ETH/USDT": -1.0},
            t3: {"BTC/USDT": 2.0},  # no matching price at t3
        }
        prices = {
            t1: {"BTC/USDT": 100.0, "ETH/USDT": 50.0},
            t2: {"ETH/USDT": 60.0},
        }
        equity = {t1: 1000.0, t2: 900.0, t3: 800.0}

        frame = calculate_exposure(positions, prices, equity)

        self.assertAlmostEqual(frame.loc[t1, "gross_exposure"], 200.0)
        self.assertAlmostEqual(frame.loc[t1, "net_exposure"], 0.0)
        self.assertEqual(frame.loc[t1, "priced_symbols"], 2)
        self.assertAlmostEqual(frame.loc[t1, "gross_exposure_pct_equity"], 0.2)

        self.assertAlmostEqual(frame.loc[t2, "gross_exposure"], 60.0)
        self.assertAlmostEqual(frame.loc[t2, "net_exposure"], -60.0)
        self.assertEqual(frame.loc[t2, "priced_symbols"], 1)

        # Flat/unpriced positions contribute nothing but don't raise or
        # silently mismeasure a book that actually holds risk.
        self.assertAlmostEqual(frame.loc[t3, "gross_exposure"], 0.0)
        self.assertEqual(frame.loc[t3, "priced_symbols"], 0)

    def test_exposure_without_equity_leaves_pct_columns_none(self):
        frame = calculate_exposure(
            {"t1": {"BTC/USDT": 1.0}}, {"t1": {"BTC/USDT": 100.0}},
        )
        self.assertIsNone(frame.loc["t1", "gross_exposure_pct_equity"])

    def test_exposure_handles_empty_input(self):
        frame = calculate_exposure({}, {})
        self.assertTrue(frame.empty)

    def _funnel_event(self, correlation_id, event_type, **payload):
        return SimpleNamespace(
            correlation_id=correlation_id, event_type=event_type, payload=payload,
        )

    def test_signal_funnel_counts_each_stage_independently(self):
        events = [
            # Chain A: reaches every stage.
            self._funnel_event("A", "risk_decision", approved=True),
            self._funnel_event("A", "order_intent"),
            self._funnel_event("A", "order", status="accepted"),
            self._funnel_event("A", "fill"),
            # Chain B: risk-rejected, goes no further.
            self._funnel_event("B", "risk_decision", approved=False),
            # Chain C: approved and an order was created, but never accepted
            # by the venue (e.g. rejected on submission) or filled.
            self._funnel_event("C", "risk_decision", approved=True),
            self._funnel_event("C", "order_intent"),
            self._funnel_event("C", "order", status="rejected"),
        ]
        result = calculate_signal_funnel(events)

        self.assertEqual(result["total_correlation_chains"], 3)
        stages = result["stages"]
        self.assertEqual(stages["risk_evaluated"]["count"], 3)
        self.assertEqual(stages["risk_approved"]["count"], 2)
        self.assertEqual(stages["order_created"]["count"], 2)
        self.assertEqual(stages["order_accepted"]["count"], 1)
        self.assertEqual(stages["filled"]["count"], 1)

        self.assertAlmostEqual(stages["risk_evaluated"]["pct_of_total"], 1.0)
        self.assertAlmostEqual(stages["filled"]["pct_of_total"], 1 / 3)
        self.assertIsNone(stages["risk_evaluated"]["pct_of_previous_stage"])
        self.assertAlmostEqual(stages["risk_approved"]["pct_of_previous_stage"], 2 / 3)
        self.assertAlmostEqual(stages["order_created"]["pct_of_previous_stage"], 1.0)
        self.assertAlmostEqual(stages["filled"]["pct_of_previous_stage"], 1.0)

    def test_signal_funnel_handles_empty_and_keyless_events(self):
        self.assertEqual(calculate_signal_funnel([])["total_correlation_chains"], 0)
        keyless = SimpleNamespace(correlation_id=None, event_type="fill", payload={})
        result = calculate_signal_funnel([keyless])
        self.assertEqual(result["total_correlation_chains"], 0)


if __name__ == "__main__":
    unittest.main()
