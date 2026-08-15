import sys
import os
import argparse
import itertools
import pandas as pd
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
from tabulate import tabulate

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from strategies.trend_following import TrendUpStrategy, TrendDownStrategy
from strategies.mean_reversion import RangeStrategy

from analysis.validation import ValidationConfig, validate_parameter_candidates

def run_grid_search(
    symbols: List[str],
    data_source: str,
    days: int,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
):
    print(f"\nStarting Grid Search Optimization...")
    print(f"Symbols: {symbols}")
    print(f"Data Source: {data_source}")
    oos: bool = False,

    # Calculate dates if not provided
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"Period: {start_date} to {end_date} ({days} days)")

    # 1. Fetch Data (Once)
    print("Fetching data...")
    fetcher = DataFetcher()
    data_map = {}

    for symbol in symbols:
        if data_source == "ccxt":
            df = fetcher.fetch_ccxt(
                symbol, limit=days, start_date=start_date, end_date=end_date
            )
        elif data_source == "yahoo":
            df = fetcher.fetch_yahoo(symbol, start_date, end_date)
        else:
            df = fetcher.generate_scenario(symbol, start_date, end_date)

        if df is not None and not df.empty:
            data_map[symbol] = df
            print(f"Loaded {len(df)} rows for {symbol}")
        else:
            print(f"Warning: No data for {symbol}")

    if not data_map:
        print("No data loaded. Aborting.")
        return

    # 2. Define Parameter Grid
    # Optimizing Trend Strategies (Up and Down)
    sma_periods = [20, 30, 50, 100]
    atr_multipliers = [1.5, 2.0, 2.5, 3.0]

    # We will use the same params for both TrendUp and TrendDown for simplicity in this run
    param_grid = list(itertools.product(sma_periods, atr_multipliers))

    results = []

    print(f"\nTesting {len(param_grid)} combinations...")
    returns_by_candidate = {}
    print("-" * 60)

    for sma, atr_mult in param_grid:
        # Create strategy instances with current params
        strategies = {
            "TrendUp": TrendUpStrategy(sma_period=sma, atr_multiplier=atr_mult),
            "TrendDown": TrendDownStrategy(sma_period=sma, atr_multiplier=atr_mult),
            # Keep RangeStrategy default as we are optimizing Trend
            "RangeMeanReversion": RangeStrategy(),
        }

        # Run Backtest
        engine = BacktestEngine(initial_capital=initial_capital)
        backtest_result = engine.run(data_map, strategies=strategies)
        
        # Calculate Metrics using ReportGenerator
        # We use a temp directory to avoid cluttering the main reports folder
        report_gen = ReportGenerator("reports/temp_opt")
        metrics = report_gen.generate(
            trades=backtest_result["trades"],
            equity_curve=backtest_result["equity_curve"],
            benchmark_curve=backtest_result["benchmark"],
            metrics_only=True,
        )
        returns_by_candidate[f"sma={sma},atr={atr_mult}"] = (
            backtest_result["equity_curve"]["equity"].pct_change().dropna()
        )
        
        # Store result
        results.append({
            "SMA_Period": sma,
            "ATR_Mult": atr_mult,
            "Total_Ret%": metrics.get("TotalReturn", 0.0) * 100,
            "Max_DD%": metrics.get("MaxDrawdownPct", 0.0) * 100,
            "Sharpe": metrics.get("SharpeRatio", 0.0),
            "Trades": metrics.get("TotalTrades", 0),
            "Win_Rate%": metrics.get("WinRate", 0.0) * 100
        })
        
        print(f"SMA={sma:<3} ATR={atr_mult:<3} | Ret: {metrics.get('TotalReturn', 0.0)*100:>6.2f}% | DD: {metrics.get('MaxDrawdownPct', 0.0)*100:>6.2f}% | Sharpe: {metrics.get('SharpeRatio', 0.0):>5.2f}")

    # 3. Display Results
    results_df = pd.DataFrame(results)

    # Sort by Sharpe Ratio
    results_df = results_df.sort_values(by="Sharpe", ascending=False)

    print("\n" + "=" * 80)
    print("Optimization Results (Sorted by Sharpe Ratio)")
    print("=" * 80)

    # Use tabulate if available, else string format
    try:
        print(tabulate(results_df, headers="keys", tablefmt="grid", floatfmt=".2f"))
    except ImportError:
        print(results_df.to_string(index=False))

    # Save to CSV
    os.makedirs("reports", exist_ok=True)
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reports/optimization_{timestamp}.csv"
    results_df.to_csv(filename, index=False)
    print(f"\nResults saved to {filename}")
    if oos:
        validation = validate_parameter_candidates(
            returns_by_candidate, p_values=[], config=ValidationConfig()
        )
        validation_path = filename.replace(".csv", "_oos.json")
        with open(validation_path, "w", encoding="utf-8") as handle:
            json.dump(validation, handle, ensure_ascii=False, indent=2, default=str)
        print(f"OOS selection (train only): {validation['selected_candidate']}")
        print(f"OOS evidence saved to {validation_path}")


def main():
    parser = argparse.ArgumentParser(description="Strategy Parameter Optimization")
    parser.add_argument(
        "--symbols", nargs="+", default=["BTC-USDT"], help="Symbols to test"
    )
    parser.add_argument("--days", type=int, default=365, help="Days of data")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--source",
        type=str,
        default="synthetic",
        choices=["synthetic", "yahoo", "ccxt"],
    )
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--oos", action="store_true", help="emit untouched OOS evidence")

    args = parser.parse_args()

    run_grid_search(
        symbols=args.symbols,
        data_source=args.source,
        days=args.days,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        oos=args.oos,
    )


if __name__ == "__main__":
    main()
