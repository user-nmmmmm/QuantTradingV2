"""Offline controls for the original-account selector comparison entry point."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from backtest.reporting.operating_periods import requested_period_curve
from config.config import config
from core.reproducibility import (capital_allocation_digest, deterministic_result_digest,
                                 sha256_file, sha256_frame)
from scripts import run_selector_backtest as runner


@pytest.fixture
def registered_account(tmp_path, monkeypatch):
    """Build a local control account without relying on ignored run artifacts."""
    parameters = yaml.safe_load((runner.ROOT / "config/params.yaml").read_text(encoding="utf-8"))
    parameters["account"]["mode"] = "spot_margin"
    capital_allocation = {"enabled": True, "cash_reserve_pct": .1, "correlation_penalty": 1.,
        "cost_buffer_bps": 50., "max_gross_exposure": .9, "max_position_pct": .2,
        "max_positions": 8, "min_history": 20, "score_cap": 5., "volatility_floor": .005,
        "volatility_lookback": 60}
    parameters["allocation"]["order"] = "score_strategy_symbol"
    parameters["allocation"]["capital"] = deepcopy(capital_allocation)
    arm = {"parameters": parameters, "engine_options": {
        "initial_capital": 100000., "alignment_mode": "union", "calculate_benchmarks": False,
        "capital_allocation": capital_allocation, "run_id": "offline-original-account",
        "signal_adaptive": {"enabled": False}, "signal_meta_layer": {"enabled": False},
        "signal_meta_replay": {"enabled": False}, "signal_observation": {"enabled": False},
        "temporal_policy": {"mode": "retrospective"}, "terminal_policy": "forced_liquidation",
        "timeframe": "1d", "trading_start": "2020-01-01T00:00:00Z", "warmup_period": 30}}
    index = pd.date_range("2020-01-01", periods=4, freq="D")
    index.name = "timestamp"
    frames = {"A/USDT": pd.DataFrame({"open": [100.] * 4, "high": [101.] * 4,
        "low": [99.] * 4, "close": [100.] * 4, "volume": [100000.] * 4}, index=index)}
    data_path, smart_path = tmp_path / runner.DATA_REGISTRATION, tmp_path / runner.SMART_REGISTRATION
    data_path.parent.mkdir(parents=True)
    smart_path.parent.mkdir(parents=True)
    engine_path = data_path.parent / "input/engine/A_USDT.csv"
    engine_path.parent.mkdir(parents=True)
    frames["A/USDT"].to_csv(engine_path, index_label="timestamp")
    inputs = {"input/engine/A_USDT.csv": sha256_file(engine_path)}
    frame_hashes = {s: sha256_frame(f) for s, f in frames.items()}
    data = {"start": "2020-01-01", "end": "2020-01-04", "initial_capital": 100000.,
            "symbols": list(frames), "input_files": inputs, "engine_frame_hashes": frame_hashes,
            "limits": ["offline control fixture"]}
    smart = {"start": data["start"], "end": data["end"], "capital": 100000.,
             "arms": {"smart": arm}, "input_files": inputs, "engine_frame_hashes": frame_hashes}
    runner._save(data_path, data)
    runner._save(smart_path, smart)
    monkeypatch.setattr(runner, "REGISTRATION_HASHES", {
        "data": sha256_file(data_path), "smart": sha256_file(smart_path)})
    curve = pd.DataFrame({"equity": [100000.] * 4, "cash": [100000.] * 4}, index=index)
    result = {"equity_curve": curve, "trades": [], "accounting_check": {"ok": True},
              "lifecycle": {"status": "completed"}, "account_mode": "spot_margin",
              "capital_allocation_policy": arm["engine_options"]["capital_allocation"]}
    historical = smart_path.parent / "smart"
    historical.mkdir()
    requested_period_curve(curve, data["start"], data["end"], capital=100000.,
        lifecycle=result["lifecycle"]).to_csv(historical / "equity_requested_period.csv")
    runner._save(historical / "digest.json", deterministic_result_digest(result))
    runner._save(historical / "summary.json", {"initial_capital": 100000., "days": 4, "fill_count": 0,
        "capital_allocation_digest": capital_allocation_digest(result)})
    monkeypatch.setattr(runner, "HISTORICAL_HASHES", {name: sha256_file(historical / name)
                                                     for name in runner.HISTORICAL_HASHES})
    monkeypatch.setattr(runner, "EXPECTED_FINAL_EQUITY", 100000.)
    return SimpleNamespace(root=tmp_path, frames=frames, data_path=data_path, smart_path=smart_path,
                           engine_path=engine_path, arm=arm, result=result)


def engine_probe(monkeypatch, account):
    calls = []

    class Engine:
        def __init__(self, **options):
            calls.append({"options": options, "parameters": deepcopy(config._config)})

        def run(self, frames, *, routing_log_enabled):
            assert routing_log_enabled is False
            pd.testing.assert_frame_equal(frames["A/USDT"], account.frames["A/USDT"], check_freq=False)
            return deepcopy(account.result)

    monkeypatch.setattr(runner, "BacktestEngine", Engine)
    return calls


def policy_identity():
    selector = SimpleNamespace(model=SimpleNamespace(model_id="fixed-parent"),
        policy=SimpleNamespace(model_id="fixed-policy"), policy_threshold=.51, audit=[])
    identity = {"enabled": True, "candidate": "rl", "model_id": "fixed-policy",
        "parent_model_id": "fixed-parent", "policy_threshold": .51, "deterministic": True,
        "source_protocol_id": "frozen-source", "new_training_updates": 0, "new_threshold_search": 0}
    return selector, identity


def test_default_off_never_imports_or_loads_ml_and_keeps_original_account(registered_account, monkeypatch):
    import builtins
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "backtest.coin_selector" or name.startswith("research.ml_selection"):
            raise AssertionError("disabled backtest attempted to load ML")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    calls = engine_probe(monkeypatch, registered_account)
    prior = config._config
    target = registered_account.root / "off"
    summary = runner.run_backtest(target, root=registered_account.root)
    assert config._config is prior
    assert calls[0]["parameters"] == registered_account.arm["parameters"]
    options = calls[0]["options"]
    assert options.pop("candidate_selector") is None
    expected_options = deepcopy(registered_account.arm["engine_options"])
    expected_options["trading_start"] = pd.Timestamp(expected_options["trading_start"]).tz_convert(None)
    assert options == expected_options
    assert summary["baseline_validation"]["all_passed"]
    assert summary["account_mode"] == "spot_margin"
    assert summary["net_pnl"] == 0. and summary["final_equity"] == 100000.
    identity = json.loads((target / "coin_selector.json").read_text())
    assert identity["enabled"] is False and identity["model_id"] is None
    assert (target / "metrics.json").is_file()
    assert (target / "closed_trades.csv").is_file()


@pytest.mark.parametrize("pinned_bundle", [None, "queued_model/manifest.json"])
def test_enabled_injects_fixed_policy_without_changing_original_engine(registered_account, monkeypatch, pinned_bundle):
    from backtest import coin_selector
    selector, identity = policy_identity()
    calls = engine_probe(monkeypatch, registered_account)
    factory_calls = []

    def factory(frames, *, initial_capital, bundle_path):
        factory_calls.append((list(frames), initial_capital, bundle_path))
        return selector, identity

    def write_report(directory, observed_selector, observed_identity):
        assert observed_selector is selector
        runner._save(directory / "coin_selector.json", observed_identity)
        return observed_identity

    monkeypatch.setattr(coin_selector, "create_selector", factory)
    monkeypatch.setattr(coin_selector, "write_selector_report", write_report)
    target = registered_account.root / "on"
    summary = runner.run_backtest(target, coin_selector="on", selector_bundle=pinned_bundle,
                                 root=registered_account.root)
    assert factory_calls == [(["A/USDT"], 100000., pinned_bundle)]
    assert calls[0]["options"]["candidate_selector"] is selector
    assert calls[0]["parameters"] == registered_account.arm["parameters"]
    assert calls[0]["options"]["terminal_policy"] == "forced_liquidation"
    assert calls[0]["options"]["warmup_period"] == 30
    assert summary["baseline_validation"]["status"] == "not_applicable"
    observed = json.loads((target / "coin_selector.json").read_text())
    assert observed["model_id"] == "fixed-policy" and observed["policy_threshold"] == .51
    assert observed["training_updates"] == 0 and observed["independent_holdout"] is False


@pytest.mark.parametrize("key,value", [("model_id", "wrong-policy"), ("parent_model_id", "wrong-parent"),
    ("policy_threshold", .5), ("new_training_updates", 1), ("new_threshold_search", 1)])
def test_enabled_rejects_policy_identity_or_budget_mismatch(registered_account, monkeypatch, key, value):
    from backtest import coin_selector
    selector, identity = policy_identity()
    identity[key] = value
    monkeypatch.setattr(coin_selector, "create_selector", lambda *args, **kwargs: (selector, identity))
    target = registered_account.root / "invalid-on"
    with pytest.raises(ValueError, match="identity differs"):
        runner.run_backtest(target, coin_selector="on", root=registered_account.root)
    assert not target.exists()


def test_tampered_registration_rejected_even_when_json_remains_valid(registered_account):
    registration = json.loads(registered_account.smart_path.read_text())
    registration["arms"]["smart"]["parameters"]["account"]["mode"] = "spot"
    runner._save(registered_account.smart_path, registration)
    with pytest.raises(ValueError, match="artifact identity mismatch"):
        runner.load_registered_baseline(registered_account.root)


def test_changed_input_bytes_are_rejected_before_engine(registered_account):
    registered_account.engine_path.write_text(registered_account.engine_path.read_text() + "\n")
    with pytest.raises(ValueError, match="frozen input hash mismatch"):
        runner.load_registered_baseline(registered_account.root)


def test_redeclared_csv_with_wrong_frame_identity_is_rejected(registered_account, monkeypatch):
    frame = registered_account.frames["A/USDT"].copy()
    frame.loc[frame.index[0], "volume"] += 1
    frame.to_csv(registered_account.engine_path, index_label="timestamp")
    for path in (registered_account.data_path, registered_account.smart_path):
        registration = json.loads(path.read_text())
        registration["input_files"]["input/engine/A_USDT.csv"] = sha256_file(registered_account.engine_path)
        runner._save(path, registration)
    monkeypatch.setattr(runner, "REGISTRATION_HASHES", {
        "data": sha256_file(registered_account.data_path), "smart": sha256_file(registered_account.smart_path)})
    with pytest.raises(ValueError, match="engine frame identity mismatch"):
        runner.load_registered_baseline(registered_account.root)


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "load_registered_baseline", lambda *args: pytest.fail("loaded existing output"))
    with pytest.raises(FileExistsError):
        runner.run_backtest(tmp_path)


def test_cli_defaults_off_and_requires_new_output(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "run_backtest", lambda path, **kwargs: calls.append((path, kwargs)) or {})
    assert runner.main(["--output-dir", str(tmp_path / "new")]) == 0
    assert calls == [(str(tmp_path / "new"), {"coin_selector": "off", "selector_bundle": None})]
    with pytest.raises(SystemExit):
        runner.main([])


def test_disabled_bundle_is_rejected_before_inputs_or_ml_loading(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "backtest.coin_selector" or name.startswith("research.ml_selection"):
            raise AssertionError("disabled backtest attempted to load ML")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(runner, "load_registered_baseline", lambda *args: pytest.fail("invalid off loaded inputs"))
    with pytest.raises(ValueError, match="requires coin_selector on"):
        runner.run_backtest(tmp_path / "invalid-off", selector_bundle="missing.json")
    with pytest.raises(ValueError, match="requires coin_selector on"):
        runner._selector({}, 100000., False, "missing.json")
    with pytest.raises(SystemExit):
        runner.main(["--output-dir", str(tmp_path / "invalid-off"), "--selector-bundle", "missing.json"])


def test_cli_passes_the_queued_frozen_bundle(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "run_backtest", lambda path, **kwargs: calls.append((path, kwargs)) or {})
    target, bundle = str(tmp_path / "new"), str(tmp_path / "queued/manifest.json")
    assert runner.main(["--output-dir", target, "--coin-selector", "on", "--selector-bundle", bundle]) == 0
    assert calls == [(target, {"coin_selector": "on", "selector_bundle": bundle})]


def test_config_is_restored_when_engine_fails(registered_account, monkeypatch):
    class BrokenEngine:
        def __init__(self, **kwargs):
            raise ValueError("actual engine failure")

    monkeypatch.setattr(runner, "BacktestEngine", BrokenEngine)
    prior = config._config
    with pytest.raises(ValueError, match="actual engine failure"):
        runner.run_backtest(registered_account.root / "failed", root=registered_account.root)
    assert config._config is prior
