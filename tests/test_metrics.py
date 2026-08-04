import math
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from core.metrics import (
    calculate_drawdown,
    calculate_equity_metrics,
    calculate_profit_factor,
    calculate_sharpe,
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


if __name__ == "__main__":
    unittest.main()
