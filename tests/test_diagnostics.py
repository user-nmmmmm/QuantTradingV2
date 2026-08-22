"""core/diagnostics.py — result-trustworthiness metrics.

Each test pins the defect the metric exists to expose, so a regression that
re-hides one of them fails here rather than silently passing as "profitable".
"""
import unittest

import pandas as pd

from core.diagnostics import (
    INERT_EXIT_RATIO_THRESHOLD,
    build_diagnostics,
    calculate_calendar_returns,
    calculate_exit_attribution,
    calculate_lifecycle_coverage,
    calculate_pnl_concentration,
    calculate_streaks,
)


def _trade(net_pnl, *, strategy="S", exit_strategy=None, reason="signal",
           exit_time="2024-01-01", symbol="BTC/USDT"):
    return {
        "net_pnl": net_pnl,
        "gross_pnl": net_pnl,
        "strategy": strategy,
        "exit_strategy": strategy if exit_strategy is None else exit_strategy,
        "exit_reason": reason,
        "symbol": symbol,
        "entry_time": pd.Timestamp(exit_time) - pd.Timedelta(days=1),
        "exit_time": pd.Timestamp(exit_time),
    }


class TestPnlConcentration(unittest.TestCase):
    def test_flags_profit_carried_by_a_few_trades(self):
        # One huge winner plus many small losers: profit is entirely the outlier.
        trades = [_trade(1000.0)] + [_trade(-10.0) for _ in range(20)]

        result = calculate_pnl_concentration(trades, top_n=(1, 10))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample_size"], 21)
        self.assertAlmostEqual(result["total_net_pnl"], 800.0)
        self.assertAlmostEqual(result["top_n"]["1"]["contribution"], 1000.0)
        self.assertGreater(result["top_n"]["1"]["share_of_total"], 1.0)
        self.assertAlmostEqual(result["top_n"]["1"]["total_excluding"], -200.0)

    def test_evenly_spread_profit_has_low_hhi(self):
        even = [_trade(10.0) for _ in range(10)]
        spiky = [_trade(100.0)] + [_trade(0.1) for _ in range(9)]

        self.assertLess(
            calculate_pnl_concentration(even)["profit_hhi"],
            calculate_pnl_concentration(spiky)["profit_hhi"],
        )
        self.assertAlmostEqual(calculate_pnl_concentration(even)["profit_hhi"], 0.1)

    def test_share_is_none_when_total_is_not_positive(self):
        """A negative total would flip the sign and render as a bogus percent."""
        result = calculate_pnl_concentration([_trade(-5.0), _trade(-15.0)])

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["top_n"]["1"]["share_of_total"])
        self.assertIsNone(result["profit_hhi"])

    def test_empty_input_is_insufficient_not_zero(self):
        result = calculate_pnl_concentration([])

        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["total_net_pnl"])


class TestExitAttribution(unittest.TestCase):
    def test_detects_strategy_whose_exit_rule_never_fires(self):
        """The TrendBreakout case: 91 entries, 0 self-exits, all router-closed."""
        trades = [
            _trade(5.0, strategy="TrendBreakout", exit_strategy="Router",
                   reason="StateSwitch")
            for _ in range(20)
        ]

        result = calculate_exit_attribution(trades)

        self.assertEqual(result["own_exit_ratio"], 0.0)
        self.assertIn("TrendBreakout", result["inert_exit_logic"])
        entry = result["by_strategy"]["TrendBreakout"]
        self.assertEqual(entry["own_exits"], 0)
        self.assertEqual(entry["external_exits"], 20)

    def test_healthy_strategy_is_not_flagged(self):
        trades = [_trade(5.0, strategy="Healthy", reason="target") for _ in range(20)]

        result = calculate_exit_attribution(trades)

        self.assertEqual(result["own_exit_ratio"], 1.0)
        self.assertEqual(result["inert_exit_logic"], [])

    def test_missing_closer_is_not_counted_as_a_self_exit(self):
        """Unknown provenance must not be scored as healthy — that hides the bug."""
        trades = [_trade(1.0, strategy="S") for _ in range(10)]
        for trade in trades:
            trade["exit_strategy"] = None

        result = calculate_exit_attribution(trades)

        self.assertEqual(result["own_exit_ratio"], 0.0)
        self.assertIn("S", result["inert_exit_logic"])

    def test_threshold_boundary_is_not_flagged(self):
        # Exactly at the threshold counts as acceptable (strictly-less flags).
        own = int(INERT_EXIT_RATIO_THRESHOLD * 20)
        trades = [_trade(1.0, strategy="S") for _ in range(own)]
        trades += [
            _trade(1.0, strategy="S", exit_strategy="Router")
            for _ in range(20 - own)
        ]

        result = calculate_exit_attribution(trades)

        self.assertAlmostEqual(
            result["by_strategy"]["S"]["own_exit_ratio"], INERT_EXIT_RATIO_THRESHOLD
        )
        self.assertEqual(result["inert_exit_logic"], [])


