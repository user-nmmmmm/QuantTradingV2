import sys
import os
import shutil
import argparse
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.data_fetcher import DataFetcher
from core.data import DataHandler
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from core.logger import get_logger

logger = get_logger(__name__)

"""
项目入口（回测 CLI）

用途：
- 通过命令行或交互模式配置数据源、标的、日期区间、滑点等参数
- 拉取/生成数据后运行 BacktestEngine
- 生成 reports/<timestamp>_* 目录下的回测报告

数据源：
- synthetic：内置情景数据（用于快速演示与本地验证）
- yahoo：Yahoo Finance 日线
- ccxt：交易所 K 线（需网络与交易所支持）
"""


def get_data(
    symbol: str, start: str, end: str, source: str = "synthetic", days: int = 365
) -> pd.DataFrame:
    """
    获取单标的数据（封装 DataFetcher 的多源实现）。

    参数：
    - symbol：标的代码（不同数据源格式不同）
    - start/end：日期字符串（YYYY-MM-DD）
    - source：synthetic/yahoo/ccxt
    - days：回测天数（ccxt 作为 limit 的近似）
    """
    fetcher = DataFetcher()

    if source == "ccxt":
        return fetcher.fetch_ccxt(symbol, limit=days, start_date=start, end_date=end)
    elif source == "yahoo":
        return fetcher.fetch_yahoo(symbol, start, end)
    else:
        # Synthetic / Scenario
        return fetcher.generate_scenario(symbol, start, end)


