"""Recorded diagnostics must distinguish no observation from an observed zero."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import research_analytics as analytics
from dashboard.report_analysis import ReportCache


class ResearchAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.reports = Path(self.directory.name)
        self.run = self.reports / "analysis_sample"
        self.run.mkdir()
        self.write("equity.csv", "timestamp,equity\n2026-01-01,100\n2026-01-02,110\n")
        self.cache_patch = patch.object(analytics, "_CACHE", ReportCache())
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.directory.cleanup()

    def write(self, name: str, contents: str) -> None:
        path = self.run / name
        previous = path.stat().st_mtime_ns if path.exists() else 0
        path.write_text(contents, encoding="utf-8", newline="\n")
        if previous:
            os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))

    def metrics(self, value: dict) -> None:
        self.write("metrics.json", json.dumps({"schema_version": "quanttrading.metrics/v1", "metrics": value}))

    def load(self, **kwargs) -> dict:
        return analytics.load_diagnostics(self.reports, self.run.name, **kwargs)

    def test_missing_diagnostics_never_turn_fills_or_flat_equity_into_closed_trades(self) -> None:
        self.write("trades.csv", "timestamp,side\n2026-01-01,buy\n")
        result = self.load()
        self.assertFalse(result["available"])
        self.assertFalse(result["closed_trades"]["available"])
        self.assertTrue(all(value is None for value in result["stats"].values()))
        self.assertTrue(all(stage["count"] is None and stage["status"] == "unknown"
                            for stage in result["funnel"]["stages"]))
        self.assertEqual(result["no_trade"]["status"], "unknown")

    def test_closed_trade_statistics_and_partial_month_independence(self) -> None:
        self.write("closed_trades.csv", "position_id,net_pnl,commission,slippage,mae,mfe,entry_time,exit_time,exit_reason,symbol\n"
                   "0001,80,1,2,10,90,2026-01-01T08:00:00+08:00,2026-01-03T00:00:00Z,take_profit,BTC-USDT\n"
                   "0002,-30,1,3,40,5,2026-01-02,2026-01-03,stop_loss,ETH-USDT\n"
                   "0003,0,1,0,,,2026-01-02,2026-01-03,flat,BTC-USDT\n")
        result = self.load(page_size=2)
        stats = result["stats"]
        self.assertEqual(stats["closed_trades"], 3)
        self.assertAlmostEqual(stats["win_rate"], 1 / 3)
        self.assertAlmostEqual(stats["profit_factor"], 80 / 30)
        self.assertAlmostEqual(stats["expectancy"], 50 / 3)
        self.assertEqual(stats["net_pnl"], 50)
        self.assertEqual(stats["commission"], 3)
        self.assertEqual(stats["slippage"], 5)
        self.assertEqual(stats["mean_holding_hours"], 32)
        self.assertEqual(stats["mean_mae"], 25)
        self.assertEqual(stats["mean_mfe"], 47.5)
        self.assertEqual(result["metric_status"]["mean_mae"]["sample_size"], 2)
        self.assertEqual(result["metric_status"]["profit_factor"]["status"], "insufficient")
        table = result["closed_trades"]
        self.assertEqual(table["rows"][0]["position_id"], "0001")
        self.assertEqual(table["rows"][0]["holding_hours"], 48)
        self.assertEqual(table["pages"], 2)
        self.assertEqual(len(self.load(page=2, page_size=2)["closed_trades"]["rows"]), 1)
        self.assertEqual(self.load(page=3, page_size=2)["closed_trades"]["rows"], [])
        concentration = result["concentration"]
        self.assertEqual(concentration["profit_hhi"], 1)
        self.assertAlmostEqual(concentration["top_n"][0]["share_of_total"], 1.6)
        self.assertEqual(concentration["top_n"][0]["total_excluding"], -30)

    def test_no_losses_do_not_create_infinite_profit_factor(self) -> None:
        self.write("closed_trades.csv", "net_pnl\n10\n20\n")
        result = self.load()
        self.assertIsNone(result["stats"]["profit_factor"])
        json.dumps(result, allow_nan=False)

    def test_recorded_metrics_preserve_insufficient_status_and_zero(self) -> None:
        self.metrics({"MaxDrawdownDurationDays": 108, "UnderwaterRatio": .6,
                      "DrawdownStatus": "ok", "TotalCommission": 8.5, "TotalSlippage": 11,
                      "ExtendedAnalytics": {"trade_quality": {"sample_size": 2, "status": "insufficient",
                          "win_rate": 0, "profit_factor": 0, "profit_factor_status": "insufficient",
                          "expectancy": -86, "holding_duration_hours": {"mean": 396, "sample_size": 2, "status": "ok"}},
                          "r_multiple": {"mae": {"mean": 120, "sample_size": 1, "status": "ok"}},
                          "drawdown_events": [{"peak": "2026-01-01", "trough": "2026-01-02",
                                               "duration_days": 108, "depth_pct": -.1, "is_open": True,
                                               "api_key": "must-not-project"}]}})
        result = self.load()
        self.assertEqual(result["stats"]["closed_trades"], 2)
        self.assertEqual(result["stats"]["profit_factor"], 0)
        self.assertEqual(result["metric_status"]["profit_factor"]["status"], "insufficient")
        self.assertEqual(result["metric_status"]["mean_mae"]["sample_size"], 1)
        self.assertEqual(result["stats"]["max_drawdown_days"], 108)
        self.assertIsNone(result["metric_status"]["max_drawdown_days"]["sample_size"])
        self.assertTrue(result["drawdowns"][0]["is_open"])
        self.assertNotIn("must-not-project", json.dumps(result))

    def test_invalid_recorded_metrics_are_not_replaced_by_optimistic_csv_values(self) -> None:
        self.write("closed_trades.csv", "net_pnl\n80\n-30\n")
        self.metrics({"ProfitFactor": 999, "ProfitFactorStatus": "invalid_input",
                      "TradeInputIntegrity": {"status": "invalid_input"}})
        result = self.load()
        for name in ("profit_factor", "win_rate", "expectancy"):
            self.assertIsNone(result["stats"][name])
            self.assertEqual(result["metric_status"][name]["status"], "invalid_input")

    def test_zero_closed_trades_is_not_a_no_entry_diagnosis(self) -> None:
        self.write("closed_trades.csv", "net_pnl,entry_time,exit_time\n")
        result = self.load()
        self.assertEqual(result["stats"]["closed_trades"], 0)
        self.assertIsNone(result["stats"]["win_rate"])
        self.assertEqual(result["no_trade"]["status"], "unknown")

    def test_funnel_keeps_absent_data_warmup_routing_unknown(self) -> None:
        self.metrics({"ExtendedAnalytics": {"signal_funnel": {
            "raw_entry_signal_chains": 4, "stages": {"risk_evaluated": {"count": 4},
            "risk_approved": {"count": 0}, "order_created": {"count": 0}, "filled": {"count": 0}},
            "excluded_exit_chains": 6, "unclassified_chains": 1}}})
        result = self.load()
        stages = {row["id"]: row for row in result["funnel"]["stages"]}
        self.assertEqual(stages["signal"]["count"], 4)
        self.assertEqual(stages["risk"]["count"], 0)
        for name in ("data", "warmup", "routing"):
            self.assertIsNone(stages[name]["count"])
            self.assertEqual(stages[name]["status"], "unknown")
        self.assertEqual(result["no_trade"]["status"], "no_entry_fills_recorded")
        self.assertEqual(result["no_trade"]["stage"], "risk")
        self.assertIn("does not establish a root cause", result["no_trade"]["reason"])

    def test_explicit_stage_evidence_is_used_and_zero_signal_not_inferred(self) -> None:
        self.metrics({"ExtendedAnalytics": {"signal_funnel": {
            "raw_entry_signal_chains": 0, "stages": {"data_ready": {"count": 120},
            "warmup_completed": {"count": 90}, "routed": {"count": 0},
            "risk_approved": {"count": 0}, "filled": {"count": 0}}}}})
        result = self.load()
        self.assertEqual(result["funnel"]["status"], "recorded")
        self.assertEqual(result["no_trade"]["stage"], "signal")
        self.assertEqual(result["funnel"]["stages"][0]["count"], 120)

    def test_existing_filled_entries_are_not_classified_as_no_trades(self) -> None:
        self.metrics({"ExtendedAnalytics": {"signal_funnel": {"stages": {"filled": {"count": 2}}}}})
        self.write("closed_trades.csv", "net_pnl\n")
        self.assertEqual(self.load()["no_trade"]["status"], "entries_observed")

    def test_partial_csv_reveals_invalid_rows_and_missing_optional_costs(self) -> None:
        self.write("closed_trades.csv", "net_pnl,commission,entry_time,exit_time,secret\n"
                   "10,1,2026-01-01,2026-01-02,never-expose\n"
                   "-5,,2026-01-03,2026-01-02,never-expose\nNaN,0,,,never-expose\n")
        result = self.load()
        self.assertEqual(result["closed_trades"]["invalid_rows"], 1)
        self.assertEqual(result["stats"]["closed_trades"], 2)
        self.assertEqual(result["stats"]["mean_holding_hours"], 24)
        self.assertIsNone(result["stats"]["commission"])
        self.assertEqual(result["metric_status"]["expectancy"]["status"], "partial")
        self.assertNotIn("never-expose", json.dumps(result))

    def test_recorded_exit_reasons_and_concentration_are_bounded_projections(self) -> None:
        self.metrics({"Diagnostics": {"exit_attribution": {"by_reason": {"stop": 2}},
                      "pnl_concentration": {"status": "ok", "sample_size": 3, "profit_hhi": .8,
                         "top_n": {"1": {"contribution": 50, "share_of_total": 1.25, "total_excluding": -10,
                                           "sample_size": 1, "secret": "hidden"}}}},
                      "ExtendedAnalytics": {"attribution": {"by_exit_reason": {"stop": -15}}}})
        result = self.load()
        self.assertEqual(result["exit_reasons"], [{"reason": "stop", "count": 2, "net_pnl": -15, "source": "metrics.json"}])
        self.assertEqual(result["concentration"]["profit_hhi"], .8)
        self.assertEqual(result["concentration"]["top_n"][0]["n"], 1)
        self.assertNotIn("hidden", json.dumps(result))

    def test_cache_invalidation_and_mutation_isolation(self) -> None:
        self.write("closed_trades.csv", "net_pnl\n10\n")
        with patch.object(analytics, "_read_metrics", wraps=analytics._read_metrics) as reader:
            first = self.load()
            first["closed_trades"]["rows"][0]["net_pnl"] = 999
            self.assertEqual(self.load()["stats"]["expectancy"], 10)
            self.assertEqual(reader.call_count, 1)
            self.metrics({"Expectancy": 12})
            self.assertEqual(self.load()["stats"]["expectancy"], 12)
            self.assertEqual(reader.call_count, 2)
        self.write("closed_trades.csv", "net_pnl\n10\n-5\n")
        self.assertEqual(self.load()["closed_trades"]["total"], 2)
        (self.run / "metrics.json").unlink()
        self.assertEqual(self.load()["stats"]["expectancy"], 2.5)
        self.write("dashboard_job.json", '{"status":"succeeded"}')
        with patch.object(analytics, "_read_metrics", wraps=analytics._read_metrics) as reader:
            self.load()
            self.assertEqual(reader.call_count, 1)

    def test_bounds_invalid_json_and_pagination(self) -> None:
        for page, size in ((0, 25), (1, 101), (True, 25), (1, False)):
            with self.subTest(page=page, size=size), self.assertRaises(ValueError):
                self.load(page=page, page_size=size)
        self.write("metrics.json", '{"metrics":')
        with self.assertRaisesRegex(ValueError, "JSON"):
            self.load()
        self.metrics({"padding": "x" * 100})
        with patch.object(analytics, "_MAX_METRICS_BYTES", 32), self.assertRaisesRegex(ValueError, "byte limit"):
            self.load()
        (self.run / "metrics.json").unlink()
        self.write("closed_trades.csv", "net_pnl\nNaN\nNaN\nNaN\n")
        with patch.object(analytics, "_MAX_CLOSED_ROWS", 2), self.assertRaisesRegex(ValueError, "row limit"):
            self.load()
        with self.assertRaises(ValueError):
            analytics.load_diagnostics(self.reports, "../secret")

    def test_malformed_recorded_types_do_not_escape_as_values(self) -> None:
        self.metrics({"WinRate": True, "ProfitFactor": float("inf"), "ProfitFactorStatus": [],
                      "Expectancy": {"private": "secret"}, "TotalTrades": -1,
                      "ExtendedAnalytics": {"signal_funnel": {"raw_entry_signal_chains": False,
                          "stages": {"filled": {"count": -1}}}}})
        result = self.load()
        self.assertIsNone(result["stats"]["win_rate"])
        self.assertIsNone(result["stats"]["profit_factor"])
        self.assertIsNone(result["stats"]["closed_trades"])
        self.assertEqual(result["no_trade"]["status"], "unknown")
        json.dumps(result, allow_nan=False)

    def test_sharpe_uses_recorded_samples_and_insufficient_status_hides_raw_value(self) -> None:
        self.metrics({"SharpeRatio": 1.5, "SharpeStatus": "ok", "SharpeSamples": 252})
        result = self.load()
        self.assertEqual(result["stats"]["sharpe_ratio"], 1.5)
        self.assertEqual(result["metric_status"]["sharpe_ratio"]["sample_size"], 252)
        self.assertEqual(result["metric_status"]["sharpe_ratio"]["source"], "metrics.json:SharpeRatio")
        self.assertIsNone(result["stats"]["sortino_ratio"])
        self.assertIsNone(result["stats"]["calmar_ratio"])
        self.assertIsNone(result["stats"]["annualized_volatility"])
        self.metrics({"SharpeRatio": 99, "SharpeStatus": "insufficient", "SharpeSamples": 1})
        result = self.load()
        self.assertIsNone(result["stats"]["sharpe_ratio"])
        self.assertEqual(result["metric_status"]["sharpe_ratio"]["status"], "insufficient")
        self.assertEqual(result["metric_status"]["sharpe_ratio"]["sample_size"], 1)

    def test_optional_risk_metrics_require_recorded_facts_and_preserve_zero(self) -> None:
        self.metrics({"SortinoRatio": 0, "SortinoStatus": "ok", "SortinoSamples": 60,
                      "CalmarRatio": .7, "CalmarStatus": "ok", "AnnualizedVolatility": .12,
                      "AnnualizedVolatilityStatus": "ok", "VolatilitySamples": 60})
        result = self.load()
        self.assertEqual(result["stats"]["sortino_ratio"], 0)
        self.assertEqual(result["stats"]["calmar_ratio"], .7)
        self.assertEqual(result["stats"]["annualized_volatility"], .12)
        self.metrics({"ExtendedAnalytics": {"portfolio_risk": {"status": "ok", "sample_size": 60,
                      "sortino_ratio": 1.2, "annualized_volatility": .2}}})
        result = self.load()
        self.assertEqual(result["stats"]["sortino_ratio"], 1.2)
        self.assertIsNone(result["stats"]["calmar_ratio"])
        self.assertEqual(result["metric_status"]["calmar_ratio"]["status"], "unknown")

    def test_exposure_prefers_recorded_elapsed_time_and_fee_ratio_requires_positive_gross(self) -> None:
        metrics = {"TotalCommission": 10, "TotalSlippage": 5, "GrossPnL": 50,
                   "ExtendedAnalytics": {"exposure": {"status": "ok", "sample_size": 182,
                       "time_in_market_ratio": .2, "mean_gross_leverage": .3, "max_gross_leverage": 1.1,
                       "elapsed_time_weighted": {"status": "ok", "time_in_market_ratio": .4, "mean_gross_leverage": .5}},
                       "turnover": {"status": "ok", "ratio": 2.2}}}
        self.metrics(metrics)
        result = self.load()
        self.assertEqual(result["stats"]["fee_return_ratio"], .2)
        self.assertEqual(result["stats"]["time_in_market_ratio"], .4)
        self.assertEqual(result["stats"]["mean_gross_leverage"], .5)
        self.assertEqual(result["stats"]["max_gross_leverage"], 1.1)
        self.assertEqual(result["stats"]["turnover_ratio"], 2.2)
        self.assertIn("elapsed_time_weighted", result["metric_status"]["time_in_market_ratio"]["source"])
        metrics["GrossPnL"] = -50
        self.metrics(metrics)
        self.assertIsNone(self.load()["stats"]["fee_return_ratio"])

    def test_health_blockers_and_findings_preserve_their_recorded_scope(self) -> None:
        self.metrics({"Diagnostics": {"strategy_activity_consistency": {"status": "ok",
                          "longest_no_trade_days": 400, "suppressed_raw_setups": 12,
                          "silent_inactivity_detected": True, "findings": ["reported inconsistency"]}},
                      "StrategyHealth": {"Trend": {"status": "cooldown", "allows_new_entries": False,
                          "suppressed_raw_setups": 12, "raw_setup_count": 20, "trigger_reason": "negative cohorts"}},
                      "BacktestLifecycle": {"termination_reason": "account loss limit", "termination_timestamp": "2026-01-02"}})
        result = self.load()
        blockers = result["no_trade"]["blockers"]
        self.assertEqual([item["kind"] for item in blockers],
                         ["health_entry_gate", "suppressed_raw_setups", "recorded_termination"])
        self.assertEqual(blockers[0]["reason"], "negative cohorts")
        self.assertIn("snapshot", blockers[0]["scope"])
        self.assertEqual(blockers[1]["count"], 12)
        self.assertEqual(result["activity"]["longest_no_closed_trade_days"], 400)
        self.assertEqual(result["activity"]["findings"], ["reported inconsistency"])
        # Gate evidence cannot invent a raw signal-to-entry conversion or absence of fills.
        self.assertEqual(result["no_trade"]["status"], "unknown")
        self.assertIsNone(result["funnel"]["stages"][2]["count"])

    def test_router_exits_and_long_closed_trade_gap_are_not_entry_blockers(self) -> None:
        self.metrics({"Diagnostics": {"strategy_activity_consistency": {
                         "longest_no_trade_days": 500, "suppressed_raw_setups": 0,
                         "findings": ["Check report consistency"]},
                         "exit_attribution": {"by_reason": {"Regime SIDEWAYS Not Allowed": 3}}},
                      "StrategyHealth": {"Trend": {"status": "active", "allows_new_entries": True}}})
        result = self.load()
        self.assertEqual(result["no_trade"]["blockers"], [])
        self.assertEqual(result["no_trade"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
