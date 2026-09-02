import sys
import os
import argparse
import itertools
import json
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pandas as pd
try:
    from tabulate import tabulate
except ImportError:  # Optional CLI presentation dependency.
    tabulate = None

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from strategies.trend_breakout import TrendBreakoutStrategy, TrendBreakdownStrategy
from strategies.volatility import VolatilityReversionStrategy
from strategies.mean_reversion import RangeStrategy

from analysis.validation import ValidationConfig, validate_parameter_candidates
from analysis.walk_forward import WalkForwardConfig, run_walk_forward
from core.metrics import one_sided_bootstrap_p_value

#: The one grid both search modes read, so a walk-forward run and a full-sample
#: run are always comparing the same candidates.
ENTRY_WINDOWS = (20, 30, 50, 100)
EXIT_WINDOWS = (5, 10, 15, 20)


def build_optimization_strategies(entry_window: int, exit_window: int):
    """Build a registry whose keys exactly match params.yaml routing values."""
    return {
        "TrendBreakout": TrendBreakoutStrategy(entry_window, exit_window),
        "TrendBreakdown": TrendBreakdownStrategy(entry_window, exit_window),
        "RangeMeanReversion": RangeStrategy(),
        "VolatilityReversion": VolatilityReversionStrategy(),
    }


def _candidate_factory(entry_window: int, exit_window: int):
    """A zero-argument builder, as analysis.walk_forward requires.

    Every window must get fresh strategy objects: health and cooldown state
    persists across bars, so a reused instance would let one window's
    lifecycle decide what the next one is allowed to trade.
    """
    def build():
        return build_optimization_strategies(entry_window, exit_window)

    return build


def _resolve_period(days: int, start_date: str, end_date: str):
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    return start_date, end_date


def _load_data(
    symbols: List[str], data_source: str, days: int, start_date: str, end_date: str,
) -> Dict[str, Any]:
    start_date, end_date = _resolve_period(days, start_date, end_date)
    print(f"Period: {start_date} to {end_date} ({days} days)")
    print("Fetching data...")
    fetcher = DataFetcher()
    data_map: Dict[str, Any] = {}
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
    return data_map


def evaluate_one_candidate(task: tuple) -> Dict[str, Any]:
    """Run one parameter combination and return only picklable results.

    Top-level (not a closure) because Windows spawns worker processes rather
    than forking them, so the callable has to be importable by name. It
    returns plain values and a pandas Series - never the engine result, which
    carries the whole event log and order book.

    Indicators are deliberately not shared between candidates. They are
    recomputed per run at about 17ms against a ~3s bar loop on a 5-year,
    3-symbol set - 0.6% of the work - so caching them would thread a flag
    through two layers to buy nothing measurable. Parallelism is where the
    time actually is.
    """
    data_map, entry_window, exit_window, initial_capital = task
    engine = BacktestEngine(initial_capital=initial_capital)
    result = engine.run(
        data_map,
        strategies=build_optimization_strategies(entry_window, exit_window),
        routing_log_enabled=False,
    )
    metrics = ReportGenerator(tempfile.mkdtemp(prefix="opt_")).generate(
        trades=result["trades"],
        equity_curve=result["equity_curve"],
        benchmark_curve=result["benchmark"],
        metrics_only=True,
    )
    return {
        "name": f"entry={entry_window},exit={exit_window}",
        "Entry_Window": entry_window,
        "Exit_Window": exit_window,
        "Total_Ret%": (metrics.get("TotalReturn") or 0.0) * 100,
        "Max_DD%": (metrics.get("MaxDrawdownPct") or 0.0) * 100,
        "Sharpe": metrics.get("SharpeRatio") or 0.0,
        "Trades": metrics.get("TotalTrades", 0),
        "Win_Rate%": (metrics.get("WinRate") or 0.0) * 100,
        "returns": result["equity_curve"]["equity"].pct_change(
            fill_method=None
        ).dropna(),
    }


