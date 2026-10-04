"""Numerical and resource-bound contracts for the browser report workspace."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import visual_data
from dashboard.report_analysis import ReportCache
from dashboard.visual_data import list_backtests, load_backtest, load_trades


class DashboardAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.reports = Path(self.directory.name)
        self.run = self.reports / "sample_run"
        self.run.mkdir()
        self.write("equity.csv", "timestamp,equity\n2026-01-01,100\n2026-01-31,110\n2026-02-28,99\n")
        self.cache_patch = patch.object(visual_data, "_REPORT_CACHE", ReportCache())
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.directory.cleanup()

    def write(self, filename: str, contents: str) -> None:
        path = self.run / filename
        previous = path.stat().st_mtime_ns if path.exists() else 0
        path.write_text(contents, encoding="utf-8", newline="\n")
        if previous:
            os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))

    def test_monthly_compounding_and_observation_statistics(self) -> None:
        self.write("benchmark.csv", "timestamp,reference\n2026-01-01,100\n2026-02-28,105\n")
        result = load_backtest(self.reports, self.run.name)
        metrics = result["metrics"]
        self.assertAlmostEqual(metrics["total_return"], -.01)
        self.assertAlmostEqual(metrics["benchmark_total_return"], .05)
        self.assertAlmostEqual(metrics["excess_return"], -.06)
        self.assertTrue(result["benchmark_aligned"])
        self.assertEqual(metrics["observations"], 3)
        self.assertEqual(metrics["periods"], 2)
        self.assertEqual(metrics["duration_days"], 58)
        self.assertEqual(metrics["positive_period_ratio"], .5)
        self.assertAlmostEqual(metrics["period_volatility"], (0.02 ** .5))
        self.assertAlmostEqual(metrics["annualized_return"], .99 ** (365.25 / 58) - 1)
        months = result["monthly_returns"]
        self.assertEqual([row["month"] for row in months], ["2026-01", "2026-02"])
        self.assertAlmostEqual(months[0]["return"], .1)
        self.assertAlmostEqual(months[1]["return"], -.1)
        self.assertEqual(months[1]["period_start"], "2026-01-31")
        self.assertIn("not a closed-trade win rate", result["methodology"]["positive_period_ratio"])

    def test_benchmark_requires_both_endpoints_and_normalizes_timezone(self) -> None:
        self.write("benchmark.csv", "timestamp,reference\n2026-01-31,100\n2026-02-28,105\n")
        result = load_backtest(self.reports, self.run.name)
        self.assertFalse(result["benchmark_aligned"])
        self.assertIsNone(result["metrics"]["benchmark_total_return"])
        self.assertIsNone(result["metrics"]["excess_return"])
        self.write("benchmark.csv", "timestamp,reference\n2026-01-01T08:00:00+08:00,100\n2026-02-28T00:00:00Z,105\n")
        self.assertTrue(load_backtest(self.reports, self.run.name)["benchmark_aligned"])

    def test_benchmark_accepts_canonical_unnamed_datetime_index_only(self) -> None:
        self.write("benchmark.csv", ",fixed_equal_weight\n2026-01-01,100\n2026-02-28,105\n")
        result = load_backtest(self.reports, self.run.name)
        self.assertTrue(result["benchmark_aligned"])
        self.assertAlmostEqual(result["metrics"]["benchmark_total_return"], .05)
        self.assertEqual(result["points"][-1]["benchmark"], 105)
        self.write("trades.csv", ",side\n2026-01-01,buy\n")
        with self.assertRaisesRegex(ValueError, "columns"):
            load_trades(self.reports, self.run.name)
        (self.run / "trades.csv").unlink()
        self.write("equity.csv", ",equity\n2026-01-01,100\n")
        with self.assertRaisesRegex(ValueError, "columns"):
            load_backtest(self.reports, self.run.name)

    def test_single_observation_has_no_invented_period_metrics(self) -> None:
        self.write("equity.csv", "timestamp,equity\n2026-01-01,100\n")
        result = load_backtest(self.reports, self.run.name)
        for key in ("annualized_return", "period_volatility", "positive_period_ratio", "best_period_return"):
            self.assertIsNone(result["metrics"][key])
        self.assertIsNone(result["monthly_returns"][0]["return"])
        self.assertEqual(result["monthly_returns"][0]["periods"], 0)

    def test_missing_months_are_not_fabricated_and_intervals_compound(self) -> None:
        self.write("equity.csv", "timestamp,equity\n2026-01-15,100\n2026-01-31,120\n2026-03-31,108\n")
        result = load_backtest(self.reports, self.run.name)
        months = result["monthly_returns"]
        self.assertEqual([row["month"] for row in months], ["2026-01", "2026-03"])
        self.assertEqual(months[1]["period_start"], "2026-01-31")
        self.assertAlmostEqual((1 + months[0]["return"]) * (1 + months[1]["return"]) - 1,
                               result["metrics"]["total_return"])

    def test_nonfinite_calculations_stay_json_safe_without_changing_sample(self) -> None:
        self.write("equity.csv", "timestamp,equity\n2026-01-01,1e-300\n2026-01-02,1e300\n2026-01-03,1e-300\n")
        result = load_backtest(self.reports, self.run.name)
        self.assertIsNone(result["metrics"]["positive_period_ratio"])
        self.assertIsNone(result["metrics"]["period_volatility"])
        json.dumps(result, allow_nan=False)

    def test_invalid_equity_rows_are_counted_and_never_used(self) -> None:
        self.write("equity.csv", "timestamp,equity\n2026-01-01,100\n2026-01-01,200\n"
                   "2025-12-31,300\ninvalid,10\n2026-01-02,NaN\n2026-01-03,-5\n"
                   "2026-01-04,120,extra\n2026-01-05,110\n")
        result = load_backtest(self.reports, self.run.name)
        self.assertEqual(result["invalid_rows"], 6)
        self.assertEqual(result["metrics"]["observations"], 2)
        self.assertAlmostEqual(result["metrics"]["total_return"], .1)

    def test_trade_pages_preserve_text_nulls_and_report_malformed_rows(self) -> None:
        self.write("trades.csv", 'timestamp,order_id,side,note\n2026-01-01,00001,buy,"a,b"\n'
                   '2026-01-02,00002,sell,\n2026-01-03,00003,buy,"first\nsecond"\n'
                   '2026-01-04,00004,buy\n2026-01-05,00005,sell,note,extra\n')
        first = load_trades(self.reports, self.run.name, page_size=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["pages"], 2)
        self.assertEqual(first["invalid_rows"], 2)
        self.assertEqual(first["rows"][0]["order_id"], "00001")
        self.assertEqual(first["rows"][0]["note"], "a,b")
        self.assertIsNone(first["rows"][1]["note"])
        second = load_trades(self.reports, self.run.name, page=2, page_size=2)
        self.assertEqual(second["rows"][0]["note"], "first\nsecond")
        self.assertEqual(load_trades(self.reports, self.run.name, page=3, page_size=2)["rows"], [])
        self.assertEqual(load_backtest(self.reports, self.run.name)["metrics"]["fills_count"], 3)

    def test_trade_cells_are_bounded_without_losing_column_names(self) -> None:
        self.write("trades.csv", "side,note\nbuy," + "x" * 2000 + "\n")
        result = load_trades(self.reports, self.run.name)
        self.assertEqual(result["truncated_cells"], 1)
        self.assertEqual(len(result["rows"][0]["note"]), 1025)
        self.assertEqual(result["columns"], ["side", "note"])

    def test_missing_trades_are_distinct_from_zero_fills(self) -> None:
        self.assertFalse(load_trades(self.reports, self.run.name)["available"])
        self.assertIsNone(load_backtest(self.reports, self.run.name)["metrics"]["fills_count"])
        self.write("trades.csv", "timestamp,side\n")
        self.assertTrue(load_trades(self.reports, self.run.name)["available"])
        self.assertEqual(load_backtest(self.reports, self.run.name)["metrics"]["fills_count"], 0)

    def test_pagination_and_run_names_are_validated(self) -> None:
        for page, size in ((0, 25), (1, 0), (1, 101), (True, 25), (1, False), (1.5, 25)):
            with self.subTest(page=page, size=size), self.assertRaises(ValueError):
                load_trades(self.reports, self.run.name, page, size)
        for run_id in ("..", "../sample_run", "sample_run/foo", "."):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                load_backtest(self.reports, run_id)

    def test_cache_reuses_reads_and_caller_mutation_cannot_corrupt_it(self) -> None:
        with patch.object(visual_data, "_csv_reader", wraps=visual_data._csv_reader) as read:
            result = load_backtest(self.reports, self.run.name)
            calls = read.call_count
            result["points"][0]["equity"] = -99
            result["metrics"]["observations"] = 999
            second = load_backtest(self.reports, self.run.name)
            self.assertEqual(second["points"][0]["equity"], 100)
            self.assertEqual(second["metrics"]["observations"], 3)
            self.assertEqual(read.call_count, calls)

    def test_cache_invalidates_for_all_dependencies_and_missing_files(self) -> None:
        load_backtest(self.reports, self.run.name)
        self.write("benchmark.csv", "timestamp,reference\n2026-01-01,100\n2026-02-28,110\n")
        self.assertAlmostEqual(load_backtest(self.reports, self.run.name)["metrics"]["benchmark_total_return"], .1)
        self.write("trades.csv", "side\nbuy\n")
        self.assertEqual(load_backtest(self.reports, self.run.name)["metrics"]["fills_count"], 1)
        self.write("trades.csv", "side\nbuy\nsell\n")
        self.assertEqual(load_backtest(self.reports, self.run.name)["metrics"]["fills_count"], 2)
        self.write("equity.csv", "timestamp,equity\n2026-01-01,100\n2026-02-28,200\n")
        self.assertEqual(load_backtest(self.reports, self.run.name)["metrics"]["total_return"], 1)
        (self.run / "benchmark.csv").unlink()
        self.assertIsNone(load_backtest(self.reports, self.run.name)["metrics"]["benchmark_total_return"])

    def test_byte_and_row_limits_include_invalid_data(self) -> None:
        with patch.object(visual_data, "_MAX_REPORT_BYTES", 16), self.assertRaisesRegex(ValueError, "byte limit"):
            load_backtest(self.reports, self.run.name)
        self.write("equity.csv", "timestamp,equity\ninvalid,NaN\ninvalid,NaN\ninvalid,NaN\n")
        with patch.object(visual_data, "_MAX_REPORT_ROWS", 2), self.assertRaisesRegex(ValueError, "row limit"):
            load_backtest(self.reports, self.run.name)
        self.write("trades.csv", "side\nbuy\nsell\nbuy\n")
        with patch.object(visual_data, "_MAX_TRADE_ROWS", 2), self.assertRaisesRegex(ValueError, "row limit"):
            load_trades(self.reports, self.run.name)

    def test_malformed_csv_is_a_value_error(self) -> None:
        for contents in ('side,side\nbuy,sell\n', 'side,note\nbuy,"unterminated\n'):
            self.write("trades.csv", contents)
            with self.assertRaises(ValueError):
                load_trades(self.reports, self.run.name)
        (self.run / "trades.csv").write_bytes(b"side\n\xff\n")
        with self.assertRaises(ValueError):
            load_trades(self.reports, self.run.name)

    def test_partial_and_failed_web_reports_remain_hidden(self) -> None:
        web_run = self.reports / "web_test"
        web_run.mkdir()
        (web_run / "equity.csv").write_text("timestamp,equity\n2026-01-01,100\n", encoding="utf-8")
        for status in (None, "running", "failed"):
            if status:
                (web_run / "dashboard_job.json").write_text(json.dumps({"status": status}), encoding="utf-8")
            self.assertNotIn("web_test", [run["id"] for run in list_backtests(self.reports)])
            with self.assertRaises(FileNotFoundError):
                load_backtest(self.reports, "web_test")
        (web_run / "dashboard_job.json").write_text('{"status":"succeeded"}', encoding="utf-8")
        self.assertIn("web_test", [run["id"] for run in list_backtests(self.reports)])
        self.assertEqual(load_backtest(self.reports, "web_test")["metrics"]["end_equity"], 100)

    def test_saved_parameters_project_only_approved_fields_and_invalidate_cache(self) -> None:
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], {})
        parameters = {"source": "synthetic", "symbols": ["BTC/USDT", "ETH/USDT"],
                      "start": "2026-01-01", "end": "2026-02-28", "capital": 10000,
                      "slippage_bps": 5, "seed": 42}
        self.write("dashboard_job.json", json.dumps({"status": "succeeded", "secret": "excluded",
                   "parameters": {**parameters, "api_key": "excluded"}}))
        report = load_backtest(self.reports, self.run.name)
        self.assertEqual(report["parameters"], parameters)
        listed = list_backtests(self.reports)[0]
        self.assertEqual(listed["source"], "synthetic")
        self.assertEqual(listed["parameters"], parameters)
        report["parameters"]["symbols"].append("BNB/USDT")
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], parameters)
        parameters["source"] = "local"
        self.write("dashboard_job.json", json.dumps({"status": "succeeded", "parameters": parameters}))
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"]["source"], "local")
        self.assertEqual(list_backtests(self.reports)[0]["source"], "local")
        (self.run / "dashboard_job.json").unlink()
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], {})

    def test_metadata_projection_rejects_unbounded_and_invalid_values(self) -> None:
        self.write("dashboard_job.json", json.dumps({"status": "succeeded", "parameters": {
            "source": {"secret": "hidden"}, "symbols": ["../../../secret"], "start": "not-a-date",
            "capital": True, "slippage_bps": float("inf"), "seed": -1,
        }}))
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], {})
        self.write("dashboard_job.json", json.dumps({"status": "failed", "parameters": {"source": "synthetic"}}))
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], {})
        self.write("dashboard_job.json", json.dumps({"status": "succeeded", "parameters": {"source": "synthetic"},
                   "oversized": "x" * (32 * 1024)}))
        self.assertIsNone(list_backtests(self.reports)[0]["source"])
        self.assertEqual(load_backtest(self.reports, self.run.name)["parameters"], {})

    def test_cache_enforces_entry_and_byte_budgets(self) -> None:
        cache = ReportCache(max_entries=2, max_bytes=64)
        for index in range(3):
            cache.put((index,), (1,), {"value": index})
        self.assertIsNone(cache.get((0,), (1,)))
        self.assertEqual(cache.get((2,), (1,)), {"value": 2})
        self.assertIsNone(cache.get((2,), (2,)))
        cache.put((3,), (1,), {"value": "x" * 100})
        self.assertIsNone(cache.get((3,), (1,)))
        self.assertLessEqual(cache._bytes, 64)


if __name__ == "__main__":
    unittest.main()
