"""Export a frozen-manifest replay without running the engine a second time."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from backtest.reporting import ReportGenerator
from core.backtest_audit import (
    cross_verify_top_trades, validate_audit_coverage, write_event_log, write_json_report,
)
from core.data import DataHandler
from core.reproducibility import (
    artifact_hashes, build_run_manifest, canonical_json, code_identity, runtime_identity,
    save_data_snapshots, sha256_bytes, sha256_file, sha256_frame, write_manifest,
)
from scripts.roadmap_baseline import source_manifest


def _utc(value):
    point = pd.Timestamp(value)
    if pd.isna(point):
        raise ValueError("Missing timestamp in replay identity")
    return point.tz_localize("UTC") if point.tz is None else point.tz_convert("UTC")


def _validate_inputs(manifest, snapshots):
    """The full report requires the complete identity, including real bounds."""
    execution = manifest["execution"]
    order = execution["data_symbol_order"]
    identities = manifest["data"]["symbols"]
    if (not snapshots or not isinstance(order, list) or len(order) != len(set(order))
            or set(order) != set(snapshots) or set(identities) != set(snapshots)
            or set(manifest["data_snapshots"]) != set(snapshots)):
        raise ValueError("Input symbol identity/order differs from the manifest")
    capital = execution["capital"]
    if (isinstance(capital, bool) or not isinstance(capital, (int, float))
            or not np.isfinite(capital) or capital <= 0):
        raise ValueError("Recorded capital must be a finite positive number")
    for name, minimum in (("seed", 0), ("warmup_period", 0)):
        value = execution[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"Invalid recorded {name}")
    if execution["alignment_mode"] not in {"union", "intersection"}:
        raise ValueError("Invalid recorded alignment mode")
    if execution["benchmark_mode"] not in {"fixed", "dynamic"}:
        raise ValueError("Invalid recorded benchmark mode")
    if execution["account_mode"] not in {"spot", "spot_margin", "perpetual"}:
        raise ValueError("Invalid recorded account mode")
    if execution["timeframe"] != manifest["data"]["timeframe"]:
        raise ValueError("Recorded timeframe identities differ")
    universe = manifest["data"].get("universe", {})
    if universe.get("mode", "static") != "static":
        raise ValueError("This replay cannot restore an unrecorded point-in-time universe")
    requested = manifest["period"]["requested"]
    lower, upper = _utc(requested["start"]), _utc(requested["end"])
    if upper < lower or requested["days"] != (upper - lower).days:
        raise ValueError("Requested replay period is inconsistent")
    effective = manifest["period"]["effective"]
    if (effective["alignment_mode"] != execution["alignment_mode"]
            or set(effective["per_symbol"]) != set(snapshots)):
        raise ValueError("Effective period/symbol identities differ")
    timeline = None
    for symbol, frame in snapshots.items():
        if (frame.empty or not isinstance(frame.index, pd.DatetimeIndex)
                or frame.index.hasnans or frame.index.has_duplicates
                or not frame.index.is_monotonic_increasing):
            raise ValueError(f"Invalid frozen input timeline: {symbol}")
        if not set(DataHandler.REQUIRED_COLUMNS).issubset(frame.columns):
            raise ValueError(f"Missing frozen OHLCV columns: {symbol}")
        digest = sha256_frame(frame)
        for entry in (manifest["data_snapshots"][symbol], identities[symbol]):
            if entry["rows"] != len(frame) or entry["sha256"] != digest:
                raise ValueError(f"Frozen input rows/values differ: {symbol}")
        if set(identities[symbol]["columns"]) != set(frame.columns):
            raise ValueError(f"Frozen input columns differ: {symbol}")
        for entry in (identities[symbol], effective["per_symbol"][symbol]):
            if (entry["rows"] != len(frame) or _utc(entry["start"]) != _utc(frame.index[0])
                    or _utc(entry["end"]) != _utc(frame.index[-1])):
                raise ValueError(f"Frozen input bounds differ: {symbol}")
        if _utc(frame.index[0]) < lower or _utc(frame.index[-1]) >= upper + pd.Timedelta(days=1):
            raise ValueError(f"Frozen input exceeds requested period: {symbol}")
        if timeline is None:
            timeline = frame.index
        elif execution["alignment_mode"] == "union":
            timeline = timeline.union(frame.index)
        else:
            timeline = timeline.intersection(frame.index)
    if (timeline.empty or effective["bars"] != len(timeline)
            or _utc(effective["start"]) != _utc(timeline.min())
            or _utc(effective["end"]) != _utc(timeline.max())):
        raise ValueError("Frozen effective timeline differs from the manifest")


def prepare_report_replay(baseline_path, baseline, snapshots, *, output_dir, repo_root,
                          report_profile):
    """Validate and claim an exclusive report directory before engine execution."""
    if report_profile != "full":
        raise ValueError("A replay output requires --report-profile full")
    _validate_inputs(baseline, snapshots)
    baseline_path, repo_root = Path(baseline_path).resolve(), Path(repo_root).resolve()
    config_path = repo_root / "config/params.yaml"
    if (baseline["config"]["sha256"] != sha256_file(config_path)
            or ("content" in baseline["config"]
                and baseline["config"]["content"] != config_path.read_text(encoding="utf-8"))):
        raise ValueError("Current configuration differs from the frozen configuration")
    output = Path(output_dir).resolve()
    if output == baseline_path.parent or output.is_relative_to(baseline_path.parent):
        raise ValueError("Replay output must be outside the original report directory")
    source = source_manifest(repo_root)
    identity = {
        "schema": "backtest-replay-comparison-identity/v1",
        "comparison_id": str(uuid4()), "engine_run_id": baseline["run_id"],
        "baseline_manifest_path": str(baseline_path),
        "baseline_manifest_sha256": sha256_file(baseline_path),
        "baseline_code": baseline["code"], "current_code": code_identity(repo_root),
        "current_source_hashes": source,
        "current_source_sha256": sha256_bytes(canonical_json(source).encode("utf-8")),
        "started_at": datetime.now(timezone.utc), "same_engine_run_count": 1,
        "report_profile": "full", "output_dir": str(output),
        "run_id_policy": "engine run_id preserved for deterministic comparison; comparison_id is new",
    }
    output.mkdir(parents=True, exist_ok=False)
    write_manifest(output / "comparison_identity.json", identity)
    return output, identity


def _research_reports(result, output):
    artifacts = []
    writers = {
        "signal_observation": ("backtest.reporting.signal_observation", "write_signal_observation_report"),
        "signal_meta_layer": ("backtest.reporting.signal_meta_layer", "write_signal_meta_layer_report"),
        "signal_adaptive": ("backtest.reporting.signal_adaptive", "write_signal_adaptive_report"),
        "signal_meta_replay": ("backtest.reporting.signal_adaptive", "write_signal_meta_replay_report"),
    }
    from importlib import import_module
    for name, (module, function) in writers.items():
        payload = result.get(name)
        if payload is None:
            continue
        kwargs = {"p1_payload": result.get("signal_meta_layer")} if name == "signal_adaptive" else {}
        summary = getattr(import_module(module), function)(payload, output, **kwargs)
        if summary["status"] != "complete":
            raise ValueError(f"Incomplete {name} research report")
        artifacts.extend(summary.get("artifacts", []))
    return artifacts


def write_replay_report(output, *, baseline_path, baseline, snapshots, result, comparison,
                        comparison_identity, repo_root):
    """Write native full reports, audit facts, snapshots and current code identity."""
    output, repo_root = Path(output), Path(repo_root)
    execution = baseline["execution"]
    requested, effective = baseline["period"]["requested"], baseline["period"]["effective"]
    quality = DataHandler.generate_quality_report(snapshots, output_path=None)
    metadata = {
        "Days": requested["days"], "Start": requested["start"], "End": requested["end"],
        "Capital": execution["capital"],
        "Symbols": ", ".join(symbol.replace("-", "/") for symbol in execution["data_symbol_order"]),
        "Source": baseline["data"]["source"],
        "RequestedPeriod": f"{requested['start']} to {requested['end']}",
        "EffectivePeriod": f"{pd.Timestamp(effective['start']).tz_localize(None)} to {pd.Timestamp(effective['end']).tz_localize(None)}",
        "AlignmentMode": execution["alignment_mode"], "BenchmarkMode": execution["benchmark_mode"],
        "Timeframe": execution["timeframe"], "MarketType": baseline["data"]["market_type"],
        "AccountMode": result.get("account_mode"),
    }
    reporter = ReportGenerator(str(output))
    reporter.generate(
        result["trades"], result["equity_curve"], metadata=metadata,
        benchmark_curve=result.get("benchmark"), close_events=result.get("close_events"),
        lifecycle=result.get("lifecycle"), strategy_health=result.get("strategy_health"),
        protective_stops=result.get("protective_stop_summary"), report_profile="full",
        data_quality=quality, event_log=result.get("event_log"),
        max_holding_days=result.get("effective_max_holding_days"),
    )
    # Emit even empty ledgers so absence of activity is distinguishable from
    # a missing report. Every row comes directly from this single engine run.
    csv_facts = {
        "margin_ledger": "margin_ledger", "financing_ledger": "financing_ledger",
        "execution_audit": "execution_audit", "breaker_audit": "breaker_audit",
        "strategy_health_timeline": "strategy_health_transitions",
        "cohort_trades": "strategy_health_cohorts",
        "risk_budget_reconciliation": "risk_budget_reconciliation",
        "stop_order_audit": "stop_order_audit", "allocation_audit": "allocation_audit",
        "correlated_risk_audit": "correlated_risk_audit", "drawdown_budget_audit": "drawdown_budget_audit",
        "entry_observations": "entry_observations", "exit_lifecycle_audit": "exit_lifecycle_audit",
        "trades": "trades",
    }
    for filename, key in csv_facts.items():
        pd.DataFrame(result.get(key) or []).to_csv(output / f"{filename}.csv", index=False)
    for filename, payload in {
        "account_cost_contract": {**(result.get("account_cost_contract") or {}),
            "degenerate_ranking_batches": result.get("degenerate_ranking_batches", 0)},
        "strategy_health": result.get("strategy_health") or {},
        "breaker_state": {"account_mode": result.get("account_mode"), **(result.get("breaker_state") or {})},
        "backtest_lifecycle": result.get("lifecycle") or {},
        "accounting_check": result.get("accounting_check") or {},
        "benchmark_metadata": result.get("benchmark_metadata") or {},
        "data_quality_report": quality,
    }.items():
        write_json_report(output / f"{filename}.json", payload)
    pd.DataFrame([
        {"strategy": name, **{key: row.get(key) for key in (
            "status", "raw_setup_count", "suppressed_raw_setups", "last_raw_setup_at",
            "last_suppressed_setup_at")}}
        for name, row in (result.get("strategy_health") or {}).items()
    ]).to_csv(output / "suppressed_setups.csv", index=False)
    for name in ("benchmark_fixed", "benchmark_dynamic", "benchmark_weights"):
        if result.get(name) is not None:
            result[name].to_csv(output / f"{name}.csv")
    if isinstance(result.get("benchmark_turnover"), pd.Series) and isinstance(result.get("benchmark_costs"), pd.Series):
        pd.concat([result["benchmark_turnover"].rename("turnover"),
                   result["benchmark_costs"].rename("cost")], axis=1).to_csv(output / "benchmark_turnover_cost.csv")
    event_summary = write_event_log(result.get("event_log") or (), output / "event_log.jsonl")
    audit = validate_audit_coverage(
        event_summary=event_summary, routing_log_path=output / "routing_log.csv",
        routing_required=bool(execution.get("routing_log_enabled", False)),
        trade_count=len(result.get("trades") or []),
        close_count=sum((result.get("close_events") or {}).values()),
    )
    legs = reporter._reconstruct_closed_trades(pd.DataFrame(result.get("trades") or []))
    secondary = cross_verify_top_trades(legs, snapshots, None, top_n=20)
    write_json_report(output / "top_trade_market_data_audit.json", secondary)
    _research_reports(result, output)
    required = (
        "report.pdf", "report.txt", "metrics.json", "closed_trades.csv", "equity.csv",
        "reconciliation.json", "execution_quality.json", "invalid_closed_trades.json",
    )
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise ValueError("Missing full replay report artifacts: " + ", ".join(missing))
    snapshot_entries = save_data_snapshots(snapshots, output / "data_inputs")
    if snapshot_entries != baseline["data_snapshots"]:
        # Filenames may be disambiguated by save_data_snapshots, but values,
        # order-independent symbol membership and row counts cannot change.
        for symbol, entry in snapshot_entries.items():
            old = baseline["data_snapshots"][symbol]
            if entry["sha256"] != old["sha256"] or entry["rows"] != old["rows"]:
                raise ValueError("Replay inputs changed during execution")
    if (sha256_file(baseline_path) != comparison_identity["baseline_manifest_sha256"]
            or source_manifest(repo_root) != comparison_identity["current_source_hashes"]
            or sha256_file(repo_root / "config/params.yaml") != baseline["config"]["sha256"]):
        raise ValueError("Frozen baseline or current source/config changed during replay")
    failures = []
    if audit["status"] != "ok":
        failures.append("event_audit_coverage")
    if not result.get("accounting_check", {}).get("ok", False):
        failures.append("accounting_check")
    comparison_identity["completed_at"] = datetime.now(timezone.utc)
    comparison_identity["current_code"] = code_identity(repo_root)
    write_manifest(output / "comparison_identity.json", comparison_identity)
    comparison.update(report_status="complete" if not failures else "failed",
                      artifact_failures=failures,
                      comparison_id=comparison_identity["comparison_id"])
    if failures:
        comparison["status"] = "failed"
    write_json_report(output / "replay_comparison.json", comparison)
    current_execution = {**deepcopy(execution), **runtime_identity(),
                         "result_digest": comparison["observed"]}
    for name in ("signal_observation", "signal_meta_layer", "signal_adaptive", "signal_meta_replay"):
        current_execution[f"{name}_digest"] = comparison.get(f"{name}_observed")
    artifacts = [path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()]
    current = build_run_manifest(
        run_id=baseline["run_id"], repo_root=repo_root, config_path=repo_root / "config/params.yaml",
        requested_period=requested, effective_period=effective, data=baseline["data"],
        snapshots=snapshot_entries, execution=current_execution,
        artifacts=artifact_hashes(output, artifacts),
        audit={"event_log": event_summary, "coverage": audit, "top_trade_market_data": secondary},
    )
    current["comparison_identity"] = comparison_identity
    write_manifest(output / "run_manifest.json", current)
