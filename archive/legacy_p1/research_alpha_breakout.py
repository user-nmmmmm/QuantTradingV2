# HISTORICAL ONLY: this script may differ from backtest.engine.BacktestEngine.
# Do not use it for trading or investment decisions without migration and baseline tests.

import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data import DataHandler


class DonchianBreakoutAlpha:
    """
    P1: Minimum Viable Alpha (Research Layer)
    Direction: Trend Acceleration (Breakout)

    Logic:
    - Entry: Close > Max(High, window)
    - Exit: Close < Min(Low, window/2)
    - Filter: No filters (Pure Price Action)
    """

    def __init__(self, entry_window=20, exit_window=10):
        self.entry_window = entry_window
        self.exit_window = exit_window

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Vectorized signal generation.
        Returns DataFrame with 'signal' (1=Long, 0=None, -1=Short... usually just 1 for Long-Only)
        """
        # Avoid SettingWithCopyWarning
        df = df.copy()

        # Donchian Channels
        df["donchian_high"] = (
            df["high"].rolling(window=self.entry_window).max().shift(1)
        )
        df["donchian_low"] = df["low"].rolling(window=self.exit_window).min().shift(1)

        # Signals
        # 1 = Long Entry, 0 = Hold/None, -1 = Exit
        # We need stateful logic for "Hold", so pure vector is tricky for PnL without a loop or specialized function.
        # But for "Signal Definition", we can define conditions.

        # Entry Condition
        long_entry = df["close"] > df["donchian_high"]

        # Exit Condition
        long_exit = df["close"] < df["donchian_low"]

        # Vectorized Position Calculation (fill forward)
        # 1 when Entry, 0 when Exit, NaN otherwise
        df["signal_raw"] = np.nan
        df.loc[long_entry, "signal_raw"] = 1
        df.loc[long_exit, "signal_raw"] = 0

        # Forward fill to simulate holding
        df["position"] = df["signal_raw"].ffill().fillna(0)

        return df

    def run_backtest(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Simple Vectorized Backtest (P1 Requirement: Naked Backtest)
        """
        df = self.generate_signals(df)

        # Calculate Returns
        df["returns"] = df["close"].pct_change()

        # Strategy Returns (Lagged Position * Returns)
        # Position calculated at Close(t) determines exposure for t+1
        df["strategy_returns"] = df["position"].shift(1) * df["returns"]

        # Cumulative Returns
        df["cumulative_returns"] = (1 + df["strategy_returns"]).cumprod()
        df["benchmark_returns"] = (1 + df["returns"]).cumprod()

        return df


def plot_performance(df, title="Alpha P1: Donchian Breakout"):
    plt.figure(figsize=(12, 6))
    plt.plot(df.index, df["cumulative_returns"], label="Strategy")
    plt.plot(df.index, df["benchmark_returns"], label="Benchmark (Buy&Hold)", alpha=0.5)
    plt.title(title)
    plt.legend()
    plt.grid(True)

    # Save plot
    output_path = os.path.join(os.path.dirname(__file__), "p1_breakout_result.png")
    plt.savefig(output_path)
    print(f"Plot saved to {output_path}")


def main():
    print("--- P1: Trend Breakout Alpha (Naked Backtest) ---")

    # 1. Get Data (Use Synthetic for P1 or Yahoo/CCXT if available)
    # Let's use DataHandler to generate synthetic trend data for validation
    # Or fetch real data if possible. Since environment has synthetic default, let's try to fetch BTC.
    # But for "Research", let's be self-contained.

    # Generate Synthetic Data with Trend
    print("Generating Synthetic Data...")
    from core.data import DataHandler

    # Create dummy config for generation
    # We will just manually create a dataframe or use main.py's generator?
    # Better: Use DataHandler if it has generation capability exposed.
    # Checking core/data.py... it does not seem to have 'generate_synthetic' directly exposed as static method easily without looking deep.
    # Let's write a simple generator here to be safe and independent.

    dates = pd.date_range(start="2020-01-01", end="2025-01-01", freq="D")
    n = len(dates)

    # Random Walk with Drift (Trend) + Volatility Clusters
    np.random.seed(42)
    returns = np.random.normal(loc=0.0005, scale=0.02, size=n)  # Positive drift
    price = 100 * (1 + returns).cumprod()

    # Create OHLC
    # High/Low derived from Close with noise
    high = price * (1 + np.abs(np.random.normal(0, 0.01, n)))
    low = price * (1 - np.abs(np.random.normal(0, 0.01, n)))
    open_p = price * (1 + np.random.normal(0, 0.005, n))  # noisy open

    # Fix High/Low/Open consistency
    high = np.maximum(high, np.maximum(open_p, price))
    low = np.minimum(low, np.minimum(open_p, price))

    df = pd.DataFrame(
        {
            "open": open_p,
            "high": high,
            "low": low,
            "close": price,
            "volume": np.random.randint(100, 1000, n),
        },
        index=dates,
    )

    print(f"Data Generated: {len(df)} bars")

    # 2. Run Strategy
    alpha = DonchianBreakoutAlpha(entry_window=20, exit_window=10)
    res = alpha.run_backtest(df)

    # 3. Analyze
    total_ret = res["cumulative_returns"].iloc[-1] - 1
    sharpe = (
        res["strategy_returns"].mean() / res["strategy_returns"].std() * np.sqrt(252)
    )
    max_dd = (res["cumulative_returns"] / res["cumulative_returns"].cummax() - 1).min()

    print(f"Total Return: {total_ret * 100:.2f}%")
    print(f"Sharpe Ratio: {sharpe:.2f}")
    print(f"Max Drawdown: {max_dd * 100:.2f}%")

    # Verify Acceptance Criteria
    alive = total_ret > 0 and max_dd > -0.5  # Simple heuristic
    print(f"Status: {'ALIVE' if alive else 'DEAD'}")

    plot_performance(res)


if __name__ == "__main__":
    main()
