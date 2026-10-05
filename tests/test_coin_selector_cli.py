"""Opt-in serving, frozen replay, and isolation of the original backtest."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import main as entrypoint
from backtest.coin_selector import (
    create_selector, disabled_identity, read_bundle, restore_selector,
    snapshot_bundle, write_selector_report,
)
from core.reproducibility import deterministic_result_digest, sha256_file
from tests.engine_baseline_harness import build_synthetic_data_map


def candles():
    frames = build_synthetic_data_map(symbols=("BTC/USDT", "ETH/USDT", "SOL/USDT"), bars=140)
    for frame in frames.values():
        frame["quote_volume"] = frame.volume * frame.close
    return frames


@pytest.fixture
def frozen_ml_runtime():
    pytest.importorskip("lightgbm", reason="Frozen ML serving requires the optional research dependencies")


@pytest.mark.parametrize("flags", [[], ["--coin-selector", "off"]])
def test_disabled_cli_never_loads_models_and_keeps_engine_options(monkeypatch, tmp_path, flags):
    import backtest.coin_selector as serving
    load = MagicMock(side_effect=AssertionError("Disabled backtests must not load ML"))
    monkeypatch.setattr(serving, "create_selector", load)
    factory = MagicMock()
    monkeypatch.setattr(entrypoint, "BacktestEngine", factory)
    monkeypatch.chdir(tmp_path)
    args = entrypoint._build_parser().parse_args(flags)
    engine, _, _ = entrypoint._execute_backtest(args, {})
    load.assert_not_called()
    assert factory.call_args.kwargs["candidate_selector"] is None
    assert factory.call_args.kwargs["capital_allocation"] is None
    assert engine.coin_selector_identity == disabled_identity()


def test_enabled_cli_only_adds_selector_without_replacing_risk_or_allocation(monkeypatch, tmp_path):
    import backtest.coin_selector as serving
    selector = object()
    identity = {"enabled": True, "model_id": "frozen"}
    load = MagicMock(return_value=(selector, identity))
    monkeypatch.setattr(serving, "create_selector", load)
    factory = MagicMock()
    monkeypatch.setattr(entrypoint, "BacktestEngine", factory)
    monkeypatch.chdir(tmp_path)
    args = entrypoint._build_parser().parse_args([
        "--coin-selector", "on", "--capital", "100000", "--smart-allocation"])
    engine, _, _ = entrypoint._execute_backtest(args, {})
    load.assert_called_once_with({}, initial_capital=100000.0, bundle_path=None,
                                 account_mode="spot_margin")
    assert factory.call_args.kwargs["candidate_selector"] is selector
    assert factory.call_args.kwargs["capital_allocation"]["enabled"] is True
    assert engine.coin_selector_identity == identity


def test_explicit_off_replays_the_original_economic_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    frames = candles()
    parser = entrypoint._build_parser()
    ordinary = parser.parse_args(["--smart-allocation", "--disable-routing-log"])
    off = parser.parse_args(["--smart-allocation", "--coin-selector", "off", "--disable-routing-log"])
    _, expected, _ = entrypoint._execute_backtest(ordinary, frames)
    _, observed, _ = entrypoint._execute_backtest(off, frames)
    assert deterministic_result_digest(observed) == deterministic_result_digest(expected)
    pd.testing.assert_frame_equal(observed["equity_curve"], expected["equity_curve"])
    assert observed["accounting_check"]["ok"]


def test_frozen_serving_uses_current_weights_and_never_exposes_future_labels(frozen_ml_runtime):
    selector, identity = create_selector(candles(), initial_capital=100000)
    assert identity["model_id"] == "9db8c77c77e58997798c005b016f34a07bfb85a3fb7d77eacdce8f25bb8b851f"
    assert identity["parent_model_id"] == "1e7f2d876452069b06dc2dc0702dfcc430c35c643d1b44688ccb98c7773cdb90"
    assert selector.policy_threshold == identity["policy_threshold"] == .51
    assert selector.deterministic
    assert not any(column.startswith("label_") for column in selector.table)
    assert identity["new_training_updates"] == identity["new_threshold_search"] == 0
    assert identity["eligible_rows"] > 0


def test_future_candle_changes_cannot_modify_earlier_serving_features(frozen_ml_runtime):
    frames = candles()
    first, _ = create_selector(frames, initial_capital=100000)
    changed = deepcopy(frames)
    for frame in changed.values():
        frame.iloc[-1, frame.columns.get_loc("high")] *= 1.5
        frame.iloc[-1, frame.columns.get_loc("quote_volume")] *= 2
    second, _ = create_selector(changed, initial_capital=100000)
    cutoff = pd.Timestamp(next(iter(frames.values())).index[-1], tz="UTC")
    a = first.table.loc[first.table.index.get_level_values("as_of") <= cutoff]
    b = second.table.loc[second.table.index.get_level_values("as_of") <= cutoff]
    pd.testing.assert_frame_equal(a, b)


def test_saved_package_reconstructs_same_serving_and_missing_weights_fail(tmp_path, frozen_ml_runtime):
    frames = candles()
    selector, identity = create_selector(frames, initial_capital=100000)
    report = write_selector_report(tmp_path, selector, identity)
    assert (tmp_path / "coin_selection.csv").is_file()
    restored = restore_selector({"coin_selector": report, "capital": 100000}, frames, tmp_path)
    pd.testing.assert_frame_equal(restored.table, selector.table)
    np.testing.assert_array_equal(restored.policy.weights, selector.policy.weights)
    assert restored.policy_threshold == selector.policy_threshold
    assert restore_selector({"capital": 100000}, frames, tmp_path) is None
    path, package = read_bundle(tmp_path / report["snapshot_manifest"])
    weight = path.parent / package["files"]["policy"]["path"]
    weight.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        restore_selector({"coin_selector": report, "capital": 100000}, frames, tmp_path)


def test_enabled_manifest_cannot_silently_fall_back_to_original(tmp_path):
    with pytest.raises(KeyError, match="snapshot_manifest"):
        restore_selector({"coin_selector": {"enabled": True}, "capital": 100000}, {}, tmp_path)


@pytest.mark.parametrize("flags", [
    ["--selector-bundle", "missing.json"],
    ["--coin-selector", "on", "--timeframe", "1h"],
    ["--coin-selector", "on", "--selector-bundle", "missing.json"],
])
def test_invalid_selector_configuration_fails_before_data_loading(monkeypatch, flags):
    load = MagicMock(side_effect=AssertionError("Input validation must precede loading"))
    monkeypatch.setattr(entrypoint, "_load_requested_data", load)
    assert entrypoint.main(flags) == 2
    load.assert_not_called()


def test_package_snapshot_keeps_byte_identity(tmp_path, frozen_ml_runtime):
    _, identity = create_selector(candles(), initial_capital=100000)
    path = snapshot_bundle(identity, tmp_path / "inputs")
    assert sha256_file(path) == identity["bundle_sha256"]
    _, package = read_bundle(path)
    assert package["candidate"]["model_id"] == identity["model_id"]


def test_disabled_report_is_explicit_and_has_no_model_snapshot(tmp_path):
    result = write_selector_report(tmp_path, None, disabled_identity())
    assert result["enabled"] is False
    assert not (tmp_path / "selector_inputs").exists()
    assert json.loads((tmp_path / "coin_selector.json").read_text())["enabled"] is False


@pytest.mark.parametrize("profile", ["workbook", "compact", "full"])
def test_enabled_main_reports_and_full_replay_use_the_frozen_snapshot(
        monkeypatch, tmp_path, profile, frozen_ml_runtime):
    """Run real serving/engine; only expensive presentation rendering is stubbed."""
    frames = candles()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entrypoint, "_load_requested_data", lambda *args: (frames, {}, now, now))
    metadata = []
    monkeypatch.setattr(entrypoint.ReportGenerator, "generate",
        lambda *args, **kwargs: metadata.append(kwargs["metadata"]) or {})
    monkeypatch.setattr(entrypoint, "format_primary_metrics", lambda *args, **kwargs: "test metrics")
    output = tmp_path / "run"
    assert entrypoint.main([
        "--start", "2024-01-01", "--end", "2024-05-19",
        "--symbols", "BTC/USDT", "ETH/USDT", "SOL/USDT",
        "--coin-selector", "on", "--smart-allocation", "--disable-routing-log",
        "--report-profile", profile, "--output-dir", str(output)]) == 0
    identity = json.loads((output / "coin_selector.json").read_text())
    assert identity["enabled"] is True
    assert identity["scored_candidates"] > 0
    assert metadata[0]["CoinSelector"] == "on"
    assert metadata[0]["CoinSelectorModelId"] == identity["model_id"]
    assert sha256_file(output / identity["snapshot_manifest"]) == identity["bundle_sha256"]
    assert sha256_file(output / "coin_selection.csv") == identity["selection_audit_sha256"]
    if profile == "full":
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["execution"]["coin_selector"] == identity
        assert "coin_selection.csv" in manifest["artifacts"]
        import backtest.coin_selector as serving
        monkeypatch.setattr(serving, "DEFAULT_BUNDLE", tmp_path / "unavailable-current-alias.json")
        assert entrypoint.replay_manifest(str(output / "run_manifest.json")) == 0
