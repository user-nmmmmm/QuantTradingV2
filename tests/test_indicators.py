import unittest
import pandas as pd
import numpy as np
from core.indicators import Indicators

class TestIndicators(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        # Create a sample DataFrame
        self.length = 100
        dates = pd.date_range(start='2023-01-01', periods=self.length, freq='D')
        self.df = pd.DataFrame({
            'open': np.random.randn(self.length).cumsum() + 100,
            'high': np.random.randn(self.length).cumsum() + 105,
            'low': np.random.randn(self.length).cumsum() + 95,
            'close': np.random.randn(self.length).cumsum() + 100,
            'volume': np.random.randint(100, 1000, self.length)
        }, index=dates)
        
        # Ensure high is highest and low is lowest
        self.df['high'] = self.df[['open', 'close', 'high']].max(axis=1) + 1
        self.df['low'] = self.df[['open', 'close', 'low']].min(axis=1) - 1

    def test_sma(self):
        n = 20
        sma = Indicators.SMA(self.df['close'], n)
        
        # Check length
        self.assertEqual(len(sma), self.length)
        
        # Check NaNs
        self.assertTrue(sma.iloc[:n-1].isna().all(), "First n-1 values should be NaN")
        self.assertFalse(sma.iloc[n-1:].isna().any(), "Values from n-1 onwards should not be NaN")

    def test_atr(self):
        n = 14
        atr = Indicators.ATR(self.df, n)
        
        # Check length
        self.assertEqual(len(atr), self.length)
        
        # Check NaNs
        self.assertTrue(atr.iloc[:n-1].isna().all(), "First n-1 values should be NaN")
        self.assertFalse(atr.iloc[n-1:].isna().any(), "Values from n-1 onwards should not be NaN")
        
        # Check values are positive (ATR is always positive)
        self.assertTrue((atr.iloc[n:] > 0).all())

    def test_adx(self):
        n = 14
        adx = Indicators.ADX(self.df, n)
        
        # Check length
        self.assertEqual(len(adx), self.length)
        
        # Check NaNs
        self.assertTrue(adx.iloc[:n-1].isna().all(), "First n-1 values should be NaN")
        # ADX might have more NaNs depending on implementation (e.g. if using previous values), 
        # but our implementation explicitly sets first n-1 to NaN and calculates from index 0.
        # However, DX calculation involves division, so we should check if valid.
        # Given random data, division by zero is unlikely but possible. 
        # For this test with random floats, it should be fine.
        
        # Note: ADX calculation often stabilizes after 2*n bars. 
        # But strictly checking "first n-1 are NaN" as per requirement.
        self.assertTrue(adx.iloc[:n-1].isna().all())
        
        # Check values range (0-100)
        valid_adx = adx.dropna()
        self.assertTrue((valid_adx >= 0).all() and (valid_adx <= 100).all())

    def test_bbands(self):
        n = 20
        k = 2.0
        upper, mid, lower = Indicators.BBANDS(self.df['close'], n, k)
        
        # Check length
        self.assertEqual(len(upper), self.length)
        self.assertEqual(len(mid), self.length)
        self.assertEqual(len(lower), self.length)
        
        # Check NaNs
        self.assertTrue(upper.iloc[:n-1].isna().all())
        self.assertTrue(mid.iloc[:n-1].isna().all())
        self.assertTrue(lower.iloc[:n-1].isna().all())
        
        # Check Logic
        valid_indices = slice(n, None)
        self.assertTrue((upper.iloc[valid_indices] >= mid.iloc[valid_indices]).all())
        self.assertTrue((mid.iloc[valid_indices] >= lower.iloc[valid_indices]).all())

if __name__ == '__main__':
    unittest.main()
