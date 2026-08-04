# HISTORICAL ONLY: this script may differ from backtest.engine.BacktestEngine.
# Do not use it for trading or investment decisions without migration and baseline tests.

import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data_fetcher import DataFetcher
from research.alpha_breakout import DonchianBreakoutAlpha


def fetch_real_data(symbol="BTC/USDT", start_date="2020-01-01", end_date="2024-12-31"):
    fetcher = DataFetcher()
    print(f"Fetching real data for {symbol} ({start_date} to {end_date})...")
    df = fetcher.fetch_ccxt(
        symbol, start_date=start_date, end_date=end_date, limit=1000
    )

    if df.empty:
        print(
            "Fetch failed or returned empty. Falling back to synthetic scenario mimicking market cycles."
        )
        return generate_market_cycles(start_date, end_date)

    return df


def generate_market_cycles(start_date, end_date):
    """
    Mock 2020-2024 cycles if fetch fails.
    2020: Volatile Bull
    2021: Double Top Bull
    2022: Bear
    2023: Recovery/Crab
    2024: Bull
    """
    dates = pd.date_range(start=start_date, end=end_date, freq="D")
    n = len(dates)

    # Simple sine wave + drift + noise to mimic cycles
    t = np.linspace(0, 4 * np.pi, n)  # 2 cycles?

    # Trend component (Long term up)
    trend = np.linspace(0, 1.5, n)

    # Cycle component (Bull/Bear)
    cycle = 0.5 * np.sin(t)  # Reduced amplitude

    # Noise (Crypto daily vol ~3-4%)
    noise = np.random.normal(0, 0.03, n)

    price_path = 10000 * np.exp(trend + cycle + noise)

    # OHLC
    close = price_path
    high = close * 1.02
    low = close * 0.98
    open_p = close  # simplified

    df = pd.DataFrame(
        {"open": open_p, "high": high, "low": low, "close": close, "volume": 1000},
        index=dates,
    )
    return df


def run_reality_check():
    print("--- P2: Reality Check (Costs & Stress Test) ---")

    # 1. Get Data
    df = fetch_real_data()
    print(f"Data Loaded: {len(df)} bars")

    if len(df) == 0:
        print("Error: No data.")
        return

    # 2. Run Strategy
    alpha = DonchianBreakoutAlpha(entry_window=20, exit_window=10)
    res = alpha.generate_signals(df)

    # 3. Apply Costs
    # Cost Model: 0.1% per side (0.05% Comm + 0.05% Slip) => 0.001
    cost_per_side = 0.001

    # Identify Trades
    # Entry: Position 0 -> 1
    # Exit: Position 1 -> 0
    # (We ignore size scaling for P2, assuming 100% equity usage)

    res["pos_change"] = res["position"].diff().fillna(0)
    res["turnover"] = res["pos_change"].abs()  # 1.0 on entry, 1.0 on exit

    # Raw Returns
    res["returns"] = res["close"].pct_change().fillna(0)
    res["strategy_gross"] = res["position"].shift(1) * res["returns"]

    # Net Returns = Gross - Cost
    # Cost is paid on the day the trade occurs (Entry or Exit)
    # Note: If we enter at Close, we pay cost at Close.
    res["cost"] = res["turnover"] * cost_per_side
    res["strategy_net"] = res["strategy_gross"] - res["cost"]

    res["cum_gross"] = (1 + res["strategy_gross"]).cumprod()
    res["cum_net"] = (1 + res["strategy_net"]).cumprod()
    res["benchmark"] = (1 + res["returns"]).cumprod()

    # 4. Annual Metrics
    res["year"] = res.index.year
    years = res["year"].unique()

    print("\n=== Annual Performance (Net of 0.2% Round-Trip Cost) ===")
    print(
        f"{'Year':<6} | {'Return':<8} | {'MaxDD':<8} | {'Trades':<6} | {'Status':<10}"
    )
    print("-" * 55)

    all_alive = True

    for year in years:
        y_df = res[res["year"] == year]
        if len(y_df) < 10:
            continue

        y_ret = (1 + y_df["strategy_net"]).prod() - 1

        # MaxDD within year
        cum = (1 + y_df["strategy_net"]).cumprod()
        peak = cum.cummax()
        dd = (cum / peak - 1).min()

        trades = (y_df["turnover"] > 0).sum() / 2  # Approx round trips

        # Criteria: "Alive" means not catastrophic loss (e.g. > -20% or profitable)
        # User: "Signs of life in different years", "Not dead under real costs"
        status = "✅ OK" if y_ret > -0.15 else "⚠️ WEAK"
        if y_ret < -0.30:
            status = "❌ DEAD"

        print(
            f"{year:<6} | {y_ret * 100:6.1f}% | {dd * 100:6.1f}% | {trades:6.1f} | {status}"
        )

        if status == "❌ DEAD":
            all_alive = False

    # Total Stats
    total_ret = res["cum_net"].iloc[-1] - 1
    max_dd = (res["cum_net"] / res["cum_net"].cummax() - 1).min()
    print("-" * 55)
    print(
        f"TOTAL  | {total_ret * 100:6.1f}% | {max_dd * 100:6.1f}% | {(res['turnover'] > 0).sum() / 2} Trades"
    )

    if all_alive and total_ret > 0:
        print("\n🏆 P2 RESULT: PASSED (Alpha survives costs & stress years)")
    else:
        print("\n⚠️ P2 RESULT: MARGINAL/FAILED (Refinement needed)")

    # Plot
    plt.figure(figsize=(12, 6))
    plt.plot(res.index, res["cum_net"], label="Net Strategy (0.2% Cost)")
    plt.plot(res.index, res["benchmark"], label="Benchmark", alpha=0.3)
    plt.title("P2: Reality Check (Net Performance)")
    plt.yscale("log")
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(os.path.dirname(__file__), "p2_reality_result.png"))
    print("Plot saved.")


if __name__ == "__main__":
    run_reality_check()
