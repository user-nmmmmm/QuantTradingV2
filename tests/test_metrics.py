import math
import unittest
from types import SimpleNamespace

import pandas as pd
from pandas.testing import assert_frame_equal

from core.metrics import (
    benjamini_hochberg,
    bootstrap_return_distribution,
    calculate_attribution,
    calculate_benchmark_comparison,
    calculate_cost_sensitivity,
    calculate_drawdown,
    calculate_drawdown_events,
    calculate_equity_metrics,
    calculate_exposure,
    calculate_profit_factor,
    calculate_r_multiple_stats,
    calculate_rolling_returns,
    calculate_segment_returns,
    calculate_sharpe,
    calculate_signal_funnel,
    calculate_trade_quality,
    infer_periods_per_year,
    monte_carlo_trade_sequence,
    monthly_returns,
    train_test_split_returns,
    walk_forward_windows,
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


class TestBM4CostSensitivity(unittest.TestCase):
    def test_empty_trades_are_insufficient(self):
        result = calculate_cost_sensitivity([])
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["grid"], [])

    def test_grid_values_and_monotonic_non_increasing_net_pnl(self):
        trades = [
            {"gross_pnl": 100.0, "commission": 10.0, "slippage": 5.0},
            {"gross_pnl": -20.0, "commission": 2.0, "slippage": 1.0},
        ]
        result = calculate_cost_sensitivity(
            trades, commission_multipliers=(1.0, 2.0), slippage_multipliers=(1.0, 2.0),
        )
        self.assertAlmostEqual(result["gross_pnl"], 80.0)
        self.assertAlmostEqual(result["baseline_commission"], 12.0)
        self.assertAlmostEqual(result["baseline_slippage"], 6.0)
        self.assertAlmostEqual(result["baseline_net_pnl"], 80.0 - 12.0 - 6.0)

        by_key = {
            (row["commission_multiplier"], row["slippage_multiplier"]): row["net_pnl"]
            for row in result["grid"]
        }
        self.assertAlmostEqual(by_key[(1.0, 1.0)], 80.0 - 12.0 - 6.0)
        self.assertAlmostEqual(by_key[(2.0, 2.0)], 80.0 - 24.0 - 12.0)
        # Raising either multiplier can only reduce or hold net PnL.
        self.assertLessEqual(by_key[(2.0, 1.0)], by_key[(1.0, 1.0)])
        self.assertLessEqual(by_key[(1.0, 2.0)], by_key[(1.0, 1.0)])
        self.assertLessEqual(by_key[(2.0, 2.0)], by_key[(2.0, 1.0)])


class TestBM5Attribution(unittest.TestCase):
    def test_group_breakdowns_reconcile_exactly_to_total(self):
        trades = [
            {"net_pnl": 100.0, "strategy": "A", "symbol": "BTC/USDT", "exit_time": "2024-01-15"},
            {"net_pnl": -40.0, "strategy": "A", "symbol": "ETH/USDT", "exit_time": "2024-01-20"},
            {"net_pnl": 25.0, "strategy": "B", "symbol": "BTC/USDT", "exit_time": "2024-02-01"},
            {"net_pnl": 10.0},  # no strategy/symbol/exit_time at all
        ]
        result = calculate_attribution(trades)
        total = result["total_net_pnl"]
        self.assertAlmostEqual(total, 95.0)
        self.assertAlmostEqual(sum(result["by_strategy"].values()), total)
        self.assertAlmostEqual(sum(result["by_symbol"].values()), total)
        self.assertAlmostEqual(sum(result["by_month"].values()), total)
        self.assertIn("UNKNOWN", result["by_strategy"])
        self.assertAlmostEqual(result["by_month"]["2024-01"], 60.0)
        self.assertAlmostEqual(result["by_month"]["2024-02"], 25.0)


class TestBM6BenchmarkRollingSegment(unittest.TestCase):
    def test_benchmark_comparison_excess_return_and_correlation(self):
        index = pd.date_range("2024-01-01", periods=5, freq="D")
        strategy_growth = [1.10, 1.05, 1.08, 1.12]
        # Benchmark returns are exactly half of strategy returns each period
        # -> a perfect positive linear relationship (correlation == 1.0).
        benchmark_growth = [1 + (g - 1) / 2 for g in strategy_growth]

        def _compound(growth):
            values = [100.0]
            for g in growth:
                values.append(values[-1] * g)
            return values

        equity = pd.Series(_compound(strategy_growth), index=index)
        benchmark = pd.Series(_compound(benchmark_growth), index=index)
        result = calculate_benchmark_comparison(equity, benchmark)
        self.assertEqual(result["status"], "ok")
        expected_strategy_return = equity.iloc[-1] / equity.iloc[0] - 1
        expected_benchmark_return = benchmark.iloc[-1] / benchmark.iloc[0] - 1
        self.assertAlmostEqual(result["strategy_return"], expected_strategy_return)
        self.assertAlmostEqual(result["benchmark_return"], expected_benchmark_return)
        self.assertAlmostEqual(
            result["excess_return"], result["strategy_return"] - result["benchmark_return"],
        )
        self.assertAlmostEqual(result["correlation"], 1.0, places=6)

    def test_benchmark_comparison_insufficient_without_overlap(self):
        equity = pd.Series([100, 110], index=pd.date_range("2024-01-01", periods=2))
        benchmark = pd.Series([100], index=pd.date_range("2024-03-01", periods=1))
        result = calculate_benchmark_comparison(equity, benchmark)
        self.assertEqual(result["status"], "insufficient")

    def test_rolling_returns_are_trailing_only(self):
        index = pd.date_range("2024-01-01", periods=5, freq="D")
        equity = pd.Series([100, 110, 121, 100, 150], index=index)
        rolling = calculate_rolling_returns(equity, window=2)
        # First two observations have no full trailing window and are absent.
        self.assertEqual(len(rolling), 3)
        self.assertAlmostEqual(rolling.iloc[0], 121 / 100 - 1)
        self.assertAlmostEqual(rolling.iloc[-1], 150 / 121 - 1)

    def test_segment_returns_split_into_equal_chunks(self):
        index = pd.date_range("2024-01-01", periods=9, freq="D")
        equity = pd.Series(range(100, 109), index=index)
        segments = calculate_segment_returns(equity, segments=4)
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0]["segment"], 1)
        self.assertEqual(segments[-1]["end"], index[-1])