class TestLifecycleCoverage(unittest.TestCase):
    def test_flags_strategy_blind_to_its_own_closures(self):
        """TrendBreakdown saw 1 of 48 round trips, disabling its health gate."""
        trades = [_trade(1.0, strategy="TrendBreakdown") for _ in range(48)]

        result = calculate_lifecycle_coverage(trades, {"TrendBreakdown": 1})

        self.assertIn("TrendBreakdown", result["blind_strategies"])
        self.assertAlmostEqual(
            result["by_strategy"]["TrendBreakdown"]["coverage"], 1 / 48
        )

    def test_full_coverage_is_not_flagged(self):
        trades = [_trade(1.0, strategy="Good") for _ in range(5)]

        result = calculate_lifecycle_coverage(trades, {"Good": 5})

        self.assertEqual(result["blind_strategies"], [])
        self.assertEqual(result["overall_coverage"], 1.0)


class TestCalendarReturns(unittest.TestCase):
    def test_reports_per_year_returns_including_the_first_year(self):
        index = pd.date_range("2022-01-01", "2024-12-31", freq="D")
        # 100 -> 200 -> 300 -> 150 across three calendar years.
        equity = pd.Series(100.0, index=index)
        equity[equity.index.year == 2022] = pd.Series(
            [100.0] * (equity.index.year == 2022).sum()
        ).values
        equity[equity.index.year == 2023] = 200.0
        equity[equity.index.year == 2024] = 150.0
        equity.iloc[-1] = 150.0

        result = calculate_calendar_returns(equity)

        self.assertEqual(result["status"], "ok")
        periods = {entry["period"]: entry for entry in result["periods"]}
        self.assertIn("2022", periods, "first year must not be dropped by pct_change")
        self.assertIn("2024", periods)
        self.assertEqual(result["negative_periods"], 1)

    def test_counts_trades_into_the_period_they_closed_in(self):
        index = pd.date_range("2023-01-01", "2024-12-31", freq="D")
        equity = pd.Series(range(len(index)), index=index, dtype=float) + 100.0
        trades = [
            _trade(10.0, exit_time="2023-06-01"),
            _trade(-5.0, exit_time="2024-06-01"),
            _trade(7.0, exit_time="2024-07-01"),
        ]

        result = calculate_calendar_returns(equity, trades)
        periods = {entry["period"]: entry for entry in result["periods"]}

        self.assertEqual(periods["2023"]["trades"], 1)
        self.assertEqual(periods["2024"]["trades"], 2)
        self.assertAlmostEqual(periods["2024"]["net_pnl"], 2.0)

    def test_empty_equity_is_insufficient(self):
        self.assertEqual(
            calculate_calendar_returns(pd.Series(dtype=float))["status"],
            "insufficient",
        )


class TestStreaks(unittest.TestCase):
    def test_counts_longest_runs_in_chronological_order(self):
        pnls = [1.0, 2.0, 3.0, -1.0, -2.0, -3.0, -4.0, 5.0]
        trades = [
            _trade(pnl, exit_time=f"2024-01-{day:02d}")
            for day, pnl in enumerate(pnls, start=1)
        ]

        result = calculate_streaks(trades)

        self.assertEqual(result["max_win_streak"], 3)
        self.assertEqual(result["max_loss_streak"], 4)
        self.assertAlmostEqual(result["max_win_streak_pnl"], 6.0)
        self.assertAlmostEqual(result["max_loss_streak_pnl"], -10.0)

    def test_input_order_does_not_change_the_result(self):
        pnls = [1.0, 2.0, 3.0, -1.0, -2.0, -3.0, -4.0, 5.0]
        trades = [
            _trade(pnl, exit_time=f"2024-01-{day:02d}")
            for day, pnl in enumerate(pnls, start=1)
        ]

        self.assertEqual(
            calculate_streaks(trades), calculate_streaks(list(reversed(trades)))
        )

    def test_breakeven_breaks_both_runs(self):
        trades = [
            _trade(pnl, exit_time=f"2024-02-{day:02d}")
            for day, pnl in enumerate([1.0, 0.0, 1.0], start=1)
        ]

        self.assertEqual(calculate_streaks(trades)["max_win_streak"], 1)


