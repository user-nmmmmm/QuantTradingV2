"""The ``quant-backtest`` command: argument contract, one backtest run and its report artifacts."""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import argparse
import json
import numpy as np
import os
import pandas as pd
import random
import shutil
import sys

from backtest.cli import build_backtest_parser
from backtest.engine import BacktestEngine, DEFAULT_INITIAL_CAPITAL
from backtest.reporting import ReportGenerator, format_primary_metrics
from cli.data import _load_requested_data, _load_secondary_data, _resolve_date_range
from cli.replay import _temporal_report_payload, replay_manifest
from core.analytics.backtest_audit import cross_verify_top_trades, validate_audit_coverage, write_event_log, write_json_report
from core.foundation.logger import get_logger
from core.foundation.reproducibility import artifact_hashes, build_run_manifest, capital_allocation_digest, data_identity, deterministic_result_digest, runtime_identity, save_data_snapshots, sha256_file, write_manifest
from core.market.data import DataHandler
from core.market.temporal_data import temporal_policy as parse_temporal_policy

ROOT = Path(__file__).resolve().parents[1]   # repository root (this file is cli/<module>.py)

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Preserve the entrypoint API used by callers and tests."""
    return build_backtest_parser(DEFAULT_INITIAL_CAPITAL)


def _capital_allocation_options(args):
    """Validate per-run CLI overrides without changing the shared configuration."""
    from config.config import config
    from core.trading.position_management import CapitalAllocationPolicy
    enabled = getattr(args, "smart_allocation", False)
    values = {key: getattr(args, key, None) for key in ("max_positions", "cash_reserve_pct")}
    if not enabled and any(value is not None for value in values.values()):
        raise ValueError("--max-positions and --cash-reserve-pct require --smart-allocation")
    if not enabled:
        return None
    policy = {**((config.get("allocation") or {}).get("capital") or {}), "enabled": True,
              **{key: value for key, value in values.items() if value is not None}}
    return asdict(CapitalAllocationPolicy.from_mapping(policy))


def _validate_selector_execution_options(args):
    """Reject unsupported execution before loading data or constructing a model."""
    from config.config import config
    from core.foundation.runtime import validate_selector_execution_path

    enabled = getattr(args, "coin_selector", "off") == "on"
    validate_selector_execution_path(
        selector_enabled=enabled,
        portfolio_targets_enabled=(config.get("portfolio_targets") or {}).get("enabled", False),
    )
    market_type = getattr(args, "market_type", None)
    if market_type is not None:
        return "spot_margin" if market_type == "margin" else market_type
    return (config.get("account") or {}).get("mode", "spot")


def _execute_backtest(args, data_map):
    """Construct the engine and run it with the requested reporting footprint."""
    print("\nInitializing Backtest Engine...")
    account_mode = _validate_selector_execution_options(args)
    candidate_selector = None
    selector_identity = {"schema": "backtest-coin-selector/v1", "enabled": False,
                         "candidate": None, "new_training_updates": 0, "new_threshold_search": 0}
    if getattr(args, "coin_selector", "off") == "on":
        from research.ml_selection.serving import create_selector
        candidate_selector, selector_identity = create_selector(
            data_map, initial_capital=args.capital,
            bundle_path=getattr(args, "selector_bundle", None), account_mode=account_mode)
    engine = BacktestEngine(
        initial_capital=args.capital,
        slippage=args.slippage,
        random_slip=args.random_slip,
        alignment_mode=args.alignment_mode,
        benchmark_mode=args.benchmark_mode,
        benchmark_rebalance_cost_bps=args.benchmark_rebalance_cost_bps,
        timeframe=args.timeframe,
        account_mode=account_mode,
        signal_observation={"enabled": True} if getattr(args, "observe_signals", False) else None,
        signal_meta_layer={"enabled": True} if getattr(args, "signal_meta_layer", False) else None,
        signal_adaptive={"enabled": True} if getattr(args, "adaptive_signal_meta", False) else None,
        signal_meta_replay={"enabled": True} if getattr(args, "signal_meta_replay", False) else None,
        temporal_policy=getattr(args, "resolved_temporal_policy", None),
        temporal_financing=getattr(args, "temporal_financing_evidence", None),
        capital_allocation=_capital_allocation_options(args),
        candidate_selector=candidate_selector,
    )
    engine.coin_selector_identity = selector_identity
    print("Running Backtest...")
    reports_dir = os.path.join(os.getcwd(), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    temp_routing_log = os.path.join(getattr(args, "output_dir", None) or reports_dir,
                                   "temp_routing_log.csv")
    results = engine.run(
        data_map,
        routing_log_path=temp_routing_log,
        routing_log_enabled=(
            not args.disable_routing_log and args.report_profile == "full"
        ),
    )
    return engine, results, temp_routing_log


class _MainState:
    """Locals of one ``main`` run that more than one stage needs (B5, ADR 0019).

    Every field is assigned by the stage that creates it; ``__slots__`` makes a typo an error.
    """

    __slots__ = (
        "argv",
        "args",
        "start_date",
        "end_date",
        "data_map",
        "universe_identity",
        "download_started_at",
        "download_completed_at",
        "quality_report",
        "engine",
        "results",
        "temp_routing_log",
        "output_dir",
        "output_path",
        "reporter",
        "artifact_failures",
        "signal_summary",
        "meta_summary",
        "adaptive_summary",
        "meta_replay_summary",
        "effective_period",
        "selector_summary",
        "metrics",
        "event_summary",
        "audit_coverage",
        "market_data_audit",
    )

    argv: Optional[Sequence[str]]
    args: argparse.Namespace
    start_date: datetime
    end_date: datetime
    data_map: Dict[str, pd.DataFrame]
    universe_identity: Dict[str, Any]
    download_started_at: datetime
    download_completed_at: datetime
    quality_report: Dict[str, Any]
    engine: BacktestEngine
    results: Dict[str, Any]
    temp_routing_log: str
    output_dir: str
    output_path: Path
    reporter: ReportGenerator
    artifact_failures: List[str]
    signal_summary: Dict[str, Any]
    meta_summary: Dict[str, Any]
    adaptive_summary: Dict[str, Any]
    meta_replay_summary: Dict[str, Any]
    effective_period: Dict[str, Any]
    selector_summary: Dict[str, Any]
    metrics: Dict[str, Any]
    event_summary: Dict[str, Any]
    audit_coverage: Dict[str, Any]
    market_data_audit: Dict[str, Any]


def _parse_arguments(rs: _MainState) -> Optional[int]:
    """Parse the command line; handle the no-argument error and manifest replay."""
    parser = _build_parser()

    cli_args = list(sys.argv[1:] if rs.argv is None else rs.argv)
    if not cli_args:
        parser.print_help(sys.stderr)
        print(
            "\nError: no arguments supplied; use explicit CLI options "
            "(for example: --source synthetic --days 365).",
            file=sys.stderr,
        )
        return 2
    rs.args = parser.parse_args(cli_args)

    if rs.args.replay_manifest:
        return replay_manifest(rs.args.replay_manifest)


def _validate_options(rs: _MainState) -> Optional[int]:
    """Reject inconsistent capital-allocation and coin-selector options before any work."""

    try:
        _capital_allocation_options(rs.args)
        _validate_selector_execution_options(rs.args)
        if rs.args.selector_bundle and rs.args.coin_selector != "on":
            raise ValueError("--selector-bundle requires --coin-selector on")
        if rs.args.coin_selector == "on":
            if rs.args.timeframe != "1d":
                raise ValueError("Coin selection currently requires --timeframe 1d")
            from backtest.coin_selector import read_bundle
            read_bundle(rs.args.selector_bundle)
    except (TypeError, ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def _prepare_run(rs: _MainState) -> Optional[int]:
    """Seed the RNGs, resolve the date range and output directory, then load the market data."""

    if rs.args.seed is not None:
        np.random.seed(rs.args.seed)
        random.seed(rs.args.seed)
        print(f"Random seed set to {rs.args.seed}")

    print("Starting Quantitative Trading System...")
    print(f"Current Working Directory: {os.getcwd()}")

    try:
        rs.start_date, rs.end_date = _resolve_date_range(rs.args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if rs.args.output_dir:
        try:
            Path(rs.args.output_dir).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            print(f"Output directory must be new: {exc}", file=sys.stderr)
            return 2

    print(
        f"Config: Capital={rs.args.capital}, Symbols={rs.args.symbols}, Source={rs.args.source}, Slippage={rs.args.slippage if rs.args.slippage is not None else 'config'}, RandomSlip={rs.args.random_slip}"
    )

    try:
        (
            rs.data_map,
            rs.universe_identity,
            rs.download_started_at,
            rs.download_completed_at,
        ) = _load_requested_data(rs.args, rs.start_date, rs.end_date)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3


def _run_backtest(rs: _MainState) -> Optional[int]:
    """Data-quality report, then run the engine; stop when it produced nothing."""

    # 1.1 Generate Data Quality Report
    print("Generating Data Quality Report...")
    # Store report in memory to save later in the specific backtest folder
    rs.quality_report = DataHandler.generate_quality_report(rs.data_map, output_path=None)
    for symbol, frame in rs.data_map.items():
        if frame.attrs.get("derivatives"):
            rs.quality_report.setdefault(symbol, {})["derivatives"] = frame.attrs["derivatives"]

    try:
        rs.engine, rs.results, rs.temp_routing_log = _execute_backtest(rs.args, rs.data_map)
    except (TypeError, ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Backtest setup failed: {exc}", file=sys.stderr)
        return 2

    if not rs.results or rs.results["equity_curve"].empty:
        print("\n" + "!" * 50)
        print("ERROR: Backtest failed or produced no results.")
        print("Possible causes:")
        print("1. No common timeframe found between symbols (check start/end dates).")
        print("2. Data fetching failed for some symbols.")
        print("3. Strategy produced no trades and no equity updates.")
        print("!" * 50 + "\n")
        # Cleanup temp log
        if os.path.exists(rs.temp_routing_log):
            try:
                os.remove(rs.temp_routing_log)
            except OSError as e:
                logger.warning("Failed to remove temp routing log %s: %s", rs.temp_routing_log, e)
        return 4


def _build_report(rs: _MainState) -> None:
    """Name the run folder and write the allocation, temporal and signal summaries."""

    # 3. Generate Report
    print("\nGenerating Report...")

    # Calculate basic return for naming
    equity = rs.results["equity_curve"]["equity"]
    total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
    return_str = f"Ret{total_return * 100:.1f}pct"

    # Naming convention: YYYYMMDD_HHMMSS_{Days}d_{Syms}Syms_{Ret}pct
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    symbols_str = f"{len(rs.args.symbols)}Syms"
    days_str = f"{rs.args.days}d"

    folder_name = f"{timestamp}_{days_str}_{symbols_str}_{return_str}"
    rs.output_dir = rs.args.output_dir or os.path.join(os.getcwd(), "reports", folder_name)

    rs.output_path = Path(rs.output_dir)
    rs.reporter = ReportGenerator(rs.output_dir)
    rs.artifact_failures = []
    try:
        write_json_report(rs.output_path / "capital_allocation.json", {
            "policy": rs.results.get("capital_allocation_policy"),
            "batches": rs.results.get("capital_allocation_audit", []),
        })
        if (rs.results.get("capital_allocation_policy") or {}).get("enabled"):
            pd.DataFrame(rs.results.get("allocation_audit") or []).to_csv(
                rs.output_path / "capital_allocation.csv", index=False)
    except Exception as exc:
        rs.artifact_failures.append("capital_allocation")
        logger.exception("Failed to save capital allocation evidence: %s", exc)
    try:
        write_json_report(rs.output_path / "temporal_data.json", _temporal_report_payload(rs.engine, rs.results, rs.data_map))
    except Exception as exc:
        rs.artifact_failures.append("temporal_data")
        logger.exception("Failed to save temporal data evidence: %s", exc)
    rs.signal_summary = {}
    if rs.results.get("signal_observation") is not None:
        from backtest.reporting.signal_observation import write_signal_observation_report
        rs.signal_summary = write_signal_observation_report(rs.results["signal_observation"], rs.output_path)
        if rs.signal_summary["status"] != "complete":
            rs.artifact_failures.append("signal_observation_incomplete")
    rs.meta_summary = {}
    if rs.results.get("signal_meta_layer") is not None:
        from backtest.reporting.signal_meta_layer import write_signal_meta_layer_report
        rs.meta_summary = write_signal_meta_layer_report(rs.results["signal_meta_layer"], rs.output_path)
        if rs.meta_summary["status"] != "complete":
            rs.artifact_failures.append("signal_meta_layer_incomplete")
    rs.adaptive_summary, rs.meta_replay_summary = {}, {}
    if rs.results.get("signal_adaptive") is not None:
        from backtest.reporting.signal_adaptive import write_signal_adaptive_report
        rs.adaptive_summary = write_signal_adaptive_report(
            rs.results["signal_adaptive"], rs.output_path, p1_payload=rs.results.get("signal_meta_layer"))
        if rs.adaptive_summary["status"] != "complete":
            rs.artifact_failures.append("signal_adaptive_incomplete")
    if rs.results.get("signal_meta_replay") is not None:
        from backtest.reporting.signal_adaptive import write_signal_meta_replay_report
        rs.meta_replay_summary = write_signal_meta_replay_report(rs.results["signal_meta_replay"], rs.output_path)
        if rs.meta_replay_summary["status"] != "complete":
            rs.artifact_failures.append("signal_meta_replay_incomplete")


def _describe_run(rs: _MainState) -> Optional[int]:
    """Effective period, selector summary, run metadata and the main report."""

    adapter = rs.engine.market_data_adapter
    if adapter is None:
        raise RuntimeError("the engine finished without a market-data adapter")
    effective_timestamps = adapter.timestamps
    rs.effective_period = {
        "start": effective_timestamps.min(),
        "end": effective_timestamps.max(),
        "bars": len(effective_timestamps),
        "alignment_mode": rs.args.alignment_mode,
        "per_symbol": {
            symbol: {
                "start": frame.index.min(),
                "end": frame.index.max(),
                "rows": len(frame),
            }
            for symbol, frame in sorted(rs.data_map.items())
        },
    }

    # Engines supplied by older callers may predate the CLI selector identity.
    selector_identity = getattr(rs.engine, "coin_selector_identity", {
        "schema": "backtest-coin-selector/v1", "enabled": False,
        "candidate": None, "new_training_updates": 0, "new_threshold_search": 0})
    # Prepare metadata
    metadata = {
        "Days": rs.args.days,
        "Start": rs.start_date.strftime("%Y-%m-%d"),
        "End": rs.end_date.strftime("%Y-%m-%d"),
        "Capital": rs.args.capital,
        "Symbols": ", ".join(rs.args.symbols),
        "Source": rs.args.source,
        "RequestedPeriod": (
            f"{rs.start_date.strftime('%Y-%m-%d')} to {rs.end_date.strftime('%Y-%m-%d')}"
        ),
        "EffectivePeriod": (
            f"{effective_timestamps.min()} to {effective_timestamps.max()}"
        ),
        "AlignmentMode": rs.args.alignment_mode,
        "BenchmarkMode": rs.args.benchmark_mode,
        "Timeframe": rs.args.timeframe,
        "MarketType": rs.args.market_type or rs.results.get("account_mode"),
        "AccountMode": rs.results.get("account_mode"),
        "CoinSelector": "on" if selector_identity["enabled"] else "off",
        "CoinSelectorModelId": selector_identity.get("model_id"),
    }

    from backtest.coin_selector import write_selector_report
    rs.selector_summary = write_selector_report(
        rs.output_path, rs.engine.candidate_selector, selector_identity)

    rs.metrics = rs.reporter.generate(
        rs.results["trades"],
        rs.results["equity_curve"],
        metadata=metadata,
        benchmark_curve=rs.results.get("benchmark"),
        close_events=rs.results.get("close_events"),
        lifecycle=rs.results.get("lifecycle"),
        strategy_health=rs.results.get("strategy_health"),
        protective_stops=rs.results.get("protective_stop_summary"),
        report_profile=rs.args.report_profile,
        data_quality=rs.quality_report,
        # BM3: the signal->fill funnel is only derivable from the run's own
        # event stream, which nothing outside the engine has.
        event_log=rs.results.get("event_log"),
        max_holding_days=rs.results.get("effective_max_holding_days"),
    )


    # Daily research runs should be easy to inspect and cheap to retain.  The
    # full profile below remains the promotion/validation path with immutable
    # ledgers, event coverage and replay snapshots.
    if rs.args.report_profile in {"workbook", "compact"}:
        if os.path.exists(rs.temp_routing_log):
            try:
                os.remove(rs.temp_routing_log)
            except OSError:
                logger.warning("Failed to remove temporary routing log %s", rs.temp_routing_log)
        print("\n" + "=" * 30)
        print("BACKTEST RESULTS (core metrics)")
        print("=" * 30)
        print(format_primary_metrics(rs.metrics, bilingual=False))
        print("=" * 30)
        report_name = (
            "backtest_report.xlsx" if rs.args.report_profile == "workbook"
            else "report.pdf"
        )
        print(f"\n{rs.args.report_profile.title()} report saved to: {rs.output_path / report_name}")
        print("Use --report-profile full for ledgers, event logs and replay artifacts.")
        if rs.artifact_failures:
            print("Research artifacts incomplete: " + ", ".join(rs.artifact_failures), file=sys.stderr)
            return 5
        return 0


def _write_artifacts(rs: _MainState) -> None:
    """Workbook/compact profile outputs and the optional artifacts that may fail."""

    # Phase 3 account/risk/cost ledgers are first-class mandatory audit
    # artifacts, not values buried only in the in-memory engine result.
    try:
        pd.DataFrame(rs.results.get("margin_ledger") or []).to_csv(
            rs.output_path / "margin_ledger.csv", index=False
        )
        pd.DataFrame(rs.results.get("financing_ledger") or []).to_csv(
            rs.output_path / "financing_ledger.csv", index=False
        )
        pd.DataFrame(rs.results.get("execution_audit") or []).to_csv(
            rs.output_path / "execution_audit.csv", index=False
        )
        pd.DataFrame(rs.results.get("breaker_audit") or []).to_csv(
            rs.output_path / "breaker_audit.csv", index=False
        )
        # SR1-4 deliverables: the health lifecycle and its authoritative
        # observation unit are audit artifacts, not log text.
        pd.DataFrame(rs.results.get("strategy_health_transitions") or []).to_csv(
            rs.output_path / "strategy_health_timeline.csv", index=False
        )
        pd.DataFrame(rs.results.get("strategy_health_cohorts") or []).to_csv(
            rs.output_path / "cohort_trades.csv", index=False
        )
        pd.DataFrame(rs.results.get("risk_budget_reconciliation") or []).to_csv(
            rs.output_path / "risk_budget_reconciliation.csv", index=False
        )
        # STR-P1-01 deliverable: the resident protective stop's intents and
        # fills, so a backtest stop can be audited exactly like a live one.
        pd.DataFrame(rs.results.get("stop_order_audit") or []).to_csv(
            rs.output_path / "stop_order_audit.csv", index=False
        )
        # SR3 deliverables: the ranking that actually decided allocation, and
        # the correlated-risk budget that metered it.
        pd.DataFrame(rs.results.get("allocation_audit") or []).to_csv(
            rs.output_path / "allocation_audit.csv", index=False
        )
        pd.DataFrame(rs.results.get("correlated_risk_audit") or []).to_csv(
            rs.output_path / "correlated_risk_audit.csv", index=False
        )
        write_json_report(rs.output_path / "account_cost_contract.json", {
            **(rs.results.get("account_cost_contract") or {}),
            "degenerate_ranking_batches": rs.results.get(
                "degenerate_ranking_batches", 0
            ),
        })
        write_json_report(
            rs.output_path / "strategy_health.json", rs.results.get("strategy_health") or {}
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
            for name, entry in (rs.results.get("strategy_health") or {}).items()
        ]).to_csv(rs.output_path / "suppressed_setups.csv", index=False)
        write_json_report(rs.output_path / "breaker_state.json", {
            "account_mode": rs.results.get("account_mode"),
            **(rs.results.get("breaker_state") or {}),
        })
        write_json_report(
            rs.output_path / "backtest_lifecycle.json",
            rs.results.get("lifecycle") or {},
        )
    except Exception as exc:
        rs.artifact_failures.append("phase3_account_risk_audit")
        logger.exception("Failed to save Phase 3 account/risk artifacts: %s", exc)

    # T-2.7/T-2.8: preserve both benchmark definitions plus the selected
    # benchmark's weight, turnover and cost ledger for independent audit.
    try:
        if rs.results.get("benchmark_fixed") is not None:
            rs.results["benchmark_fixed"].to_csv(rs.output_path / "benchmark_fixed.csv")
        if rs.results.get("benchmark_dynamic") is not None:
            rs.results["benchmark_dynamic"].to_csv(rs.output_path / "benchmark_dynamic.csv")
        weights = rs.results.get("benchmark_weights")
        if isinstance(weights, pd.DataFrame) and not weights.empty:
            weights.to_csv(rs.output_path / "benchmark_weights.csv")
        turnover = rs.results.get("benchmark_turnover")
        costs = rs.results.get("benchmark_costs")
        if isinstance(turnover, pd.Series) and isinstance(costs, pd.Series):
            pd.concat([turnover.rename("turnover"), costs.rename("cost")], axis=1).to_csv(
                rs.output_path / "benchmark_turnover_cost.csv"
            )
        write_json_report(
            rs.output_path / "benchmark_metadata.json",
            rs.results.get("benchmark_metadata") or {},
        )
    except Exception as exc:
        rs.artifact_failures.append("benchmark_audit")
        logger.exception("Failed to save benchmark audit artifacts: %s", exc)


def _collect_outputs(rs: _MainState) -> None:
    """Data-quality report, routing log and the remaining optional artifacts."""

    # Save Data Quality Report
    dq_report_path = os.path.join(rs.output_dir, "data_quality_report.json")
    try:
        with open(dq_report_path, "w", encoding="utf-8") as f:
            json.dump(rs.quality_report, f, indent=4, default=str)
        print(f"Data quality report saved to {dq_report_path}")
    except Exception as e:
        rs.artifact_failures.append("data_quality_report")
        logger.exception("Failed to save data quality report: %s", e)

    # Move Routing Log
    final_routing_log = os.path.join(rs.output_dir, "routing_log.csv")
    if os.path.exists(rs.temp_routing_log):
        try:
            shutil.move(rs.temp_routing_log, final_routing_log)
            print(f"Routing log moved to {final_routing_log}")
        except Exception as e:
            rs.artifact_failures.append("routing_log")
            logger.exception("Failed to move routing log: %s", e)

    # T-2.9: event pipeline audit is mandatory for a normal backtest.  If
    # trading occurred, every signal/risk/order/fill/close stage must exist.
    try:
        rs.event_summary = write_event_log(
            rs.results.get("event_log") or (), rs.output_path / "event_log.jsonl"
        )
        rs.audit_coverage = validate_audit_coverage(
            event_summary=rs.event_summary,
            routing_log_path=final_routing_log,
            routing_required=not rs.args.disable_routing_log,
            trade_count=len(rs.results.get("trades") or []),
            close_count=sum((rs.results.get("close_events") or {}).values()),
        )
        if rs.audit_coverage["status"] != "ok":
            rs.artifact_failures.append("event_audit_coverage")
    except Exception as exc:
        rs.event_summary = {"status": "failed", "error": str(exc)}
        rs.audit_coverage = {"status": "failed", "missing_event_types": ["event_log"]}
        rs.artifact_failures.append("event_log")
        logger.exception("Failed to save mandatory event audit: %s", exc)

    # T-2.11: never pretend the primary feed verified itself.  Without an
    # independent directory this report stays explicitly UNVERIFIED.
    try:
        closed_trades = rs.reporter._reconstruct_closed_trades(
            pd.DataFrame(rs.results.get("trades") or [])
        )
        secondary_data = (
            _load_secondary_data(rs.args.secondary_data_dir, rs.data_map)
            if rs.args.secondary_data_dir else None
        )
        rs.market_data_audit = cross_verify_top_trades(
            closed_trades, rs.data_map, secondary_data, top_n=20
        )
        write_json_report(rs.output_path / "top_trade_market_data_audit.json", rs.market_data_audit)
        if rs.args.require_secondary_audit and rs.market_data_audit["status"] != "passed":
            rs.artifact_failures.append("secondary_data_audit")
    except Exception as exc:
        rs.market_data_audit = {"status": "failed", "error": str(exc)}
        rs.artifact_failures.append("secondary_data_audit")
        logger.exception("Failed top-trade market data audit: %s", exc)


def _execution_identity(rs: _MainState) -> dict:
    """The execution part of the run manifest: a pure function of the finished run."""
    return {
        **runtime_identity(),
        "capital": rs.args.capital,
        "coin_selector": rs.selector_summary,
        "data_symbol_order": list(rs.data_map),
        "seed": rs.args.seed,
        "slippage": rs.engine.slippage,
        "random_slip": rs.args.random_slip,
        "warmup_period": rs.engine.warmup_period,
        "capital_allocation": asdict(rs.engine.capital_allocation_policy),
        "capital_allocation_digest": capital_allocation_digest(rs.results),
        "signal_observation": rs.results["signal_observation"]["policy"] if rs.results.get("signal_observation") else None,
        "signal_observation_digest": rs.signal_summary.get("research_payload_sha256"),
        "signal_meta_layer": rs.engine.signal_meta_policy.to_dict(),
        "signal_meta_layer_digest": rs.meta_summary.get("research_payload_sha256"),
        "signal_meta_layer_artifacts": rs.meta_summary.get("artifacts", []),
        "signal_adaptive": rs.engine.signal_adaptive_policy.to_dict(),
        "signal_adaptive_digest": rs.adaptive_summary.get("research_payload_sha256"),
        "signal_adaptive_artifacts": rs.adaptive_summary.get("artifacts", []),
        "signal_meta_replay": rs.engine.signal_meta_replay_policy.to_dict(),
        "signal_meta_replay_digest": rs.meta_replay_summary.get("research_payload_sha256"),
        "signal_meta_replay_artifacts": rs.meta_replay_summary.get("artifacts", []),
        "alignment_mode": rs.args.alignment_mode,
        "benchmark_mode": rs.args.benchmark_mode,
        "benchmark_rebalance_cost_bps": rs.args.benchmark_rebalance_cost_bps,
        "timeframe": rs.args.timeframe,
        "account_mode": rs.results.get("account_mode"),
        "routing_log_enabled": not rs.args.disable_routing_log,
        "result_digest": deterministic_result_digest(rs.results),
        "temporal_policy": vars(parse_temporal_policy(getattr(rs.engine, "temporal_policy", None))),
        "temporal_data_sha256": sha256_file(rs.output_path / "temporal_data.json"),
    }

def _write_final_artifacts(rs: _MainState) -> None:
    """Manifest, hashes and the artifact index."""

    # T-2.1--T-2.5/T-2.13: snapshot exact engine inputs and write the complete
    # identity only after every auditable output exists.
    try:
        snapshot_entries = save_data_snapshots(rs.data_map, rs.output_path / "data_inputs")
        requested_period = {
            "start": rs.start_date.strftime("%Y-%m-%d"),
            "end": rs.end_date.strftime("%Y-%m-%d"),
            "days": rs.args.days,
        }
        identity = data_identity(
            rs.data_map,
            source=rs.args.source,
            exchange=rs.args.exchange if rs.args.source == "ccxt" else None,
            market_type=rs.args.market_type or rs.results.get("account_mode"),
            timeframe=rs.args.timeframe,
            timezone_name=rs.args.data_timezone,
            downloaded_at=rs.download_completed_at,
        )
        identity["download_started_at"] = rs.download_started_at
        identity["universe"] = rs.universe_identity
        artifact_names = [
            "metrics.json", "closed_trades.csv", "reconciliation.json", "execution_quality.json", "invalid_closed_trades.json",
            "equity.csv", "trades.csv", "report.txt", "benchmark.csv",
            "benchmark_fixed.csv", "benchmark_dynamic.csv", "benchmark_weights.csv",
            "benchmark_turnover_cost.csv", "benchmark_metadata.json",
            "routing_log.csv", "event_log.jsonl", "data_quality_report.json",
            "top_trade_market_data_audit.json",
            "margin_ledger.csv", "financing_ledger.csv", "execution_audit.csv",
            "breaker_audit.csv", "breaker_state.json", "backtest_lifecycle.json",
            "temporal_data.json",
        ]
        artifact_names.extend(rs.signal_summary.get("artifacts", []))
        artifact_names.append("capital_allocation.json")
        if (rs.results.get("capital_allocation_policy") or {}).get("enabled"):
            artifact_names.append("capital_allocation.csv")
        artifact_names.extend(rs.meta_summary.get("artifacts", []))
        artifact_names.extend(rs.adaptive_summary.get("artifacts", []))
        artifact_names.extend(rs.meta_replay_summary.get("artifacts", []))
        artifact_names.append("coin_selector.json")
        if rs.selector_summary["enabled"]:
            artifact_names.append("coin_selection.csv")
        execution_identity = _execution_identity(rs)
        if getattr(rs.engine, "temporal_financing", None) is not None:
            from signal_meta.temporal_financing import TemporalFinancing
            from signal_meta.signal_observation_types import fingerprint
            if isinstance(rs.engine.temporal_financing, TemporalFinancing):
                execution_identity["temporal_financing_sha256"] = fingerprint(rs.engine.temporal_financing.export())
        manifest = build_run_manifest(
            run_id=rs.results["run_id"],
            repo_root=ROOT,
            config_path=ROOT / "config" / "params.yaml",
            requested_period=requested_period,
            effective_period=rs.effective_period,
            data=identity,
            snapshots=snapshot_entries,
            execution=execution_identity,
            artifacts=artifact_hashes(rs.output_path, artifact_names),
            audit={
                "event_log": rs.event_summary,
                "coverage": rs.audit_coverage,
                "top_trade_market_data": rs.market_data_audit,
            },
        )
        write_manifest(rs.output_path / "run_manifest.json", manifest)
    except Exception as exc:
        rs.artifact_failures.append("run_manifest")
        logger.exception("Failed to create reproducible run manifest: %s", exc)


def _print_summary(rs: _MainState) -> int:
    """Print the core metrics and report the artifact failures."""

    print("\n" + "=" * 30)
    print("BACKTEST RESULTS (core metrics)")
    print("=" * 30)
    # English-only on stdout: Windows consoles commonly default to the GBK
    # codepage, which garbles Chinese text; report.txt is written with an
    # explicit utf-8 encoding and carries the bilingual labels instead.
    print(format_primary_metrics(rs.metrics, bilingual=False))
    print("=" * 30)
    print("Full metrics (drawdown events, trade quality, attribution, benchmark) written to report.txt")

    print(f"\nReport saved to: {rs.output_dir}")
    if rs.artifact_failures:
        print(
            "Report completed with artifact failures: "
            + ", ".join(rs.artifact_failures),
            file=sys.stderr,
        )
        return 5
    return 0


def main(argv=None) -> int:
    """Parse, validate and orchestrate one backtest run."""
    rs = _MainState()
    rs.argv = argv
    code = _parse_arguments(rs)
    if code is not None:
        return code
    code = _validate_options(rs)
    if code is not None:
        return code
    code = _prepare_run(rs)
    if code is not None:
        return code
    code = _run_backtest(rs)
    if code is not None:
        return code
    _build_report(rs)
    code = _describe_run(rs)
    if code is not None:
        return code
    _write_artifacts(rs)
    _collect_outputs(rs)
    _write_final_artifacts(rs)
    return _print_summary(rs)
