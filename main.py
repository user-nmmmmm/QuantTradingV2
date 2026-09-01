import sys
import os
import shutil
import argparse
import random
import json
import re
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.data_fetcher import DataFetcher
from core.data import DataHandler
from core.backtest_audit import (
    cross_verify_top_trades,
    validate_audit_coverage,
    write_event_log,
    write_json_report,
)
from core.reproducibility import (
    artifact_hashes,
    build_run_manifest,
    data_identity,
    deterministic_result_digest,
    load_data_snapshots,
    load_manifest,
    runtime_identity,
    save_data_snapshots,
    sha256_file,
    write_manifest,
)
from core.universe import PointInTimeUniverse, static_universe_manifest
from backtest.engine import BacktestEngine, DEFAULT_INITIAL_CAPITAL
from backtest.reporting import ReportGenerator, format_primary_metrics
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


def _load_local_ohlcv(symbol: str, start: str, end: str, data_dir: str) -> pd.DataFrame:
    """
    从本地缓存目录读取单标的 OHLCV CSV（由 scripts/fetch_binance_data.py 生成）。

    - 文件名按 symbol 归一化匹配，兼容 BTC/USDT、BTC-USDT、BTC_USDT 等写法。
    - 读入后按 [start, end] 半开区间（end 次日 00:00 之前）裁剪。
    """
    root = Path(data_dir)
    if not root.is_dir():
        raise ValueError(f"Local data directory does not exist: {root}")

    candidates = [
        root / f"{_safe_symbol_name(symbol)}.csv",
        root / f"{symbol.replace('/', '_').replace('-', '_').replace(':', '_')}.csv",
        root / f"{symbol}.csv",
    ]
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        raise FileNotFoundError(
            f"No local CSV for {symbol} in {root} (tried: {', '.join(c.name for c in candidates)})"
        )

    df = DataHandler.load_csv(str(path))
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
    return df[(df.index >= start_ts) & (df.index < end_ts)]


def get_data(
    symbol: str,
    start: str,
    end: str,
    source: str = "synthetic",
    days: int = 365,
    *,
    exchange: str = "binance",
    timeframe: str = "1d",
    data_timezone: str = "UTC",
    data_dir: str | None = None,
) -> pd.DataFrame:
    """
    获取单标的数据（封装 DataFetcher 的多源实现）。

    参数：
    - symbol：标的代码（不同数据源格式不同）
    - start/end：日期字符串（YYYY-MM-DD）
    - source：synthetic/yahoo/ccxt/local
    - days：回测天数（ccxt 作为 limit 的近似）
    - data_dir：source=local 时的本地缓存目录（如 data/binance/1d）
    """
    fetcher = DataFetcher(data_timezone=data_timezone)

    if source == "local":
        if not data_dir:
            raise ValueError("--source local requires --data-dir")
        return _load_local_ohlcv(symbol, start, end, data_dir)
    elif source == "ccxt":
        return fetcher.fetch_ccxt(
            symbol,
            timeframe=timeframe,
            limit=days,
            start_date=start,
            end_date=end,
            exchange_id=exchange,
        )
    elif source == "yahoo":
        return fetcher.fetch_yahoo(symbol, start, end)
    else:
        # Synthetic / Scenario
        return fetcher.generate_scenario(symbol, start, end)


def _safe_symbol_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol).strip("._") or "symbol"


def _load_secondary_data(directory: str, symbols) -> dict:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Secondary data directory does not exist: {root}")
    result = {}
    for symbol in symbols:
        candidates = [
            root / f"{_safe_symbol_name(symbol)}.csv",
            root / f"{symbol}.csv",
        ]
        path = next((item for item in candidates if item.exists()), None)
        if path is None:
            continue
        frame = DataHandler.load_csv(str(path))
        result[symbol] = DataHandler.annotate_quality(frame)
    return result