def _evaluate_grid(
    data_map: Dict[str, Any],
    param_grid: List[tuple],
    initial_capital: float,
    jobs: int,
) -> List[Dict[str, Any]]:
    """Evaluate every combination, in parallel when asked.

    Results are re-ordered to match ``param_grid`` regardless of completion
    order, so a run's table and its saved CSV do not depend on how the
    scheduler happened to interleave the workers.
    """
    tasks = [
        (data_map, entry_window, exit_window, initial_capital)
        for entry_window, exit_window in param_grid
    ]
    if jobs <= 1:
        return [evaluate_one_candidate(task) for task in tasks]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(evaluate_one_candidate, tasks))


def run_grid_search(
    symbols: List[str],
    data_source: str,
    days: int,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
    oos: bool = False,
    jobs: int = 1,
):
    print("\nStarting Grid Search Optimization...")
    print(f"Symbols: {symbols}")
    print(f"Data Source: {data_source}")
    data_map = _load_data(symbols, data_source, days, start_date, end_date)
    if not data_map:
        print("No data loaded. Aborting.")
        return

    # Same params for both trend directions, kept in one place so a
    # walk-forward run scores the identical candidate set.
    param_grid = list(itertools.product(ENTRY_WINDOWS, EXIT_WINDOWS))

    print(f"\nTesting {len(param_grid)} combinations on {jobs or 1} worker(s)...")
    print("-" * 60)

    evaluations = _evaluate_grid(data_map, param_grid, initial_capital, jobs)

    results = []
    returns_by_candidate = {}
    for row in evaluations:
        returns_by_candidate[row["name"]] = row["returns"]
        results.append({
            "Entry_Window": row["Entry_Window"],
            "Exit_Window": row["Exit_Window"],
            "Total_Ret%": row["Total_Ret%"],
            "Max_DD%": row["Max_DD%"],
            "Sharpe": row["Sharpe"],
            "Trades": row["Trades"],
            "Win_Rate%": row["Win_Rate%"],
        })
        print(
            f"Entry={row['Entry_Window']:<3} Exit={row['Exit_Window']:<3} | "
            f"Ret: {row['Total_Ret%']:>6.2f}% | DD: {row['Max_DD%']:>6.2f}% | "
            f"Sharpe: {row['Sharpe']:>5.2f}"
        )

    # 3. Display Results
    results_df = pd.DataFrame(results)

    # Sort by Sharpe Ratio
    results_df = results_df.sort_values(by="Sharpe", ascending=False)

    print("\n" + "=" * 80)
    print("Optimization Results (Sorted by Sharpe Ratio)")
    print("=" * 80)

    # Use tabulate if available, else string format
    if tabulate is not None:
        print(tabulate(results_df, headers="keys", tablefmt="grid", floatfmt=".2f"))
    else:
        print(results_df.to_string(index=False))

    # Save to CSV
    os.makedirs("reports", exist_ok=True)
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reports/optimization_{timestamp}.csv"
    results_df.to_csv(filename, index=False)
    print(f"\nResults saved to {filename}")
    if oos:
        # One hypothesis per candidate actually tried. This used to pass an
        # empty list, so the FDR correction ran over nothing while the grid
        # above tested len(param_grid) combinations - exactly the "best of N"
        # inflation the correction exists to remove.
        p_values = [
            one_sided_bootstrap_p_value(
                series, n_samples=ValidationConfig().bootstrap_samples,
                seed=ValidationConfig().seed,
            )["p_value"]
            for series in returns_by_candidate.values()
        ]
        validation = validate_parameter_candidates(
            returns_by_candidate,
            p_values=[value for value in p_values if value is not None],
            config=ValidationConfig(),
        )
        validation["candidate_p_values"] = dict(
            zip(returns_by_candidate, p_values)
        )
        validation["caveat"] = (
            "Candidates were ranked on the FULL sample and only then split, so "
            "the 'test' half was visible to selection. Use "
            "analysis/walk_forward.py for a selection that never sees its own "
            "test window."
        )
        validation_path = filename.replace(".csv", "_oos.json")
        with open(validation_path, "w", encoding="utf-8") as handle:
            json.dump(validation, handle, ensure_ascii=False, indent=2, default=str)
        print(f"OOS selection (train only): {validation['selected_candidate']}")
        print(
            "Multiple testing: "
            f"{validation['multiple_testing']['rejected_count']}"
            f"/{validation['multiple_testing']['sample_size']} candidates "
            f"survive FDR {ValidationConfig().fdr}"
        )
        print(f"OOS evidence saved to {validation_path}")
        print(
            "NOTE: this split is post-hoc. For a selection that never sees its "
            "test window run --walk-forward."
        )