def main():
    """
    命令行入口：
    1) 解析参数（无参数则进入交互模式）
    2) 拉取数据并生成数据质量报告（DataHandler.generate_quality_report）
    3) 运行回测（BacktestEngine.run）
    4) 输出报告（ReportGenerator.generate），并整理 routing_log/data_quality_report
    """
    parser = argparse.ArgumentParser(description="Quantitative Trading System Backtest")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Number of days to backtest (default: 365)",
    )
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--capital", type=float, default=1000.0, help="Initial capital (USDT)"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC-USDT", "ETH-USDT"],
        help="List of symbols to trade (default: BTC-USDT ETH-USDT)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="synthetic",
        choices=["synthetic", "yahoo", "ccxt"],
        help="Data source",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=None,
        help="Slippage rate (e.g. 0.001 for 0.1%). If omitted, uses config execution.slippage_bps.",
    )
    parser.add_argument(
        "--random_slip",
        action="store_true",
        help="Enable random slippage (uniform distribution from 0 to --slippage)",
    )

    # Check for interactive mode (no args provided)
    if len(sys.argv) == 1:
        print("\n" + "=" * 40)
        print("   QuantTrading Interactive Mode")
        print("=" * 40)
        print("No arguments provided. Please select options:\n")

        # 1. Source
        print("Select Data Source:")
        print("1. synthetic (Default)")
        print("2. yahoo")
        print("3. ccxt")
        source_choice = input("Enter choice [1-3] or name: ").strip().lower()

        source_map = {"1": "synthetic", "2": "yahoo", "3": "ccxt", "": "synthetic"}
        source = source_map.get(source_choice, source_choice)
        if source not in ["synthetic", "yahoo", "ccxt"]:
            print(f"Invalid source '{source}', defaulting to synthetic.")
            source = "synthetic"

        # 2. Symbols
        default_syms = "BTC-USDT ETH-USDT"
        syms_input = input(
            f"Enter symbols (space separated) [Default: {default_syms}]: "
        ).strip()
        if not syms_input:
            symbols = default_syms.split()
        else:
            symbols = syms_input.split()

        # 3. Capital
        cap_input = input("Enter Initial Capital (USDT) [Default: 1000]: ").strip()
        capital = cap_input if cap_input else "1000"

        # 4. Date Range or Days
        print("\nTime Period Configuration:")
        print("1. Last N Days (Default)")
        print("2. Specific Date Range")
        time_choice = input("Enter choice [1-2]: ").strip()

        start_arg = None
        end_arg = None
        days_arg = "365"

        if time_choice == "2":
            start_arg = input("Enter Start Date (YYYY-MM-DD): ").strip()
            end_arg = input("Enter End Date (YYYY-MM-DD): ").strip()
        else:
            d_input = input("Enter Days to Backtest [Default: 365]: ").strip()
            if d_input:
                days_arg = d_input

        # 5. Slippage
        slip_input = input(
            "Enter Slippage (e.g. 0.001 for 0.1%) [Default: config]: "
        ).strip()
        slippage = slip_input if slip_input else None

        # 6. Random Slip
        rand_slip_input = (
            input("Enable Random Slippage? (y/n) [Default: n]: ").strip().lower()
        )
        random_slip = rand_slip_input.startswith("y")

        # Construct args list
        cmd_args = [
            "--source",
            source,
            "--capital",
            capital,
            "--symbols",
        ] + symbols

        if slippage is not None:
            cmd_args.extend(["--slippage", slippage])

        if start_arg and end_arg:
            cmd_args.extend(["--start", start_arg, "--end", end_arg])
        else:
            cmd_args.extend(["--days", days_arg])

        if random_slip:
            cmd_args.append("--random_slip")

        print(f"\nRunning with: {' '.join(cmd_args)}")
        print("-" * 40 + "\n")

        args = parser.parse_args(cmd_args)
    else:
        args = parser.parse_args()

    if args.seed is not None:
        np.random.seed(args.seed)
        random.seed(args.seed)
        print(f"Random seed set to {args.seed}")

    print("Starting Quantitative Trading System...")
    print(f"Current Working Directory: {os.getcwd()}")

    # Determine Date Range
    if args.start and args.end:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d")
            end_date = datetime.strptime(args.end, "%Y-%m-%d")
            if start_date >= end_date:
                print("Error: Start date must be before end date.")
                return
            args.days = (end_date - start_date).days
            print(f"Config: Date Range={args.start} to {args.end} ({args.days} days)")
        except ValueError:
            print("Error: Invalid date format. Please use YYYY-MM-DD.")
            return
    else:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=args.days)
        print(
            f"Config: Last {args.days} Days (Auto-calculated: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})"
        )

    print(
        f"Config: Capital={args.capital}, Symbols={args.symbols}, Source={args.source}, Slippage={args.slippage if args.slippage is not None else 'config'}, RandomSlip={args.random_slip}"
    )

    # 1. Fetch Data
    # start_date and end_date are already set above

    # Test with Crypto pairs
    symbols = args.symbols
    data_map = {}

    for sym in symbols:
        df = get_data(
            sym,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            args.source,
            args.days,
        )

        if not df.empty and len(df) > 10:  # Lower limit for short tests
            print(f"Loaded {sym}: {len(df)} bars")
            data_map[sym] = df
        else:
            print(f"Failed to load sufficient data for {sym}")

    if not data_map:
        print("No data available. Exiting.")
        return

    # 1.1 Generate Data Quality Report
    print("Generating Data Quality Report...")
    # Store report in memory to save later in the specific backtest folder
    quality_report = DataHandler.generate_quality_report(data_map, output_path=None)

    # 2. Run Backtest
    print("\nInitializing Backtest Engine...")
    engine = BacktestEngine(
        initial_capital=args.capital,
        slippage=args.slippage,
        random_slip=args.random_slip,
    )

    print("Running Backtest...")
    # Use a temporary path for routing log, to be moved later
    if not os.path.exists(os.path.join(os.getcwd(), "reports")):
        os.makedirs(os.path.join(os.getcwd(), "reports"))
    temp_routing_log = os.path.join(os.getcwd(), "reports", "temp_routing_log.csv")
    results = engine.run(data_map, routing_log_path=temp_routing_log)

    if not results or results["equity_curve"].empty:
        print("\n" + "!" * 50)
        print("ERROR: Backtest failed or produced no results.")
        print("Possible causes:")
        print("1. No common timeframe found between symbols (check start/end dates).")
        print("2. Data fetching failed for some symbols.")
        print("3. Strategy produced no trades and no equity updates.")
        print("!" * 50 + "\n")
        # Cleanup temp log
        if os.path.exists(temp_routing_log):
            try:
                os.remove(temp_routing_log)
            except OSError as e:
                logger.warning("Failed to remove temp routing log %s: %s", temp_routing_log, e)
        return

    # 3. Generate Report
    print("\nGenerating Report...")

    # Calculate basic return for naming
    equity = results["equity_curve"]["equity"]
    total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
    return_str = f"Ret{total_return * 100:.1f}pct"

    # Naming convention: YYYYMMDD_HHMMSS_{Days}d_{Syms}Syms_{Ret}pct
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    symbols_str = f"{len(args.symbols)}Syms"
    days_str = f"{args.days}d"

    folder_name = f"{timestamp}_{days_str}_{symbols_str}_{return_str}"
    output_dir = os.path.join(os.getcwd(), "reports", folder_name)

    reporter = ReportGenerator(output_dir)

    # Prepare metadata
    metadata = {
        "Days": args.days,
        "Start": start_date.strftime("%Y-%m-%d"),
        "End": end_date.strftime("%Y-%m-%d"),
        "Capital": args.capital,
        "Symbols": ", ".join(args.symbols),
        "Source": args.source,
    }

    metrics = reporter.generate(
        results["trades"],
        results["equity_curve"],
        metadata=metadata,
        benchmark_curve=results.get("benchmark"),
    )

    # Save Data Quality Report
    dq_report_path = os.path.join(output_dir, "data_quality_report.json")
    try:
        import json

        with open(dq_report_path, "w") as f:
            json.dump(quality_report, f, indent=4, default=str)
        print(f"Data quality report saved to {dq_report_path}")
    except Exception as e:
        print(f"Failed to save data quality report: {e}")

    # Move Routing Log
    final_routing_log = os.path.join(output_dir, "routing_log.csv")
    if os.path.exists(temp_routing_log):
        try:
            shutil.move(temp_routing_log, final_routing_log)
            print(f"Routing log moved to {final_routing_log}")
        except Exception as e:
            print(f"Failed to move routing log: {e}")

    print("\n" + "=" * 30)
    print("BACKTEST RESULTS")
    print("=" * 30)
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k:<35}: {v:.4f}")
        else:
            print(f"{k:<35}: {v}")
    print("=" * 30)

    print(f"\nReport saved to: {output_dir}")


if __name__ == "__main__":
    main()
