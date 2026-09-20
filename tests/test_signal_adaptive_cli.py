"""P2/P3 are separately identified research consumers of unchanged P0/P1."""
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
from core.signal_adaptive_types import AdaptiveEVPolicy, MetaReplayPolicy
from core.signal_ev_types import EVPolicy

from tests.engine_baseline_harness import build_synthetic_data_map


ROOT = Path(__file__).resolve().parents[1]


def research_settings():
    return {"signal_observation": {"enabled": True, "horizons": [1, 3], "ghost_horizon": 3},
            "signal_meta_layer": EVPolicy(enabled=True, train_days=35, test_days=20,
                                           embargo_days=3, block_days=3),
            "signal_adaptive": AdaptiveEVPolicy(enabled=True,
                ev_policy=EVPolicy(enabled=True, train_days=35, test_days=20,
                                   embargo_days=3, block_days=3),
                regime_fit_days=12, regime_neighbors=2, min_regime_samples=6,
                attribution_min_samples=6, attribution_min_blocks=2),
            "signal_meta_replay": MetaReplayPolicy(enabled=True, horizon_bars=3)}


@pytest.fixture(scope="module")
def runs():
    data = build_synthetic_data_map(symbols=("Z", "A"), bars=110)
    results = {}
    for random_slip in (False, True):
        for enabled in (False, True):
            random.seed(20260918)
            np.random.seed(20260918)
            settings = research_settings()
            if not enabled:
                settings.update(signal_adaptive={"enabled": False}, signal_meta_replay={"enabled": False})
            engine = BacktestEngine(initial_capital=10000, random_slip=random_slip,
                                    run_id="p2-p3-isolation", **settings)
            results[random_slip, enabled] = engine, engine.run(data, routing_log_enabled=False)
    return data, results


def test_separate_policy_defaults_and_transitive_dependencies_preserve_p1(monkeypatch):
    baseline = BacktestEngine()
    assert not baseline.signal_adaptive_policy.enabled
    assert not baseline.signal_meta_replay_policy.enabled
    monkeypatch.setitem(config._config, "signal_adaptive", {"regime_fit_days": 99})
    p1 = EVPolicy(half_life_days=70., min_ev_bps=5.)
    engine = BacktestEngine(signal_meta_replay={"enabled": True},
        signal_observation={"enabled": False}, signal_meta_layer=p1)
    assert engine.signal_meta_replay_policy.enabled
    assert engine.signal_adaptive_policy.enabled
    assert engine.signal_adaptive_policy.regime_fit_days == 99
    assert engine.observation_policy.enabled
    assert engine.signal_meta_policy.to_dict() == p1.to_dict() | {"enabled": True}
    assert "regime_fit_days" not in asdict(engine.observation_policy)
    assert "regime_fit_days" not in engine.signal_meta_policy.to_dict()
    p2_only = BacktestEngine(signal_adaptive={"enabled": True},
        signal_meta_layer={"enabled": False}, signal_observation={"enabled": False})
    assert p2_only.signal_meta_policy.enabled and p2_only.observation_policy.enabled
    assert not p2_only.signal_meta_replay_policy.enabled
    explicit = BacktestEngine(signal_adaptive=AdaptiveEVPolicy(enabled=False),
                              signal_meta_replay=MetaReplayPolicy(enabled=False))
    assert explicit.signal_adaptive_policy == AdaptiveEVPolicy()


def test_p3_horizon_must_be_preregistered_without_extending_p0():
    with pytest.raises(ValueError, match="already exist in the P0"):
        BacktestEngine(signal_observation={"horizons": [1, 3]},
                       signal_meta_replay={"enabled": True, "horizon_bars": 5})


@pytest.mark.parametrize("random_slip", [False, True])
def test_research_replays_leave_official_p0_p1_outputs_and_random_sequence_unchanged(runs, random_slip):
    _, results = runs
    _, off = results[random_slip, False]
    engine, on = results[random_slip, True]
    assert off["signal_adaptive"] is off["signal_meta_replay"] is None
    assert on["signal_adaptive"]["status"] == "complete"
    assert on["signal_meta_replay"]["status"] == "complete", on["signal_meta_replay"]["errors"]
    assert on["signal_adaptive"]["policy"] == engine.signal_adaptive_policy.to_dict()
    assert on["signal_meta_replay"]["policy"] == engine.signal_meta_replay_policy.to_dict()
    assert on["trades"]
    assert deterministic_result_digest(off) == deterministic_result_digest(on)
    assert signal_observation_digest(off["signal_observation"]) == signal_observation_digest(on["signal_observation"])
    assert signal_meta_layer_digest(off["signal_meta_layer"]) == signal_meta_layer_digest(on["signal_meta_layer"])
    assert off["strategy_health"] == on["strategy_health"]
    assert off["allocation_audit"] == on["allocation_audit"]


def test_cli_flags_pass_independent_policies(monkeypatch, tmp_path):
    factory = MagicMock()
    monkeypatch.setattr(entrypoint, "BacktestEngine", factory)
    monkeypatch.chdir(tmp_path)
    args = entrypoint._build_parser().parse_args(["--adaptive-signal-meta", "--signal-meta-replay"])
    entrypoint._execute_backtest(args, {})
    assert factory.call_args.kwargs["signal_adaptive"] == {"enabled": True}
    assert factory.call_args.kwargs["signal_meta_replay"] == {"enabled": True}
    assert factory.call_args.kwargs["signal_meta_layer"] is None