class TestBM7RMultipleAndSQN(unittest.TestCase):
    def test_r_multiple_stats_and_sqn(self):
        trades = [
            {"net_pnl": 200.0, "initial_risk": 100.0, "mae": -50.0, "mfe": 220.0},
            {"net_pnl": -100.0, "initial_risk": 100.0, "mae": -110.0, "mfe": 10.0},
            {"net_pnl": 150.0, "initial_risk": 50.0, "mae": -20.0, "mfe": 160.0},
            {"net_pnl": 30.0},  # no initial_risk: excluded from R-multiple stats
        ]
        result = calculate_r_multiple_stats(trades)
        self.assertEqual(result["excluded_no_initial_risk"], 1)
        self.assertEqual(result["r_multiple"]["status"], "ok")
        self.assertEqual(result["r_multiple"]["sample_size"], 3)
        # R-multiples: 200/100=2, -100/100=-1, 150/50=3
        self.assertAlmostEqual(result["r_multiple"]["mean_r"], (2 - 1 + 3) / 3)
        self.assertIsNotNone(result["r_multiple"]["sqn"])
        self.assertEqual(result["mae"]["sample_size"], 3)
        self.assertEqual(result["mfe"]["sample_size"], 3)

    def test_r_multiple_stats_insufficient_without_risk_data(self):
        result = calculate_r_multiple_stats([{"net_pnl": 10.0}])
        self.assertEqual(result["excluded_no_initial_risk"], 1)
        self.assertEqual(result["r_multiple"]["status"], "insufficient")
        self.assertEqual(result["mae"]["status"], "insufficient")


class TestBM8Robustness(unittest.TestCase):
    def test_train_test_split_is_chronological_not_shuffled(self):
        returns = pd.Series(range(10))
        split = train_test_split_returns(returns, train_fraction=0.7)
        self.assertEqual(len(split["train"]), 7)
        self.assertEqual(len(split["test"]), 3)
        self.assertEqual(list(split["train"]), list(range(7)))
        self.assertEqual(list(split["test"]), list(range(7, 10)))

    def test_walk_forward_windows_are_non_overlapping_and_non_anticipating(self):
        windows = walk_forward_windows(n_periods=20, train_size=10, test_size=5)
        self.assertEqual(len(windows), 2)
        first, second = windows
        self.assertEqual(first, {"train_start": 0, "train_end": 10, "test_start": 10, "test_end": 15})
        # Within each window, test starts exactly where its own train ends
        # (no gap, no lookahead into data the train window didn't see).
        self.assertEqual(first["test_start"], first["train_end"])
        self.assertEqual(second["test_start"], second["train_end"])
        # Consecutive test windows are contiguous and never overlap.
        self.assertEqual(second["test_start"], first["test_end"])

    def test_bootstrap_return_distribution_recovers_known_mean(self):
        returns = [0.01] * 50  # constant returns: bootstrap mean must equal 0.01 exactly
        result = bootstrap_return_distribution(returns, statistic="mean", n_samples=200)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], 0.01)
        self.assertAlmostEqual(result["lower"], 0.01)
        self.assertAlmostEqual(result["upper"], 0.01)

    def test_bootstrap_return_distribution_is_reproducible_with_fixed_seed(self):
        returns = [0.02, -0.01, 0.03, -0.02, 0.01, 0.015, -0.005, 0.025]
        first = bootstrap_return_distribution(returns, seed=7)
        second = bootstrap_return_distribution(returns, seed=7)
        self.assertEqual(first, second)

    def test_monte_carlo_preserves_realized_total_regardless_of_ordering(self):
        pnls = [10.0, -5.0, 20.0, -15.0, 8.0]
        result = monte_carlo_trade_sequence(pnls, n_simulations=500)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["realized_final_pnl"], sum(pnls))
        self.assertAlmostEqual(result["final_pnl_mean"], sum(pnls))
        # Simulated max-drawdown paths only reorder existing trades, so no
        # simulated drawdown can be worse than the sum of all losing trades.
        worst_possible = sum(p for p in pnls if p < 0)
        self.assertGreaterEqual(result["max_drawdown_p05"], worst_possible - 1e-9)

    def test_benjamini_hochberg_rejects_only_the_small_p_values(self):
        p_values = [0.001, 0.02, 0.03, 0.5, 0.8]
        result = benjamini_hochberg(p_values, fdr=0.05)
        self.assertEqual(result["sample_size"], 5)
        self.assertTrue(result["rejected"][0])
        self.assertFalse(result["rejected"][3])
        self.assertFalse(result["rejected"][4])
        # Adjusted p-values are never smaller than the raw p-values.
        for raw, adjusted in zip(p_values, result["adjusted_p_values"]):
            self.assertGreaterEqual(adjusted, raw - 1e-12)


if __name__ == "__main__":
    unittest.main()