class TestClosedTradeExitProvenance(unittest.TestCase):
    """Reconstruction must tag every closed trade with who closed it and why.

    Missing provenance silently degrades exit attribution into "unknown", which
    is indistinguishable from a healthy result. All four fill sides must carry
    it — the short-covering path was missed once already.
    """

    def _fills(self):
        base = {"commission": 1.0, "slip": 0.5}
        return pd.DataFrame([
            # Long round trip closed by the strategy's own rule.
            {"symbol": "BTC/USDT", "side": "buy", "qty": 1.0, "fill_price": 100.0,
             "strategy_id": "S", "exit_reason": "signal",
             "fill_time": pd.Timestamp("2024-01-01"), **base},
            {"symbol": "BTC/USDT", "side": "sell", "qty": 1.0, "fill_price": 110.0,
             "strategy_id": "S", "exit_reason": "target",
             "fill_time": pd.Timestamp("2024-01-02"), **base},
            # Short round trip force-closed by the router via `cover`.
            {"symbol": "ETH/USDT", "side": "short", "qty": 1.0, "fill_price": 100.0,
             "strategy_id": "S", "exit_reason": "signal",
             "fill_time": pd.Timestamp("2024-01-03"), **base},
            {"symbol": "ETH/USDT", "side": "cover", "qty": 1.0, "fill_price": 90.0,
             "strategy_id": "Router", "exit_reason": "StateSwitch",
             "fill_time": pd.Timestamp("2024-01-04"), **base},
        ])

    def test_every_closed_trade_carries_exit_reason_and_closer(self):
        from backtest.reporting import ReportGenerator
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            closed = ReportGenerator(directory)._reconstruct_closed_trades(self._fills())

        self.assertEqual(len(closed), 2)
        for trade in closed:
            self.assertIsNotNone(
                trade.get("exit_reason"),
                f"exit_reason missing for a trade closed on {trade.get('exit_time')}",
            )
            self.assertIsNotNone(trade.get("exit_strategy"))

        by_symbol = {trade["symbol"]: trade for trade in closed}
        self.assertEqual(by_symbol["ETH/USDT"]["exit_reason"], "StateSwitch")
        self.assertEqual(by_symbol["ETH/USDT"]["exit_strategy"], "Router")
        self.assertEqual(by_symbol["BTC/USDT"]["exit_strategy"], "S")

    def test_router_closed_short_is_reported_as_an_external_exit(self):
        from backtest.reporting import ReportGenerator
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            closed = ReportGenerator(directory)._reconstruct_closed_trades(self._fills())

        attribution = calculate_exit_attribution(closed)

        self.assertEqual(attribution["by_strategy"]["S"]["own_exits"], 1)
        self.assertEqual(attribution["by_strategy"]["S"]["external_exits"], 1)
        self.assertNotIn("unknown", attribution["by_reason"])


class TestBuildDiagnostics(unittest.TestCase):
    def test_suite_runs_end_to_end_and_does_not_mutate_input(self):
        index = pd.date_range("2024-01-01", periods=400, freq="D")
        equity = pd.Series(range(len(index)), index=index, dtype=float) + 10000.0
        trades = [
            _trade(10.0, exit_time="2024-03-01"),
            _trade(-4.0, exit_time="2024-06-01", exit_strategy="Router"),
        ]
        snapshot = [dict(trade) for trade in trades]

        suite = build_diagnostics(trades, equity, {"S": 2})

        self.assertEqual(
            set(suite),
            {"pnl_concentration", "exit_attribution", "calendar_returns",
             "streaks", "lifecycle_coverage"},
        )
        self.assertEqual(trades, snapshot, "diagnostics must not mutate its input")

    def test_lifecycle_omitted_when_no_observations_supplied(self):
        equity = pd.Series(
            [1.0, 2.0], index=pd.date_range("2024-01-01", periods=2, freq="D")
        )

        self.assertNotIn("lifecycle_coverage", build_diagnostics([], equity))


if __name__ == "__main__":
    unittest.main()
