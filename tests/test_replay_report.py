"""A full replay consumes frozen values once and never overwrites its baseline."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

import main as entrypoint
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from core.reproducibility import (
    build_run_manifest, data_identity, deterministic_result_digest, load_data_snapshots,
    save_data_snapshots, sha256_file, sha256_frame, write_manifest,
)
from tests.engine_baseline_harness import build_synthetic_data_map


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def frozen_run(tmp_path_factory):
    baseline = tmp_path_factory.mktemp("replay_baseline")
    frames = build_synthetic_data_map(bars=180)
    entries = save_data_snapshots(frames, baseline / "data_inputs")
    # Freeze after the same round-trip reader used by replay, never a second
    # approximate reader. Include three symbols and nontrivial 17-digit values.
    frames = load_data_snapshots(baseline / "data_inputs", entries, verify=True)
    engine = BacktestEngine(initial_capital=10000.0, warmup_period=30,
        slippage=.0005, random_slip=False, run_id="synthetic-frozen-replay",
        signal_observation={"enabled": False}, signal_meta_layer={"enabled": False},
        signal_adaptive={"enabled": False}, signal_meta_replay={"enabled": False})
    result = engine.run(frames, routing_log_enabled=False)
    assert result["accounting_check"]["ok"]
    timeline = engine.market_data_adapter.timestamps
    requested = {"start": "2024-01-01", "end": "2024-06-28", "days": 179}
    effective = {"start": timeline.min(), "end": timeline.max(), "bars": len(timeline),
        "alignment_mode": "union", "per_symbol": {
            s: {"start": f.index.min(), "end": f.index.max(), "rows": len(f)}
            for s, f in frames.items()}}
    execution = {"capital": 10000.0, "seed": 42, "warmup_period": 30,
        "slippage": .0005, "random_slip": False, "alignment_mode": "union",
        "benchmark_mode": "fixed", "benchmark_rebalance_cost_bps": 5.0,
        "timeframe": "1d", "account_mode": "spot_margin", "data_symbol_order": list(frames),
        "routing_log_enabled": True, "result_digest": deterministic_result_digest(result),
        "signal_observation": None, "signal_observation_digest": None,
        "signal_meta_layer": engine.signal_meta_policy.to_dict(), "signal_meta_layer_digest": None,
        "signal_adaptive": engine.signal_adaptive_policy.to_dict(), "signal_adaptive_digest": None,
        "signal_meta_replay": engine.signal_meta_replay_policy.to_dict(), "signal_meta_replay_digest": None}
    manifest = build_run_manifest(run_id=result["run_id"], repo_root=ROOT,
        config_path=ROOT / "config/params.yaml", requested_period=requested,
        effective_period=effective, data=data_identity(frames, source="synthetic", exchange=None,
            market_type="spot_margin", timeframe="1d", timezone_name="UTC",
            downloaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        snapshots=entries, execution=execution, artifacts={}, audit={})
    write_manifest(baseline / "run_manifest.json", manifest)
    return baseline


@pytest.fixture
def baseline(tmp_path, frozen_run):
    path = tmp_path / "baseline"
    shutil.copytree(frozen_run, path)
    return path / "run_manifest.json"


@pytest.fixture
def calls(monkeypatch):
    captured = []
    actual = BacktestEngine.run
    def run(engine, data, **kwargs):
        result = actual(engine, data, **kwargs)
        captured.append({"engine": engine, "data": data, "kwargs": kwargs, "result": result,
                         "digest": deterministic_result_digest(result)})
        return result
    monkeypatch.setattr(BacktestEngine, "run", run)
    return captured


@pytest.fixture
def cheap_charts(monkeypatch):
    # Native report tables, reconciliation, serialization and the PDF writer
    # still run; only chart rendering is irrelevant to the replay contract.
    for name in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(ReportGenerator, name, lambda *args, **kwargs: None)


def document(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_full_replay_exports_once_with_exact_inputs_and_new_code_identity(
        baseline, tmp_path, calls, cheap_charts):
    original = document(baseline)
    baseline_hash = sha256_file(baseline)
    output = tmp_path / "new_report"
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(output)) == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["engine"].run_id == original["run_id"]
    assert call["engine"].initial_capital == original["execution"]["capital"]
    assert type(call["engine"].initial_capital) is float
    assert call["engine"].warmup_period == 30
    assert list(call["data"]) == original["execution"]["data_symbol_order"]
    assert call["kwargs"] == {"routing_log_enabled": True,
        "routing_log_path": str(output / "routing_log.csv")}
    for symbol, frame in call["data"].items():
        assert sha256_frame(frame) == original["data_snapshots"][symbol]["sha256"]
    assert deterministic_result_digest(call["result"]) == call["digest"]
    assert call["digest"] == original["execution"]["result_digest"]
    assert sha256_file(baseline) == baseline_hash
    identity = document(output / "comparison_identity.json")
    assert identity["comparison_id"] != identity["engine_run_id"]
    assert identity["same_engine_run_count"] == 1
    assert identity["baseline_manifest_sha256"] == baseline_hash
    assert identity["current_source_hashes"]["backtest/replay_reporting.py"] == sha256_file(ROOT / "backtest/replay_reporting.py")
    assert identity["started_at"] <= identity["completed_at"]
    current = document(output / "run_manifest.json")
    assert current["schema_version"] == "2.0" and current["run_id"] == original["run_id"]
    assert current["data_snapshots"] == original["data_snapshots"]
    for key in ("config", "period", "data"):
        assert current[key] == original[key]
    assert current["execution"]["result_digest"] == original["execution"]["result_digest"]
    assert current["comparison_identity"] == identity
    comparison = document(output / "replay_comparison.json")
    assert comparison["status"] == "passed" and comparison["report_status"] == "complete"
    assert comparison["artifact_failures"] == []
    for name in ("report.pdf", "metrics.json", "closed_trades.csv", "trades.csv", "equity.csv",
                 "reconciliation.json", "execution_quality.json", "event_log.jsonl",
                 "financing_ledger.csv", "margin_ledger.csv", "entry_observations.csv",
                 "risk_budget_reconciliation.csv", "allocation_audit.csv", "cohort_trades.csv",
                 "strategy_health_timeline.csv", "strategy_health.json", "backtest_lifecycle.json",
                 "stop_order_audit.csv", "routing_log.csv", "accounting_check.json"):
        assert name in current["artifacts"]
        assert sha256_file(output / name) == current["artifacts"][name]["sha256"]
    assert (output / "report.pdf").read_bytes().startswith(b"%PDF")


def test_legacy_replay_has_no_output_and_still_runs_once(baseline, calls, capsys):
    assert entrypoint.replay_manifest(str(baseline)) == 0
    assert len(calls) == 1 and calls[0]["kwargs"] == {"routing_log_enabled": False}
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and "report_status" not in report
    assert not (baseline.parent / "comparison_identity.json").exists()


def test_existing_output_is_refused_without_execution_or_overwrite(baseline, tmp_path, calls):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep")
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(output)) == 7
    assert calls == [] and sentinel.read_text() == "keep"
    assert list(output.iterdir()) == [sentinel]


@pytest.mark.parametrize("destination", ["baseline", "baseline/nested"])
def test_cannot_write_inside_old_report(baseline, tmp_path, calls, destination):
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(tmp_path / destination)) == 7
    assert calls == []


@pytest.mark.parametrize("profile", ["workbook", "compact"])
def test_output_requires_full_profile(baseline, tmp_path, calls, profile):
    output = tmp_path / "bad_profile"
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(output), report_profile=profile) == 7
    assert calls == [] and not output.exists()


@pytest.mark.parametrize("mutation", ["config", "row_count", "bounds", "symbols", "period",
                                      "capital", "seed", "warmup", "account", "timeframe"])
def test_invalid_identity_is_refused_before_execution(baseline, tmp_path, calls, mutation):
    value = document(baseline)
    symbol = next(iter(value["data_snapshots"]))
    if mutation == "config":
        value["config"]["sha256"] = "0" * 64
    elif mutation == "row_count":
        value["data"]["symbols"][symbol]["rows"] += 1
    elif mutation == "bounds":
        value["period"]["effective"]["per_symbol"][symbol]["start"] = "2024-01-02"
    elif mutation == "symbols":
        value["execution"]["data_symbol_order"] = [symbol, symbol, symbol]
    elif mutation == "period":
        value["period"]["effective"]["bars"] += 1
    elif mutation == "capital":
        value["execution"]["capital"] = False
    elif mutation == "seed":
        value["execution"]["seed"] = 42.5
    elif mutation == "warmup":
        value["execution"]["warmup_period"] = -1
    elif mutation == "account":
        value["execution"]["account_mode"] = "unknown"
    else:
        value["execution"]["timeframe"] = "1h"
    write_manifest(baseline, value)
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(tmp_path / "rejected")) == 7
    assert calls == []


def test_snapshot_byte_tampering_is_refused(baseline, tmp_path, calls):
    value = document(baseline)
    first = next(iter(value["data_snapshots"].values()))
    snapshot = baseline.parent / "data_inputs" / first["path"]
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(tmp_path / "bad_input")) == 7
    assert calls == []


def test_mismatch_returns_eight_and_keeps_same_run_reports(baseline, tmp_path, calls, cheap_charts):
    value = document(baseline)
    value["execution"]["result_digest"]["trades"] = "0" * 64
    write_manifest(baseline, value)
    output = tmp_path / "mismatch"
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(output)) == 8
    assert len(calls) == 1
    comparison = document(output / "replay_comparison.json")
    assert comparison["status"] == "failed" and comparison["report_status"] == "complete"
    assert comparison["observed"] == calls[0]["digest"]
    assert document(output / "run_manifest.json")["execution"]["result_digest"] == calls[0]["digest"]
    assert (output / "closed_trades.csv").exists()


def test_report_failure_is_visible_and_does_not_rerun(baseline, tmp_path, calls, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("report writer failed")
    monkeypatch.setattr(ReportGenerator, "generate", fail)
    output = tmp_path / "report_failed"
    assert entrypoint.replay_manifest(str(baseline), output_dir=str(output)) == 8
    assert len(calls) == 1
    comparison = document(output / "replay_comparison.json")
    assert comparison["status"] == "failed" and comparison["report_status"] == "failed"
    assert comparison["artifact_failures"] == ["report writer failed"]
    assert not (output / "run_manifest.json").exists()


def test_cli_forwards_existing_output_and_profile_options(baseline, tmp_path, monkeypatch):
    recorded = []
    def replay(path, **kwargs):
        recorded.append((path, kwargs))
        return 8
    monkeypatch.setattr(entrypoint, "replay_manifest", replay)
    monkeypatch.setattr("sys.argv", ["main.py", "--replay-manifest", str(baseline),
        "--output-dir", str(tmp_path / "cli"), "--report-profile", "full"])
    assert entrypoint.main() == 8
    assert recorded == [(str(baseline), {"output_dir": str(tmp_path / "cli"), "report_profile": "full"})]


def test_default_precision_cannot_replace_round_trip(baseline):
    value = document(baseline)
    mismatches = 0
    for entry in value["data_snapshots"].values():
        frame = pd.read_csv(baseline.parent / "data_inputs" / entry["path"],
                            index_col="timestamp", parse_dates=True)
        mismatches += sha256_frame(frame) != entry["sha256"]
    assert mismatches > 0