def run_walk_forward_search(
    symbols: List[str],
    data_source: str,
    days: int,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
    train: int = 180,
    validation: int = 60,
    test: int = 60,
    purge: int = 5,
) -> None:
    """Grid search where each window's winner is chosen before its test bars."""
    data_map = _load_data(symbols, data_source, days, start_date, end_date)
    if not data_map:
        print("No data loaded. Aborting.")
        return

    candidates = {
        f"entry={entry},exit={exit_}": _candidate_factory(entry, exit_)
        for entry, exit_ in itertools.product(ENTRY_WINDOWS, EXIT_WINDOWS)
    }
    config = WalkForwardConfig(
        train_size=train, validation_size=validation, test_size=test,
        purge_size=purge, initial_capital=initial_capital,
    )
    print(
        f"\nWalk-forward over {len(candidates)} candidates "
        f"(train={train}, validation={validation}, test={test}, purge={purge})..."
    )
    report = run_walk_forward(data_map, candidates, config)

    for window in report["windows"]:
        print(
            f"  window {window['window']}: test {window['test_start'][:10]}"
            f"..{window['test_end'][:10]} selected={window['selected']} "
            f"return={window['test_return']}"
        )
    for skipped in report["skipped_windows"]:
        print(f"  window {skipped['window']}: skipped ({skipped['reason']})")

    procedure = report["procedure"]
    print(f"\nProcedure OOS total return: {procedure['total_return']}")
    print(
        "Selection stability: "
        f"{procedure['selection_stability'].get('distinct_selections')} distinct "
        f"choices over {procedure['selection_stability'].get('sample_size')} windows"
    )
    survivors = [
        name for name, row in report["candidates"].items() if row["survives_fdr"]
    ]
    print(
        f"Candidates surviving FDR {config.fdr}: "
        f"{len(survivors)}/{len(report['candidates'])}"
        + (f" ({', '.join(survivors)})" if survivors else "")
    )

    os.makedirs("reports", exist_ok=True)
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    path = f"reports/walk_forward_{timestamp}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(f"Walk-forward evidence saved to {path}")


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
    parser.add_argument(
        "--oos",
        action="store_true",
        help=(
            "emit post-hoc split evidence for the full-sample ranking "
            "(selection has already seen the test half - prefer --walk-forward)"
        ),
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="select per window before its test bars and re-run the engine on them",
    )
    parser.add_argument("--wf-train", type=int, default=180)
    parser.add_argument("--wf-validation", type=int, default=60)
    parser.add_argument("--wf-test", type=int, default=60)
    parser.add_argument("--wf-purge", type=int, default=5)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel worker processes for the full-sample grid (1 = serial)",
    )

    args = parser.parse_args()

    if args.walk_forward:
        run_walk_forward_search(
            symbols=args.symbols,
            data_source=args.source,
            days=args.days,
            start_date=args.start,
            end_date=args.end,
            initial_capital=args.capital,
            train=args.wf_train,
            validation=args.wf_validation,
            test=args.wf_test,
            purge=args.wf_purge,
        )
        return

    run_grid_search(
        symbols=args.symbols,
        data_source=args.source,
        days=args.days,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        oos=args.oos,
        jobs=max(1, args.jobs),
    )


if __name__ == "__main__":
    main()