def replay_manifest(manifest_path: str) -> int:
    """Re-run a saved manifest and compare trades/equity/report payload exactly."""

    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    expected_code = manifest["code"]
    config_path = Path(__file__).resolve().parent / "config" / "params.yaml"
    if manifest["config"]["sha256"] != sha256_file(config_path):
        print("Replay refused: current config hash differs from manifest.", file=sys.stderr)
        return 7
    snapshots = load_data_snapshots(
        path.parent / "data_inputs", manifest["data_snapshots"], verify=True
    )
    execution = manifest["execution"]
    seed = int(execution["seed"])
    np.random.seed(seed)
    random.seed(seed)
    engine = BacktestEngine(
        initial_capital=float(execution["capital"]),
        slippage=execution.get("slippage"),
        random_slip=bool(execution.get("random_slip", False)),
        warmup_period=int(execution.get("warmup_period", 30)),
        alignment_mode=execution["alignment_mode"],
        benchmark_mode=execution["benchmark_mode"],
        benchmark_rebalance_cost_bps=float(execution["benchmark_rebalance_cost_bps"]),
        timeframe=execution["timeframe"],
        run_id=manifest["run_id"],
        account_mode=execution.get("account_mode"),
    )
    result = engine.run(snapshots, routing_log_enabled=False)
    observed = deterministic_result_digest(result)
    expected = execution["result_digest"]
    report = {
        "status": "passed" if observed == expected else "failed",
        "expected": expected,
        "observed": observed,
        "code_identity_expected": expected_code,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 8


def main(argv=None) -> int:
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
        "--capital", type=float, default=DEFAULT_INITIAL_CAPITAL, help="Initial capital (USDT)"
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
        "--exchange", default="binance",
        help="Exchange identity for market data (default: binance).",
    )
    parser.add_argument(
        "--market-type", default=None, choices=["spot", "margin", "perpetual"],
        help="Override account mode; omitted uses config account.mode.",
    )
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

    cli_args = list(sys.argv[1:] if argv is None else argv)
    if not cli_args:
        parser.print_help(sys.stderr)
        print(
            "\nError: no arguments supplied; use explicit CLI options "
            "(for example: --source synthetic --days 365).",
            file=sys.stderr,
        )
        return 2
    args = parser.parse_args(cli_args)

    if args.replay_manifest:
        return replay_manifest(args.replay_manifest)

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
                print("Error: Start date must be before end date.", file=sys.stderr)
                return 2
            args.days = (end_date - start_date).days
            print(f"Config: Date Range={args.start} to {args.end} ({args.days} days)")
        except ValueError:
            print("Error: Invalid date format. Please use YYYY-MM-DD.", file=sys.stderr)
            return 2
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
    download_started_at = datetime.now(timezone.utc)

    for sym in symbols:
        df = get_data(
            sym,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            args.source,
            args.days,
            exchange=args.exchange,
            timeframe=args.timeframe,
            data_timezone=args.data_timezone,
            data_dir=args.data_dir,
        )

        if not df.empty and len(df) > 10:  # Lower limit for short tests
            print(f"Loaded {sym}: {len(df)} bars")
            data_map[sym] = DataHandler.annotate_quality(df)
        else:
            print(f"Failed to load sufficient data for {sym}")

    if not data_map:
        print("No data available. Exiting.", file=sys.stderr)
        return 3

    if args.universe_file:
        universe = PointInTimeUniverse.from_csv(args.universe_file)
        data_map = universe.apply(data_map)
        universe_identity = universe.to_manifest()
        if not data_map:
            print("Point-in-time universe removed all requested data.", file=sys.stderr)
            return 3
    else:
        universe = None
        universe_identity = static_universe_manifest(data_map)

    download_completed_at = datetime.now(timezone.utc)

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
        alignment_mode=args.alignment_mode,
        benchmark_mode=args.benchmark_mode,
        benchmark_rebalance_cost_bps=args.benchmark_rebalance_cost_bps,
        timeframe=args.timeframe,
        account_mode=(
            "spot_margin" if args.market_type == "margin" else args.market_type
        ),
    )

    print("Running Backtest...")
    # Use a temporary path for routing log, to be moved later
    if not os.path.exists(os.path.join(os.getcwd(), "reports")):
        os.makedirs(os.path.join(os.getcwd(), "reports"))
    temp_routing_log = os.path.join(os.getcwd(), "reports", "temp_routing_log.csv")
    results = engine.run(
        data_map,
        routing_log_path=temp_routing_log,
        routing_log_enabled=(
            not args.disable_routing_log and args.report_profile == "full"
        ),
    )

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
        return 4

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

    output_path = Path(output_dir)
    reporter = ReportGenerator(output_dir)

    effective_timestamps = engine.market_data_adapter.timestamps
    effective_period = {
        "start": effective_timestamps.min(),
        "end": effective_timestamps.max(),
        "bars": len(effective_timestamps),
        "alignment_mode": args.alignment_mode,
        "per_symbol": {
            symbol: {
                "start": frame.index.min(),
                "end": frame.index.max(),
                "rows": len(frame),
            }
            for symbol, frame in sorted(data_map.items())
        },
    }

    # Prepare metadata
    metadata = {
        "Days": args.days,
        "Start": start_date.strftime("%Y-%m-%d"),
        "End": end_date.strftime("%Y-%m-%d"),
        "Capital": args.capital,
        "Symbols": ", ".join(args.symbols),
        "Source": args.source,
        "RequestedPeriod": (
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        ),
        "EffectivePeriod": (
            f"{effective_timestamps.min()} to {effective_timestamps.max()}"
        ),
        "AlignmentMode": args.alignment_mode,
        "BenchmarkMode": args.benchmark_mode,
        "Timeframe": args.timeframe,
        "MarketType": args.market_type or results.get("account_mode"),
        "AccountMode": results.get("account_mode"),
    }

    metrics = reporter.generate(
        results["trades"],
        results["equity_curve"],
        metadata=metadata,
        benchmark_curve=results.get("benchmark"),
        close_events=results.get("close_events"),
        lifecycle=results.get("lifecycle"),
        strategy_health=results.get("strategy_health"),
        protective_stops=results.get("protective_stop_summary"),
        report_profile=args.report_profile,
        data_quality=quality_report,
    )

    artifact_failures = []

    # Daily research runs should be easy to inspect and cheap to retain.  The
    # full profile below remains the promotion/validation path with immutable
    # ledgers, event coverage and replay snapshots.
    if args.report_profile in {"workbook", "compact"}:
        if os.path.exists(temp_routing_log):
            try:
                os.remove(temp_routing_log)
            except OSError:
                logger.warning("Failed to remove temporary routing log %s", temp_routing_log)
        print("\n" + "=" * 30)
        print("BACKTEST RESULTS (core metrics)")
        print("=" * 30)
        print(format_primary_metrics(metrics, bilingual=False))
        print("=" * 30)
        report_name = (
            "backtest_report.xlsx" if args.report_profile == "workbook"
            else "report.pdf"
        )
        print(f"\n{args.report_profile.title()} report saved to: {output_path / report_name}")
        print("Use --report-profile full for ledgers, event logs and replay artifacts.")
        return 0

    # Phase 3 account/risk/cost ledgers are first-class mandatory audit
    # artifacts, not values buried only in the in-memory engine result.
    try:
        pd.DataFrame(results.get("margin_ledger") or []).to_csv(
            output_path / "margin_ledger.csv", index=False
        )
        pd.DataFrame(results.get("financing_ledger") or []).to_csv(
            output_path / "financing_ledger.csv", index=False
        )
        pd.DataFrame(results.get("execution_audit") or []).to_csv(
            output_path / "execution_audit.csv", index=False
        )
        pd.DataFrame(results.get("breaker_audit") or []).to_csv(
            output_path / "breaker_audit.csv", index=False
        )
        # SR1-4 deliverables: the health lifecycle and its authoritative
        # observation unit are audit artifacts, not log text.
        pd.DataFrame(results.get("strategy_health_transitions") or []).to_csv(
            output_path / "strategy_health_timeline.csv", index=False
        )
        pd.DataFrame(results.get("strategy_health_cohorts") or []).to_csv(
            output_path / "cohort_trades.csv", index=False
        )
        pd.DataFrame(results.get("risk_budget_reconciliation") or []).to_csv(
            output_path / "risk_budget_reconciliation.csv", index=False
        )
        # STR-P1-01 deliverable: the resident protective stop's intents and
        # fills, so a backtest stop can be audited exactly like a live one.
        pd.DataFrame(results.get("stop_order_audit") or []).to_csv(
            output_path / "stop_order_audit.csv", index=False
        )
        # SR3 deliverables: the ranking that actually decided allocation, and
        # the correlated-risk budget that metered it.
        pd.DataFrame(results.get("allocation_audit") or []).to_csv(
            output_path / "allocation_audit.csv", index=False
        )
        pd.DataFrame(results.get("correlated_risk_audit") or []).to_csv(
            output_path / "correlated_risk_audit.csv", index=False
        )
        write_json_report(output_path / "account_cost_contract.json", {
            **(results.get("account_cost_contract") or {}),
            "degenerate_ranking_batches": results.get(
                "degenerate_ranking_batches", 0
            ),
        })
        write_json_report(
            output_path / "strategy_health.json", results.get("strategy_health") or {}
        )
        pd.DataFrame([
            {
                "strategy": name,
                "status": entry.get("status"),
                "raw_setup_count": entry.get("raw_setup_count"),
                "suppressed_raw_setups": entry.get("suppressed_raw_setups"),
                "last_raw_setup_at": entry.get("last_raw_setup_at"),
                "last_suppressed_setup_at": entry.get("last_suppressed_setup_at"),
            }
            for name, entry in (results.get("strategy_health") or {}).items()
        ]).to_csv(output_path / "suppressed_setups.csv", index=False)
        write_json_report(output_path / "breaker_state.json", {
            "account_mode": results.get("account_mode"),
            **(results.get("breaker_state") or {}),
        })
        write_json_report(
            output_path / "backtest_lifecycle.json",
            results.get("lifecycle") or {},
        )
    except Exception as exc:
        artifact_failures.append("phase3_account_risk_audit")
        logger.exception("Failed to save Phase 3 account/risk artifacts: %s", exc)

    # T-2.7/T-2.8: preserve both benchmark definitions plus the selected
    # benchmark's weight, turnover and cost ledger for independent audit.
    try:
        if results.get("benchmark_fixed") is not None:
            results["benchmark_fixed"].to_csv(output_path / "benchmark_fixed.csv")
        if results.get("benchmark_dynamic") is not None:
            results["benchmark_dynamic"].to_csv(output_path / "benchmark_dynamic.csv")
        weights = results.get("benchmark_weights")
        if isinstance(weights, pd.DataFrame) and not weights.empty:
            weights.to_csv(output_path / "benchmark_weights.csv")
        turnover = results.get("benchmark_turnover")
        costs = results.get("benchmark_costs")
        if isinstance(turnover, pd.Series) and isinstance(costs, pd.Series):
            pd.concat([turnover.rename("turnover"), costs.rename("cost")], axis=1).to_csv(
                output_path / "benchmark_turnover_cost.csv"
            )
        write_json_report(
            output_path / "benchmark_metadata.json",
            results.get("benchmark_metadata") or {},
        )
    except Exception as exc:
        artifact_failures.append("benchmark_audit")
        logger.exception("Failed to save benchmark audit artifacts: %s", exc)

    # Save Data Quality Report
    dq_report_path = os.path.join(output_dir, "data_quality_report.json")
    try:
        with open(dq_report_path, "w", encoding="utf-8") as f:
            json.dump(quality_report, f, indent=4, default=str)
        print(f"Data quality report saved to {dq_report_path}")
    except Exception as e:
        artifact_failures.append("data_quality_report")
        logger.exception("Failed to save data quality report: %s", e)

    # Move Routing Log
    final_routing_log = os.path.join(output_dir, "routing_log.csv")
    if os.path.exists(temp_routing_log):
        try:
            shutil.move(temp_routing_log, final_routing_log)
            print(f"Routing log moved to {final_routing_log}")
        except Exception as e:
            artifact_failures.append("routing_log")
            logger.exception("Failed to move routing log: %s", e)

    # T-2.9: event pipeline audit is mandatory for a normal backtest.  If
    # trading occurred, every signal/risk/order/fill/close stage must exist.
    try:
        event_summary = write_event_log(
            results.get("event_log") or (), output_path / "event_log.jsonl"
        )
        audit_coverage = validate_audit_coverage(
            event_summary=event_summary,
            routing_log_path=final_routing_log,
            routing_required=not args.disable_routing_log,
            trade_count=len(results.get("trades") or []),
            close_count=sum((results.get("close_events") or {}).values()),
        )
        if audit_coverage["status"] != "ok":
            artifact_failures.append("event_audit_coverage")
    except Exception as exc:
        event_summary = {"status": "failed", "error": str(exc)}
        audit_coverage = {"status": "failed", "missing_event_types": ["event_log"]}
        artifact_failures.append("event_log")
        logger.exception("Failed to save mandatory event audit: %s", exc)

    # T-2.11: never pretend the primary feed verified itself.  Without an
    # independent directory this report stays explicitly UNVERIFIED.
    try:
        closed_trades = reporter._reconstruct_closed_trades(
            pd.DataFrame(results.get("trades") or [])
        )
        secondary_data = (
            _load_secondary_data(args.secondary_data_dir, data_map)
            if args.secondary_data_dir else None
        )
        market_data_audit = cross_verify_top_trades(
            closed_trades, data_map, secondary_data, top_n=20
        )
        write_json_report(output_path / "top_trade_market_data_audit.json", market_data_audit)
        if args.require_secondary_audit and market_data_audit["status"] != "passed":
            artifact_failures.append("secondary_data_audit")
    except Exception as exc:
        market_data_audit = {"status": "failed", "error": str(exc)}
        artifact_failures.append("secondary_data_audit")
        logger.exception("Failed top-trade market data audit: %s", exc)

    # T-2.1--T-2.5/T-2.13: snapshot exact engine inputs and write the complete
    # identity only after every auditable output exists.
    try:
        snapshot_entries = save_data_snapshots(data_map, output_path / "data_inputs")
        requested_period = {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "days": args.days,
        }
        identity = data_identity(
            data_map,
            source=args.source,
            exchange=args.exchange if args.source == "ccxt" else None,
            market_type=args.market_type or results.get("account_mode"),
            timeframe=args.timeframe,
            timezone_name=args.data_timezone,
            downloaded_at=download_completed_at,
        )
        identity["download_started_at"] = download_started_at
        identity["universe"] = universe_identity
        artifact_names = [
            "equity.csv", "trades.csv", "report.txt", "benchmark.csv",
            "benchmark_fixed.csv", "benchmark_dynamic.csv", "benchmark_weights.csv",
            "benchmark_turnover_cost.csv", "benchmark_metadata.json",
            "routing_log.csv", "event_log.jsonl", "data_quality_report.json",
            "top_trade_market_data_audit.json",
            "margin_ledger.csv", "financing_ledger.csv", "execution_audit.csv",
            "breaker_audit.csv", "breaker_state.json", "backtest_lifecycle.json",
        ]
        execution_identity = {
            **runtime_identity(),
            "capital": args.capital,
            "seed": args.seed,
            "slippage": engine.slippage,
            "random_slip": args.random_slip,
            "warmup_period": engine.warmup_period,
            "alignment_mode": args.alignment_mode,
            "benchmark_mode": args.benchmark_mode,
            "benchmark_rebalance_cost_bps": args.benchmark_rebalance_cost_bps,
            "timeframe": args.timeframe,
            "account_mode": results.get("account_mode"),
            "routing_log_enabled": not args.disable_routing_log,
            "result_digest": deterministic_result_digest(results),
        }
        manifest = build_run_manifest(
            run_id=results["run_id"],
            repo_root=Path(__file__).resolve().parent,
            config_path=Path(__file__).resolve().parent / "config" / "params.yaml",
            requested_period=requested_period,
            effective_period=effective_period,
            data=identity,
            snapshots=snapshot_entries,
            execution=execution_identity,
            artifacts=artifact_hashes(output_path, artifact_names),
            audit={
                "event_log": event_summary,
                "coverage": audit_coverage,
                "top_trade_market_data": market_data_audit,
            },
        )
        write_manifest(output_path / "run_manifest.json", manifest)
    except Exception as exc:
        artifact_failures.append("run_manifest")
        logger.exception("Failed to create reproducible run manifest: %s", exc)

    print("\n" + "=" * 30)
    print("BACKTEST RESULTS (core metrics)")
    print("=" * 30)
    # English-only on stdout: Windows consoles commonly default to the GBK
    # codepage, which garbles Chinese text; report.txt is written with an
    # explicit utf-8 encoding and carries the bilingual labels instead.
    print(format_primary_metrics(metrics, bilingual=False))
    print("=" * 30)
    print("Full metrics (drawdown events, trade quality, attribution, benchmark) written to report.txt")

    print(f"\nReport saved to: {output_dir}")
    if artifact_failures:
        print(
            "Report completed with artifact failures: "
            + ", ".join(artifact_failures),
            file=sys.stderr,
        )
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