def manifest_for(tmp_path, runs, *, adaptive=True):
    from backtest.reporting.signal_adaptive import research_digest
    data, results = runs
    engine, result = results[False, adaptive]
    execution = {"capital": 10000, "seed": 20260918, "random_slip": False,
        "slippage": engine.slippage, "warmup_period": engine.warmup_period,
        "alignment_mode": engine.alignment_mode, "benchmark_mode": engine.benchmark_mode,
        "benchmark_rebalance_cost_bps": engine.benchmark_rebalance_cost_bps,
        "timeframe": engine.timeframe, "account_mode": result["account_mode"],
        "result_digest": deterministic_result_digest(result), "data_symbol_order": list(data),
        "signal_observation": result["signal_observation"]["policy"],
        "signal_observation_digest": signal_observation_digest(result["signal_observation"]),
        "signal_meta_layer": engine.signal_meta_policy.to_dict(),
        "signal_meta_layer_digest": signal_meta_layer_digest(result["signal_meta_layer"])}
    if adaptive:
        for name, policy in (("signal_adaptive", engine.signal_adaptive_policy),
                             ("signal_meta_replay", engine.signal_meta_replay_policy)):
            execution[name] = policy.to_dict()
            execution[f"{name}_digest"] = research_digest(result[name])
    manifest = {"schema_version": "2.0", "code": {}, "run_id": result["run_id"],
        "config": {"sha256": sha256_file(ROOT/"config/params.yaml")},
        "data_snapshots": save_data_snapshots(data, tmp_path/"data_inputs"), "execution": execution}
    path = tmp_path/"run_manifest.json"
    write_manifest(path, manifest)
    return path, manifest


def test_p2_p3_manifest_replays_both_research_digests(runs, tmp_path, capsys):
    path, _ = manifest_for(tmp_path, runs)
    assert entrypoint.replay_manifest(str(path)) == 0
    output = capsys.readouterr().out
    assert '"status": "passed"' in output
    assert '"signal_adaptive_expected"' in output
    assert '"signal_meta_replay_expected"' in output


@pytest.mark.parametrize("name", ["signal_adaptive", "signal_meta_replay"])
def test_enabled_research_requires_recorded_digest_and_rejects_tampering(runs, tmp_path, name):
    path, manifest = manifest_for(tmp_path, runs)
    del manifest["execution"][f"{name}_digest"]
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7
    manifest["execution"][f"{name}_digest"] = "0" * 64
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 8


def test_manifest_with_invalid_policy_or_unrecorded_dependency_is_refused(runs, tmp_path):
    path, manifest = manifest_for(tmp_path, runs)
    manifest["execution"]["signal_meta_replay"]["enabled"] = "yes"
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7
    manifest["execution"]["signal_meta_replay"]["enabled"] = True
    manifest["execution"]["signal_adaptive"]["enabled"] = False
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7
    manifest["execution"]["signal_adaptive"]["enabled"] = True
    manifest["execution"]["signal_meta_replay"]["horizon_bars"] = 99
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7
    manifest["execution"]["signal_meta_replay"]["horizon_bars"] = 3
    del manifest["execution"]["signal_observation_digest"]
    write_manifest(path, manifest)
    assert entrypoint.replay_manifest(str(path)) == 7


def test_p1_manifest_replays_unchanged_even_if_new_config_enables_p2_p3(runs, tmp_path, monkeypatch):
    path, _ = manifest_for(tmp_path, runs, adaptive=False)
    monkeypatch.setitem(config._config, "signal_adaptive", {"enabled": True})
    monkeypatch.setitem(config._config, "signal_meta_replay", {"enabled": True})
    actual_engine, constructed = entrypoint.BacktestEngine, []

    def capture(**kwargs):
        constructed.append(kwargs)
        return actual_engine(**kwargs)

    monkeypatch.setattr(entrypoint, "BacktestEngine", capture)
    assert entrypoint.replay_manifest(str(path)) == 0
    assert constructed[0]["signal_adaptive"]["enabled"] is False
    assert constructed[0]["signal_meta_replay"]["enabled"] is False
    assert type(constructed[0]["initial_capital"]) is int


@pytest.mark.parametrize("profile", ["workbook", "compact", "full"])
def test_all_profiles_export_research_and_full_manifest_registers_artifacts(runs, monkeypatch, tmp_path, profile):
    from backtest.reporting.signal_adaptive import research_digest
    data, results = runs
    engine, result = results[False, True]
    monkeypatch.chdir(tmp_path)
    now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(entrypoint, "_load_requested_data", lambda *args: (data, {}, now, now))
    monkeypatch.setattr(entrypoint, "_execute_backtest", lambda *args: (engine, result, str(tmp_path/"absent.csv")))
    monkeypatch.setattr(entrypoint.ReportGenerator, "generate", lambda *args, **kwargs: {})
    monkeypatch.setattr(entrypoint, "format_primary_metrics", lambda *args, **kwargs: "test metrics")
    assert entrypoint.main(["--start", "2020-01-01", "--end", "2020-04-20", "--signal-meta-replay",
        "--report-profile", profile, "--disable-routing-log", "--symbols", "Z", "A"]) == 0
    folder, = (tmp_path/"reports").iterdir()
    summaries = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*summary.json")]
    for name, policy in (("signal_adaptive", engine.signal_adaptive_policy),
                         ("signal_meta_replay", engine.signal_meta_replay_policy)):
        summary, = [s for s in summaries if s.get("research_payload_sha256") == research_digest(result[name])]
        for filename in summary["artifacts"]:
            assert (folder/filename).exists()
        if profile == "full":
            manifest = json.loads((folder/"run_manifest.json").read_text(encoding="utf-8"))
            assert manifest["execution"][name] == policy.to_dict()
            assert manifest["execution"][f"{name}_digest"] == summary["research_payload_sha256"]
            assert manifest["execution"][f"{name}_artifacts"] == summary["artifacts"]
            assert set(summary["artifacts"]) <= set(manifest["artifacts"])
