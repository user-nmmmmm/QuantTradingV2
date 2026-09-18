"""P1 wiring is opt-in, post-trade, profile-independent and replayable."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from unittest.mock import MagicMock

import numpy as np
import pytest

import main as entrypoint
from backtest.engine import BacktestEngine
from backtest.reporting.signal_meta_layer import signal_meta_layer_digest
from backtest.reporting.signal_observation import signal_observation_digest
from config.config import config
from core.reproducibility import (
    deterministic_result_digest, save_data_snapshots, sha256_file, write_manifest,
)
from core.signal_ev_types import EVPolicy
from tests.engine_baseline_harness import build_synthetic_data_map


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runs():
    data = build_synthetic_data_map(symbols=("Z", "A"), bars=110)
    p0 = {"enabled": True, "horizons": [1, 3], "ghost_horizon": 3}
    off = BacktestEngine(initial_capital=10000, signal_observation=p0,
                         signal_meta_layer={"enabled": False})
    on = BacktestEngine(initial_capital=10000,
        signal_observation={**p0, "enabled": False},
        signal_meta_layer=EVPolicy(enabled=True, train_days=35, test_days=20,
                                  embargo_days=3, block_days=3))
    return data, off, off.run(data, routing_log_enabled=False), on, on.run(data, routing_log_enabled=False)


def test_policy_defaults_and_p0_dependency_are_isolated(monkeypatch):
    assert config.get("signal_meta_layer") == EVPolicy().to_dict()
    monkeypatch.setitem(config._config, "signal_meta_layer", {"enabled": True, "half_life_days": 70.0})
    engine = BacktestEngine(signal_meta_layer={"min_ev_bps": 5.0}, signal_observation={"enabled": False})
    assert engine.signal_meta_policy == EVPolicy(enabled=True, half_life_days=70.0, min_ev_bps=5.0)
    assert engine.observation_policy.enabled is True
    overridden = BacktestEngine(signal_meta_layer=EVPolicy(enabled=False))
    assert overridden.signal_meta_policy == EVPolicy()
    assert not overridden.observation_policy.enabled
    assert "half_life_days" not in asdict(engine.observation_policy)


def test_enabled_p1_is_postprocessing_with_identical_official_and_p0_results(runs):
    _, _, off, engine, on = runs
    assert off["signal_meta_layer"] is None
    assert on["signal_meta_layer"]["status"] == "complete"
    assert on["signal_meta_layer"]["policy"] == engine.signal_meta_policy.to_dict()
    assert on["signal_meta_layer"]["folds"]
    assert len(on["signal_meta_layer"]["predictions"]) == len(on["signal_observation"]["candidates"])*2
    assert deterministic_result_digest(off) == deterministic_result_digest(on)
    assert signal_observation_digest(off["signal_observation"]) == signal_observation_digest(on["signal_observation"])
    assert off["strategy_health"] == on["strategy_health"]
    assert off["allocation_audit"] == on["allocation_audit"]


def test_random_slippage_sequence_is_unchanged_by_p1():
    data = build_synthetic_data_map(symbols=("Z", "A"), bars=110)
    results = {}
    for enabled in (False, True):
        random.seed(20260918)
        np.random.seed(20260918)
        engine = BacktestEngine(initial_capital=10000, random_slip=True,
            run_id="p1-random-isolation",
            signal_observation={"enabled": True, "horizons": [1, 3], "ghost_horizon": 3},
            signal_meta_layer={"enabled": enabled, "train_days": 35,
                               "test_days": 20, "embargo_days": 3, "block_days": 3})
        results[enabled] = engine.run(data, routing_log_enabled=False)
    off, on = results[False], results[True]
    assert on["trades"]  # Check an actual random-slip fill path, not a flat account.
    assert on["signal_meta_layer"]["status"] == "complete"
    assert deterministic_result_digest(off) == deterministic_result_digest(on)
    assert signal_observation_digest(off["signal_observation"]) == signal_observation_digest(on["signal_observation"])
    assert off["strategy_health"] == on["strategy_health"]
    assert off["allocation_audit"] == on["allocation_audit"]


def test_execute_cli_flag_passes_separate_policy(monkeypatch, tmp_path):
    fake = MagicMock()
    factory = MagicMock(return_value=fake)
    monkeypatch.setattr(entrypoint, "BacktestEngine", factory)
    monkeypatch.chdir(tmp_path)
    args = entrypoint._build_parser().parse_args(["--signal-meta-layer"])
    entrypoint._execute_backtest(args, {})
    assert factory.call_args.kwargs["signal_meta_layer"] == {"enabled": True}
    assert factory.call_args.kwargs["signal_observation"] is None


def manifest_for(tmp_path, runs, *, meta=True):
    data, off_engine, off, on_engine, on = runs
    engine, result = (on_engine, on) if meta else (off_engine, off)
    execution = {"capital": 10000, "seed": 456, "random_slip": False,
        "slippage": engine.slippage, "warmup_period": engine.warmup_period,
        "alignment_mode": engine.alignment_mode, "benchmark_mode": engine.benchmark_mode,
        "benchmark_rebalance_cost_bps": engine.benchmark_rebalance_cost_bps,
        "timeframe": engine.timeframe, "account_mode": result["account_mode"],
        "result_digest": deterministic_result_digest(result), "data_symbol_order": list(data),
        "signal_observation": result["signal_observation"]["policy"],
        "signal_observation_digest": signal_observation_digest(result["signal_observation"])}
    if meta:
        execution.update(signal_meta_layer=engine.signal_meta_policy.to_dict(),
            signal_meta_layer_digest=signal_meta_layer_digest(result["signal_meta_layer"]))
    manifest = {"schema_version": "2.0", "code": {}, "run_id": result["run_id"],
        "config": {"sha256": sha256_file(ROOT/"config/params.yaml")},
        "data_snapshots": save_data_snapshots(data, tmp_path/"data_inputs"), "execution": execution}
    path = tmp_path/"run_manifest.json"
    write_manifest(path, manifest)
    return path, manifest


def test_replay_p1_policy_digest_and_original_p0_input_order(runs, tmp_path, capsys):
    path, _ = manifest_for(tmp_path, runs)
    assert entrypoint.replay_manifest(str(path)) == 0
    output = capsys.readouterr().out
    assert '"status": "passed"' in output
    assert '"signal_meta_layer_expected"' in output


def test_replay_p1_digest_tampering_fails(runs, tmp_path):
    path, manifest = manifest_for(tmp_path, runs)
    manifest["execution"]["signal_meta_layer_digest"] = "0"*64
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 8


def test_replay_enabled_policy_without_digest_is_refused(runs, tmp_path):
    path, manifest = manifest_for(tmp_path, runs)
    del manifest["execution"]["signal_meta_layer_digest"]
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7


def test_legacy_manifest_explicitly_disables_new_model(runs, tmp_path, monkeypatch):
    path, _ = manifest_for(tmp_path, runs, meta=False)
    monkeypatch.setitem(config._config, "signal_meta_layer", {"enabled": True})
    actual_engine = entrypoint.BacktestEngine
    constructed = []

    def record_engine(**kwargs):
        constructed.append(kwargs)
        return actual_engine(**kwargs)

    monkeypatch.setattr(entrypoint, "BacktestEngine", record_engine)
    assert entrypoint.replay_manifest(str(path)) == 0
    assert constructed[0]["signal_meta_layer"] == {"enabled": False}
    assert constructed[0]["initial_capital"] == 10000
    assert type(constructed[0]["initial_capital"]) is int


@pytest.mark.parametrize("profile", ["workbook", "compact", "full"])
def test_all_profiles_export_p1_and_full_manifest_records_its_identity(runs, monkeypatch, tmp_path, profile):
    data, _, _, engine, result = runs
    monkeypatch.chdir(tmp_path)
    now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(entrypoint, "_load_requested_data", lambda *args: (data, {}, now, now))
    monkeypatch.setattr(entrypoint, "_execute_backtest", lambda *args: (engine, result, str(tmp_path/"absent.csv")))
    monkeypatch.setattr(entrypoint.ReportGenerator, "generate", lambda *args, **kwargs: {})
    monkeypatch.setattr(entrypoint, "format_primary_metrics", lambda *args, **kwargs: "test metrics")
    assert entrypoint.main(["--start", "2020-01-01", "--end", "2020-04-20", "--signal-meta-layer",
        "--report-profile", profile, "--disable-routing-log", "--symbols", "Z", "A"]) == 0
    folders = list((tmp_path/"reports").iterdir())
    assert len(folders) == 1
    summary = json.loads((folders[0]/"ev_summary.json").read_text(encoding="utf-8"))
    assert summary["research_payload_sha256"] == signal_meta_layer_digest(result["signal_meta_layer"])
    for name in summary["artifacts"]:
        assert (folders[0]/name).exists()
    if profile == "full":
        manifest = json.loads((folders[0]/"run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["execution"]["signal_meta_layer"] == engine.signal_meta_policy.to_dict()
        assert manifest["execution"]["signal_meta_layer_digest"] == summary["research_payload_sha256"]
        assert manifest["execution"]["signal_meta_layer_artifacts"] == summary["artifacts"]
        assert set(summary["artifacts"]) <= set(manifest["artifacts"])
