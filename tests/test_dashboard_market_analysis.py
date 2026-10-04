"""Formula, bounded-input and truthful daily-availability contracts."""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from dashboard import market_analysis
from dashboard.market_analysis import load_data_quality, load_market_analysis, validate_data_selection
from dashboard.report_analysis import ReportCache


class MarketAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name)
        self.cache_patch = patch.object(market_analysis, "_MARKET_CACHE", ReportCache())
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.directory.cleanup()

    def write(self, rows: list[list], symbol: str = "BTC/USDT") -> Path:
        path = self.data / (symbol.replace("/", "_") + ".csv")
        previous = path.stat().st_mtime_ns if path.exists() else 0
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            writer.writerows(rows)
        if previous:
            os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))
        return path

    @staticmethod
    def rows(count: int = 60, start: date = date(2026, 1, 1)) -> list[list]:
        return [[(start + timedelta(days=index)).isoformat(), 10 + index,
                 12 + index, 8 + index, 10 + index, 10] for index in range(count)]

    def test_linear_prices_known_formula_values_and_warmups(self) -> None:
        self.write(self.rows())
        result = load_market_analysis(self.data, "BTC/USDT")
        points = result["points"]
        first = {"sma20": 19, "sma50": 49, "ema12": 11, "ema26": 25,
                 "rsi14": 14, "macd": 25, "macd_signal": 33, "macd_hist": 33,
                 "bb_mid": 19, "bb_upper": 19, "bb_lower": 19, "atr14": 13,
                 "adx14": 27, "stoch_k": 13, "stoch_d": 15, "roc12": 12,
                 "cci20": 19, "williams_r14": 13}
        for field, index in first.items():
            with self.subTest(field=field):
                self.assertTrue(all(point[field] is None for point in points[:index]))
                self.assertIsNotNone(points[index][field])
        self.assertAlmostEqual(points[19]["sma20"], 19.5)
        self.assertAlmostEqual(points[49]["sma50"], 34.5)
        self.assertAlmostEqual(points[11]["ema12"], 15.5)
        self.assertAlmostEqual(points[12]["ema12"], 16.5)
        self.assertAlmostEqual(points[25]["ema26"], 22.5)
        self.assertAlmostEqual(points[33]["macd"], 7)
        self.assertAlmostEqual(points[33]["macd_signal"], 7)
        self.assertAlmostEqual(points[33]["macd_hist"], 0)
        self.assertAlmostEqual(points[19]["bb_upper"], 19.5 + 2 * math.sqrt(33.25))
        self.assertAlmostEqual(points[19]["bb_lower"], 19.5 - 2 * math.sqrt(33.25))
        self.assertAlmostEqual(points[14]["rsi14"], 100)
        self.assertAlmostEqual(points[13]["atr14"], 4)
        self.assertAlmostEqual(points[27]["adx14"], 100)
        self.assertAlmostEqual(points[13]["stoch_k"], 100 * 15 / 17)
        self.assertAlmostEqual(points[15]["stoch_d"], 100 * 15 / 17)
        self.assertAlmostEqual(points[13]["williams_r14"], -100 * 2 / 17)
        self.assertAlmostEqual(points[19]["cci20"], 9.5 / (.015 * 5))
        self.assertAlmostEqual(points[12]["roc12"], 120)
        self.assertEqual(points[0]["obv"], 0)
        self.assertEqual(points[19]["obv"], 190)
        self.assertEqual(result["latest"]["sma50"], points[-1]["sma50"])
        json.dumps(result, allow_nan=False)

    def test_rsi_and_atr_use_wilder_recurrence_after_sma_seed(self) -> None:
        rows = self.rows(16)
        # First 14 close changes are +1; final change is -2 and true range is 8.
        rows[-1][1:5] = [22, 27, 19, 22]
        self.write(rows)
        point = load_market_analysis(self.data, "BTC/USDT")["points"][-1]
        self.assertAlmostEqual(point["rsi14"], 100 * 13 / 15)
        self.assertAlmostEqual(point["atr14"], (4 * 13 + 8) / 14)
        self.assertEqual(point["obv"], 130)

    def test_flat_zero_range_and_short_history_never_invent_values(self) -> None:
        self.write([[row[0], 42, 42, 42, 42, 0] for row in self.rows(40)])
        point = load_market_analysis(self.data, "BTC/USDT")["points"][-1]
        self.assertEqual(point["rsi14"], 50)
        self.assertEqual(point["adx14"], 0)
        self.assertEqual(point["atr14"], 0)
        self.assertEqual(point["bb_upper"], 42)
        self.assertEqual(point["obv"], 0)
        self.assertIsNone(point["stoch_k"])
        self.assertIsNone(point["stoch_d"])
        self.assertIsNone(point["williams_r14"])
        self.assertIsNone(point["cci20"])
        self.write(self.rows(1))
        point = load_market_analysis(self.data, "BTC/USDT")["points"][0]
        for field in market_analysis._INDICATORS:
            if field != "obv":
                self.assertIsNone(point[field])

    def test_display_limit_does_not_restart_warmup_or_mutate_cache(self) -> None:
        self.write(self.rows(80))
        full = load_market_analysis(self.data, "BTC/USDT")
        tail = load_market_analysis(self.data, "BTC/USDT", 2)
        self.assertEqual(tail["points"], full["points"][-2:])
        tail["points"][0]["close"] = -1
        tail["latest"]["sma50"] = -1
        fresh = load_market_analysis(self.data, "BTC/USDT", 2)
        self.assertEqual(fresh["points"], full["points"][-2:])
        self.assertGreater(fresh["latest"]["sma50"], 0)
        self.write(self.rows(81))
        updated = load_market_analysis(self.data, "BTC/USDT", 2)
        self.assertNotEqual(updated["last_candle"], fresh["last_candle"])

    def test_extreme_finite_prices_remain_json_safe(self) -> None:
        rows = [[row[0], value, value, value, value, 1e308]
                for index, row in enumerate(self.rows(60))
                for value in [1e300 if index % 13 else 1e-300]]
        self.write(rows)
        result = load_market_analysis(self.data, "BTC/USDT")
        json.dumps(result, allow_nan=False)
        self.assertIsNone(result["points"][12]["roc12"])
        self.write([[row[0], *([sys.float_info.max] * 4), 1] for row in self.rows(60)])
        result = load_market_analysis(self.data, "BTC/USDT")
        json.dumps(result, allow_nan=False)
        self.assertIsNone(result["points"][-1]["cci20"])
        self.assertEqual(result["points"][-1]["sma20"], sys.float_info.max)

    def test_quality_distinguishes_duplicates_invalid_rows_and_out_of_order(self) -> None:
        rows = self.rows(5)
        self.write([rows[0], rows[0], rows[2], rows[1],
                    [rows[3][0], 10, 9, 8, 10, 10], ["invalid", 10, 12, 8, 10, 10]])
        result = load_data_quality(self.data, ["BTC/USDT"])
        quality = result["symbols"][0]
        self.assertEqual(quality["rows"], 6)
        self.assertEqual(quality["valid_rows"], 3)
        self.assertEqual(quality["duplicate_timestamps"], 1)
        self.assertEqual(quality["out_of_order"], 1)
        self.assertEqual(quality["invalid_rows"], 2)
        self.assertEqual(quality["invalid_ohlcv"], 1)
        self.assertEqual(quality["invalid_timestamps"], 1)
        self.assertFalse(result["selection"]["valid"])
        self.assertIsNone(result["common_usable_range"])
        points = load_market_analysis(self.data, "BTC/USDT")["points"]
        self.assertEqual([row["close"] for row in points], [10, 11, 12])

    def test_missing_day_and_longest_common_clean_range_are_observed(self) -> None:
        rows = self.rows(10)
        self.write(rows[:3] + rows[4:])
        self.write(rows[1:9], "ETH/USDT")
        result = load_data_quality(self.data, ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(result["symbols"][0]["missing_days"], 1)
        self.assertEqual(result["symbols"][0]["missing_dates_sample"], ["2026-01-04"])
        self.assertEqual(result["common_range"], {"start": "2026-01-02", "end": "2026-01-09",
                                               "complete": False, "missing_days": 1,
                                               "observations": 7})
        self.assertEqual(result["common_usable_range"], {"start": "2026-01-05", "end": "2026-01-09", "days": 5})
        with self.assertRaisesRegex(ValueError, "missing daily candles"):
            validate_data_selection(self.data, ["BTC/USDT", "ETH/USDT"], "2026-01-02", "2026-01-09")
        clean = validate_data_selection(self.data, ["BTC/USDT", "ETH/USDT"], "2026-01-05", "2026-01-09")
        self.assertTrue(clean["selection"]["valid"])

    def test_corruption_outside_requested_dates_does_not_block_clean_range(self) -> None:
        rows = self.rows(10)
        self.write([rows[0], rows[0], *rows[1:]])
        result = validate_data_selection(self.data, ["BTC/USDT"], "2026-01-02", "2026-01-10")
        self.assertTrue(result["selection"]["valid"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_data_selection(self.data, ["BTC/USDT"], "2026-01-01", "2026-01-10")

    def test_timezone_equivalence_duplicate_detection_and_non_daily_rows(self) -> None:
        rows = self.rows(2)
        self.write([rows[0], ["2026-01-01T08:00:00+08:00", *rows[0][1:]],
                    ["2026-01-02T12:00:00Z", *rows[1][1:]]])
        result = load_data_quality(self.data, ["BTC/USDT"])
        quality = result["symbols"][0]
        self.assertEqual(quality["duplicate_timestamps"], 1)
        self.assertEqual(quality["non_daily_rows"], 1)
        self.assertEqual(quality["daily_rows"], 1)
        self.assertEqual(quality["end"], "2026-01-01")
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_data_selection(self.data, ["BTC/USDT"], "2026-01-01", "2026-01-02")

    def test_missing_empty_and_malformed_caches_do_not_invent_availability(self) -> None:
        result = load_data_quality(self.data, ["BTC/USDT"])
        self.assertFalse(result["symbols"][0]["available"])
        self.assertIsNone(result["common_range"])
        with self.assertRaises(FileNotFoundError):
            load_market_analysis(self.data, "BTC/USDT")
        self.write([])
        result = load_data_quality(self.data, ["BTC/USDT"])
        self.assertTrue(result["symbols"][0]["available"])
        self.assertIsNone(result["symbols"][0]["error"])
        self.assertFalse(result["selection"]["valid"])
        self.assertIsNone(result["common_range"])
        with self.assertRaisesRegex(ValueError, "no valid"):
            load_market_analysis(self.data, "BTC/USDT")
        (self.data / "BTC_USDT.csv").write_text("timestamp,close\n2026-01-01,10\n")
        result = load_data_quality(self.data, ["BTC/USDT"])
        self.assertIn("schema", result["symbols"][0]["error"])
        with self.assertRaisesRegex(ValueError, "schema"):
            validate_data_selection(self.data, ["BTC/USDT"], "2026-01-01", "2026-01-02")

    def test_invalid_input_bounds_and_disjoint_ranges(self) -> None:
        self.write(self.rows(2))
        for symbol in ("../BTC/USDT", "BTC/USD", "btc/USDT", None):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                load_market_analysis(self.data, symbol)
        for limit in (0, 241, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                load_market_analysis(self.data, "BTC/USDT", limit)
        for symbols, start, end in (([], "2026-01-01", "2026-01-02"),
                                    (["BTC/USDT"] * 2, "2026-01-01", "2026-01-02"),
                                    (["BTC/USDT"], "2026-01-02", "2026-01-01"),
                                    (["BTC/USDT"], "2026-01-01T00:00:00", "2026-01-02"),
                                    (["BTC/USDT"], "2025-12-31", "2026-01-02")):
            with self.subTest(symbols=symbols, start=start), self.assertRaises(ValueError):
                validate_data_selection(self.data, symbols, start, end)
        self.write(self.rows(2, date(2026, 2, 1)), "ETH/USDT")
        result = load_data_quality(self.data, ["BTC/USDT", "ETH/USDT"])
        self.assertIsNone(result["common_range"])
        self.assertIsNone(result["common_usable_range"])
        self.assertFalse(result["selection"]["valid"])
        partial = load_data_quality(self.data, ["BTC/USDT"], start="2026-02-01")
        self.assertFalse(partial["selection"]["valid"])
        self.assertIsNone(partial["symbols"][0]["selection"]["missing_days"])

    def test_resource_limits_include_invalid_rows_and_combined_bytes(self) -> None:
        self.write([["bad", 10, 12, 8, 10, 1]] * 3)
        with patch.object(market_analysis, "_MAX_ROWS", 2):
            with self.assertRaisesRegex(ValueError, "row limit"):
                load_market_analysis(self.data, "BTC/USDT")
        self.write(self.rows(2))
        with patch.object(market_analysis, "_MAX_BYTES", 30):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                load_market_analysis(self.data, "BTC/USDT")
        with patch.object(market_analysis, "_MAX_TOTAL_BYTES", 30):
            with self.assertRaisesRegex(ValueError, "combined byte"):
                load_data_quality(self.data, ["BTC/USDT"])

    def test_missing_dates_sample_is_bounded_for_extreme_ranges(self) -> None:
        self.write([["0001-01-01", 10, 12, 8, 10, 1], ["9999-12-31", 10, 12, 8, 10, 1]])
        result = load_data_quality(self.data, ["BTC/USDT"])
        quality = result["symbols"][0]
        self.assertEqual(quality["missing_days"], (date.max - date.min).days - 1)
        self.assertEqual(len(quality["missing_dates_sample"]), 20)
        self.assertEqual(result["common_usable_range"], {"start": "9999-12-31", "end": "9999-12-31", "days": 1})


if __name__ == "__main__":
    unittest.main()
