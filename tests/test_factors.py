import unittest

import numpy as np
import pandas as pd

from core.factors import (
    CapitalFlowFactors,
    Factors,
    MomentumFactors,
    SupportResistanceFactors,
    TrendFactors,
    VolatilityFactors,
    VolumeFactors,
)


class TestFactorsBase(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        self.length = 150
        dates = pd.date_range(start="2023-01-01", periods=self.length, freq="D")
        self.df = pd.DataFrame(
            {
                "open": np.random.randn(self.length).cumsum() + 100,
                "high": np.random.randn(self.length).cumsum() + 105,
                "low": np.random.randn(self.length).cumsum() + 95,
                "close": np.random.randn(self.length).cumsum() + 100,
                "volume": np.random.randint(100, 1000, self.length),
            },
            index=dates,
        )
        self.df["high"] = self.df[["open", "close", "high"]].max(axis=1) + 1
        self.df["low"] = self.df[["open", "close", "low"]].min(axis=1) - 1


class TestTrendFactors(TestFactorsBase):
    def test_macd_shape(self):
        macd_line, signal_line, hist = TrendFactors.MACD(self.df["close"])
        self.assertEqual(len(macd_line), self.length)
        self.assertTrue(np.allclose(hist.dropna(), (macd_line - signal_line).dropna()))


class TestMomentumFactors(TestFactorsBase):
    def test_rsi_range(self):
        rsi = MomentumFactors.RSI(self.df["close"], 14)
        valid = rsi.dropna()
        self.assertTrue((valid >= 0).all() and (valid <= 100).all())

    def test_stoch_range(self):
        k, d = MomentumFactors.STOCH(self.df)
        self.assertTrue((k.dropna() >= 0).all() and (k.dropna() <= 100).all())
        self.assertTrue((d.dropna() >= 0).all() and (d.dropna() <= 100).all())

    def test_williams_r_range(self):
        willr = MomentumFactors.WILLIAMS_R(self.df)
        valid = willr.dropna()
        self.assertTrue((valid >= -100).all() and (valid <= 0).all())

    def test_mfi_range(self):
        mfi = MomentumFactors.MFI(self.df, 14)
        valid = mfi.dropna()
        self.assertTrue((valid >= 0).all() and (valid <= 100).all())

    def test_cci_finite(self):
        cci = MomentumFactors.CCI(self.df, 20)
        self.assertEqual(len(cci), self.length)


class TestVolatilityFactors(TestFactorsBase):
    def test_keltner_ordering(self):
        upper, middle, lower = VolatilityFactors.KELTNER(self.df)
        valid = slice(25, None)
        self.assertTrue((upper.iloc[valid] >= middle.iloc[valid]).all())
        self.assertTrue((middle.iloc[valid] >= lower.iloc[valid]).all())


class TestVolumeFactors(TestFactorsBase):
    def test_obv_length(self):
        obv = VolumeFactors.OBV(self.df)
        self.assertEqual(len(obv), self.length)

    def test_vwap_positive(self):
        vwap = VolumeFactors.VWAP(self.df)
        self.assertTrue((vwap.dropna() > 0).all())

    def test_cmf_bounded(self):
        cmf = VolumeFactors.CMF(self.df, 20)
        valid = cmf.dropna()
        self.assertTrue((valid >= -1.0001).all() and (valid <= 1.0001).all())

    def test_volume_profile_shape(self):
        poc, vah, val = VolumeFactors.VOLUME_PROFILE(self.df, window=50, bins=10)
        self.assertEqual(len(poc), self.length)
        valid_idx = poc.dropna().index
        self.assertTrue((vah.loc[valid_idx] >= val.loc[valid_idx]).all())


class TestSupportResistanceFactors(TestFactorsBase):
    def test_pivot_points(self):
        pivot, r1, r2, s1, s2 = SupportResistanceFactors.PIVOT_POINTS(self.df)
        valid = slice(1, None)
        self.assertTrue((r1.iloc[valid] >= pivot.iloc[valid]).all())
        self.assertTrue((pivot.iloc[valid] >= s1.iloc[valid]).all())

    def test_swing_points(self):
        swing_high = SupportResistanceFactors.SWING_HIGH(self.df, 5)
        swing_low = SupportResistanceFactors.SWING_LOW(self.df, 5)
        self.assertEqual(len(swing_high), self.length)
        self.assertEqual(len(swing_low), self.length)

    def test_fibonacci_retracement(self):
        levels = SupportResistanceFactors.FIBONACCI_RETRACEMENT(200.0, 100.0)
        self.assertAlmostEqual(levels["FIB_0.000"], 200.0)
        self.assertAlmostEqual(levels["FIB_1.000"], 100.0)
        self.assertAlmostEqual(levels["FIB_0.500"], 150.0)

    def test_fibonacci_invalid_range(self):
        with self.assertRaises(ValueError):
            SupportResistanceFactors.FIBONACCI_RETRACEMENT(100.0, 200.0)


class TestCapitalFlowFactors(unittest.TestCase):
    def setUp(self):
        np.random.seed(7)
        dates = pd.date_range(start="2023-01-01", periods=60, freq="8h")
        self.funding_rate = pd.Series(np.random.normal(0.0001, 0.0003, len(dates)), index=dates)
        self.open_interest = pd.Series(
            np.random.randint(1000, 5000, len(dates)).astype(float), index=dates
        )
        self.close = pd.Series(np.random.randn(len(dates)).cumsum() + 100, index=dates)

    def test_funding_rate_zscore(self):
        z = CapitalFlowFactors.funding_rate_zscore(self.funding_rate, window=10)
        self.assertEqual(len(z), len(self.funding_rate))

    def test_open_interest_change_pct(self):
        change = CapitalFlowFactors.open_interest_change_pct(self.open_interest)
        self.assertEqual(len(change), len(self.open_interest))

    def test_price_oi_divergence_values(self):
        divergence = CapitalFlowFactors.price_oi_divergence(self.close, self.open_interest)
        valid = divergence.dropna()
        self.assertTrue(valid.isin([-1.0, 0.0, 1.0]).all())


class TestFactorsAggregate(TestFactorsBase):
    def test_calculate_all_adds_columns_without_removing_existing(self):
        original_columns = set(self.df.columns)
        Factors.calculate_all(self.df)
        new_columns = set(self.df.columns) - original_columns
        expected_subset = {
            "MACD_LINE",
            "RSI_14",
            "STOCH_K",
            "KC_UPPER",
            "OBV",
            "VWAP",
            "PIVOT",
            "SWING_HIGH",
        }
        self.assertTrue(expected_subset.issubset(new_columns))
        self.assertTrue(original_columns.issubset(set(self.df.columns)))


if __name__ == "__main__":
    unittest.main()
