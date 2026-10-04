"""Command-line argument contract for historical backtests."""

import argparse


def build_backtest_parser(default_initial_capital: float) -> argparse.ArgumentParser:
    """Declare the public backtest CLI contract in one testable place."""
    parser = argparse.ArgumentParser(description="Quantitative Trading System Backtest")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Number of days to backtest (default: 365)",
    )
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=str,
                        help="New exclusive report directory for supervised automation.")
    parser.add_argument(
        "--capital", type=float, default=default_initial_capital, help="Initial capital (USDT)"
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
        choices=["synthetic", "yahoo", "ccxt", "local"],
        help="Data source",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Local OHLCV CSV directory for --source local (e.g. data/binance/1d).",
    )
    parser.add_argument("--temporal-mode", choices=["retrospective", "strict"], default=None,
                        help="Strategy data version policy; omitted keeps explicitly retrospective compatibility.")
    parser.add_argument("--temporal-knowledge", choices=["local", "published"], default=None)
    parser.add_argument("--temporal-unknown", choices=["exclude", "raise"], default=None)
    parser.add_argument("--data-version-store", default=None,
                        help="Immutable local raw-input and incremental OHLCV version directory.")
    parser.add_argument("--temporal-decision-delay-seconds", type=float, default=None,
                        help="Strict decision cutoff allowance after bar close; must be nonnegative.")
    parser.add_argument("--temporal-financing-evidence", default=None,
                        help="Versioned funding/borrow evidence JSON for strict research labels; frozen into the report.")
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
        help="Slippage rate (e.g. 0.001 for 0.1%%). If omitted, uses config execution.slippage_bps.",
    )
    parser.add_argument(
        "--random_slip",
        action="store_true",
        help="Enable random slippage (uniform distribution from 0 to --slippage)",
    )
    parser.add_argument(
        "--disable-routing-log",
        action="store_true",
        help="Disable per-bar routing CSV output for optimization runs.",
    )
    parser.add_argument(
        "--observe-signals", action="store_true",
        help="Export passive raw candidates, future outcomes, ghost replays and gate diagnostics.",
    )
    parser.add_argument("--smart-allocation", action="store_true",
                        help="Plan same-bar multi-coin capital using score, volatility and correlation.")
    parser.add_argument("--max-positions", type=int, default=None,
                        help="Maximum held plus pending coin positions (requires --smart-allocation).")
    parser.add_argument("--cash-reserve-pct", type=float, default=None,
                        help="Cash reserve / equity, e.g. 0.10 (requires --smart-allocation).")
    parser.add_argument(
        "--signal-meta-layer", action="store_true",
        help="Enable passive P1 walk-forward EV research; implies --observe-signals without changing orders.",
    )
    parser.add_argument(
        "--adaptive-signal-meta", action="store_true",
        help="Enable P2 frozen regime and dynamic-axis EV research; implies P0/P1 reporting.",
    )
    parser.add_argument(
        "--signal-meta-replay", action="store_true",
        help="Enable P3 independent baseline, gate and sizing account replays; implies P2 research.",
    )
    parser.add_argument(
        "--exchange", default="binance",
        help="Exchange identity for market data (default: binance).",
    )
    parser.add_argument(
        "--market-type", default=None, choices=["spot", "margin", "perpetual"],
        help="Override account mode; omitted uses config account.mode.",
    )
    parser.add_argument("--derivatives-contract-file", help="Local JSON identity for a quote-settled linear perpetual contract.")
    parser.add_argument("--derivatives-funding-file", help="Local actual settlement CSV; funding costs use a separate position-event replay.")
    parser.add_argument("--derivatives-observations-file", help="Local OI, mark/index and predicted-rate CSV with observed_at and available_at.")
    parser.add_argument("--derivatives-max-age", default="24h", help="Maximum observation age at the decision cutoff (default: 24h).")
    parser.add_argument(
        "--timeframe", default="1d",
        help="Bar timeframe and manifest identity (default: 1d).",
    )
    parser.add_argument(
        "--data-timezone", default="UTC",
        help="Timezone used to interpret requested data boundaries.",
    )
    parser.add_argument(
        "--alignment-mode", default="union", choices=["union", "intersection"],
        help="Multi-asset timeline alignment rule.",
    )
    parser.add_argument(
        "--benchmark-mode", default="fixed", choices=["fixed", "dynamic"],
        help="Benchmark shown in the primary report.",
    )
    parser.add_argument(
        "--benchmark-rebalance-cost-bps", type=float, default=5.0,
        help="Turnover cost for the dynamic equal-weight benchmark.",
    )
    parser.add_argument(
        "--universe-file",
        help="Point-in-time universe CSV with symbol,listed_at,delisted_at.",
    )
    parser.add_argument(
        "--secondary-data-dir",
        help="Independent OHLCV CSV directory for top winner/loser verification.",
    )
    parser.add_argument(
        "--require-secondary-audit", action="store_true",
        help="Return non-zero unless the top-trade second-source audit passes.",
    )
    parser.add_argument(
        "--replay-manifest",
        help="Re-run an existing run_manifest.json and compare deterministic outputs.",
    )
    parser.add_argument(
        "--report-profile", choices=["workbook", "compact", "full"], default="workbook",
        help=("workbook writes one Excel file; compact writes a PDF, dashboard PNG "
              "and core CSVs; full also writes the complete audit trail."),
    )
    return parser

