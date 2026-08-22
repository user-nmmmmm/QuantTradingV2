import unittest
import pandas as pd
import numpy as np
from core.state import MarketStateMachine, MarketState

class TestMarketStateMachine(unittest.TestCase):
    def setUp(self):
        # Setup basic DataFrame structure
        self.length = 100
        dates = pd.date_range(start='2023-01-01', periods=self.length, freq='D')
        self.df = pd.DataFrame(index=dates)
        self.df['close'] = 100.0
        self.df['high'] = 105.0
        self.df['low'] = 95.0
        self.df['open'] = 100.0
        self.df['volume'] = 1000
        
        self.fsm = MarketStateMachine(stability_period=3)

    def test_stability_filter(self):
        # Create a raw state series
        # Sequence: 
        # 0-9: SIDEWAYS
        # 10-11: BULL (2 bars) -> Should remain SIDEWAYS
        # 12-19: SIDEWAYS
        # 20-24: BULL (5 bars) -> Should switch to BULL at 22 (after 3rd BULL bar: 20, 21, 22)
        # 25-29: SIDEWAYS (5 bars) -> Should switch to SIDEWAYS at 27
        
        raw_states_list = [MarketState.SIDEWAYS] * 10 + \
                          [MarketState.BULL_TREND] * 2 + \
                          [MarketState.SIDEWAYS] * 8 + \
                          [MarketState.BULL_TREND] * 5 + \
                          [MarketState.SIDEWAYS] * 5
        
        raw_states = pd.Series(raw_states_list)
        
        stable_states = self.fsm._apply_stability_filter(raw_states)
        
        # Check 0-19: Should all be SIDEWAYS
        # Because 10-11 (BULL) only lasted 2 bars, not enough to switch
        # Wait, my implementation starts with SIDEWAYS.
        # If raw is SIDEWAYS, it matches current stable (SIDEWAYS), so it stays SIDEWAYS.
        # At 10: Raw=BULL, Stable=SIDEWAYS, Count=1. Stable stays SIDEWAYS.
        # At 11: Raw=BULL, Stable=SIDEWAYS, Count=2. Stable stays SIDEWAYS.
        # At 12: Raw=SIDEWAYS, Stable=SIDEWAYS. Reset Count. Stable stays SIDEWAYS.
        
        # Verify 0-19
        self.assertTrue((stable_states.iloc[0:20] == MarketState.SIDEWAYS).all())
        
        # Check 20-24 (BULL for 5 bars)
        # 20: Raw=BULL, Count=1, Stable=SIDEWAYS
        # 21: Raw=BULL, Count=2, Stable=SIDEWAYS
        # 22: Raw=BULL, Count=3 -> Switch to BULL. Stable=BULL.
        # 23: Raw=BULL, Stable=BULL.
        # 24: Raw=BULL, Stable=BULL.
        
        self.assertEqual(stable_states.iloc[20], MarketState.SIDEWAYS)
        self.assertEqual(stable_states.iloc[21], MarketState.SIDEWAYS)
        self.assertEqual(stable_states.iloc[22], MarketState.BULL_TREND)
        self.assertEqual(stable_states.iloc[23], MarketState.BULL_TREND)
        self.assertEqual(stable_states.iloc[24], MarketState.BULL_TREND)
        
        # Check 25-29 (SIDEWAYS for 5 bars)
        # 25: Raw=SIDEWAYS, Count=1, Stable=BULL
        # 26: Raw=SIDEWAYS, Count=2, Stable=BULL
        # 27: Raw=SIDEWAYS, Count=3 -> Switch to SIDEWAYS.
        # 28: Raw=SIDEWAYS, Stable=SIDEWAYS
        # 29: Raw=SIDEWAYS, Stable=SIDEWAYS
        
        self.assertEqual(stable_states.iloc[25], MarketState.BULL_TREND)
        self.assertEqual(stable_states.iloc[26], MarketState.BULL_TREND)
        self.assertEqual(stable_states.iloc[27], MarketState.SIDEWAYS)
        self.assertEqual(stable_states.iloc[28], MarketState.SIDEWAYS)
        self.assertEqual(stable_states.iloc[29], MarketState.SIDEWAYS)

    def test_get_states_integration(self):
        # Construct data to trigger BULL_TREND
        # We need Price > MA20 > MA60 and ADX > 25
        # Let's create a strong uptrend
        
        # 200 days
        dates = pd.date_range(start='2023-01-01', periods=200, freq='D')
        df = pd.DataFrame(index=dates)
        
        # Linear uptrend: Price increases by 1 every day
        # MA20 will be approx Price - 10
        # MA60 will be approx Price - 30
        # So Price > MA20 > MA60 will hold after 60 days
        df['close'] = np.arange(200) + 100.0
        df['open'] = df['close']
        df['high'] = df['close'] + 2
        df['low'] = df['close'] - 2
        df['volume'] = 1000
        
        # To get ADX > 25, we need trendiness.
        # A steady uptrend should produce high ADX.
        # Let's verify if perfect linear trend gives high ADX.
        # TR will be constant. +DM will be constant. -DM will be 0.
        # So +DI will be high, -DI will be 0. DX will be 100. ADX will be 100.
        
        # Get states
        states = self.fsm.get_states(df)
        
        # First 60 days should be SIDEWAYS (due to NaNs in MA60)
        # Actually my code returns SIDEWAYS if len < 60, but here len=200.
        # But inside get_states, MA60 has NaNs for first 59 indices.
        # Raw State logic: (close > ma20) & ...
        # Comparisons with NaN result in False.
        # So first 59 should be SIDEWAYS.
        
        self.assertTrue((states.iloc[:59] == MarketState.SIDEWAYS).all())
        
        # After warmup (say index 80 to be safe), it should be BULL_TREND
        # Stability period is 3.
        # So it should switch pretty quickly after conditions are met.
        
        self.assertEqual(states.iloc[-1], MarketState.BULL_TREND)
        
        # Verify it stays BULL
        self.assertTrue((states.iloc[100:] == MarketState.BULL_TREND).all())

    def _wide_range_uptrend(self, periods: int = 200) -> pd.DataFrame:
        """Clean uptrend whose bars are wide enough to clear any ATR% gate.

        High-low spans ~20% of price, so atr_pct comfortably exceeds the
        VOLATILE threshold on every bar — the exact condition under which
        VOLATILE used to swallow the trend.
        """
        dates = pd.date_range(start="2023-01-01", periods=periods, freq="D")
        df = pd.DataFrame(index=dates)
        df["close"] = np.arange(periods) + 100.0
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.10
        df["low"] = df["close"] * 0.90
        df["volume"] = 1000
        return df

    def test_volatile_does_not_swallow_a_clean_trend(self):
        """A structurally clean trend stays TREND_UP even when ATR% is huge.

        Regression guard: VOLATILE shares the ADX gate with TREND_UP/DOWN and
        is assigned last. Without an explicit exclusion it overrode both, which
        erased 96%+ of trend bars on real crypto data (ATR ~4.4% of price vs a
        2.5% threshold) and left the trend strategies permanently unrouted.
        """
        fsm = MarketStateMachine(stability_period=3, atr_pct_threshold=0.025)
        df = self._wide_range_uptrend()

        states = fsm.calculate_states(df)

        atr_pct = df[f"ATR_{fsm.atr_period}"] / df["close"]
        self.assertGreater(
            atr_pct.iloc[-1], fsm.atr_pct_threshold,
            "fixture must clear the ATR gate for this test to mean anything",
        )
        self.assertEqual(states.iloc[-1], MarketState.TREND_UP)
        self.assertNotIn(
            MarketState.VOLATILE, set(states.iloc[100:]),
            "VOLATILE must not claim bars that a directional trend already owns",
        )

    def test_states_are_mutually_exclusive(self):
        """Every bar resolves to exactly one state — no silent override chain."""
        fsm = MarketStateMachine(stability_period=1)
        df = self._wide_range_uptrend(300)
        # Second half reverses into a downtrend so several states are exercised.
        df.iloc[150:, df.columns.get_loc("close")] = (
            np.arange(150, 0, -1) + 100.0
        )
        df["high"] = df["close"] * 1.10
        df["low"] = df["close"] * 0.90
        df["open"] = df["close"]

        states = fsm.calculate_states(df)

        valid = {
            MarketState.TREND_UP, MarketState.TREND_DOWN,
            MarketState.SIDEWAYS, MarketState.VOLATILE,
        }
        self.assertTrue(set(states).issubset(valid))
        self.assertEqual(len(states), len(df), "one state per bar, no gaps")


if __name__ == '__main__':
    unittest.main()
