"""Offline research scheduling respects source facts and the unopened final sample."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis.strategy_review import freeze_prospective
from core.reproducibility import save_data_snapshots, sha256_file
from scripts import run_research_automation as worker


@pytest.fixture
def protocol(tmp_path):
    target = tmp_path / "protocol.json"
    freeze_prospective(target, code_hash="registered-original-code",
        config_hash=sha256_file(worker.ROOT / "config/params.yaml"), registry_hash="fixed-registry",
        frozen_at="2026-09-20T00:00:00+00:00")
    return target


def bars(count=240):
    index = pd.date_range("2024-01-01", periods=count, freq="D")
    close = 100. + np.arange(count) * .1
    return pd.DataFrame({"open": close, "high": close + 2, "low": close - 2,
                         "close": close, "volume": 100000.}, index=index)


def local_snapshot(tmp_path, frame=None, *, cache=False):
    frame = bars() if frame is None else frame
    folder = tmp_path / ("cache" if cache else "snapshot")
    folder.mkdir()
    data_root = folder if cache else folder / "data_inputs"
    saved = save_data_snapshots({"BTC/USDT": frame}, data_root)
    meta = {"start": str(frame.index[0]), "end": str(frame.index[-1])}
    if cache:
        manifest = {"schema_version": "binance-cache/v2", "provider": "binance",
                    "market_type": "spot", "timeframe": "1d", "failures": [], "symbols": {
            "BTC/USDT": {"provider": "binance", "market_type": "spot", "timeframe": "1d",
                         "symbol": "BTC-USDT", "coverage": {"status": "complete"},
                         "file": saved["BTC/USDT"]["path"], "sha256": saved["BTC/USDT"]["sha256"],
                         "first": meta["start"], "last": meta["end"]}}}
        target = folder / "_manifest.json"
    else:
        manifest = {"schema_version": "2.0", "data": {"source": "local", "exchange": "binance",
                    "timeframe": "1d", "symbols": {"BTC/USDT": meta}}, "data_snapshots": saved}
        target = folder / "run_manifest.json"
    worker.save(target, manifest)
    return folder, target, data_root / saved["BTC/USDT"]["path"]


def load(folder, count=240):
    return worker.load_local_inputs(folder, ["BTC/USDT"], start=pd.Timestamp("2024-01-01", tz="UTC"),
        end=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=count),
        boundary=pd.Timestamp("2026-10-21", tz="UTC"), timeframe="1d")


def test_protocol_integrity_and_closed_window_before_any_data_read(protocol, tmp_path):
    original = protocol.read_bytes()
    record, copied, start, end = worker.check_protocol(protocol, "2024-01-01", "2026-10-20")
    assert copied == original and end == pd.Timestamp(record["test_start"])
    with pytest.raises(PermissionError, match="entirely before"):
        worker.check_protocol(protocol, "2024-01-01", "2026-10-21")
    # Maturity never relaxes the boundary: dates after maturity still fail.
    with pytest.raises(PermissionError, match="entirely before"):
        worker.check_protocol(protocol, "2027-06-01", "2027-06-30")
    record["test_start"] = "2028-01-01T00:00:00+00:00"
    worker.save(protocol, record)
    with pytest.raises(ValueError, match="hash mismatch"):
        worker.check_protocol(protocol, "2024-01-01", "2024-12-31")
    protocol.write_bytes(original)
    protocol.with_suffix(".json.opened").write_text("exclusive-receipt", encoding="utf-8")
    with pytest.raises(PermissionError, match="closed prospective"):
        worker.check_protocol(protocol, "2024-01-01", "2024-12-31")


@pytest.mark.parametrize("value", [[], [[20, 10], [20, 10]], [[10, 20]], [[20, True]],
                                      [[366, 10]], [[20.0, 10]], [[20, 1]], [[20, 10, 5]]])
def test_reject_unbounded_or_invalid_candidate_set(value):
    with pytest.raises(ValueError):
        worker.parse_candidates(value)


@pytest.mark.parametrize("cache", [False, True])
def test_verified_local_snapshot_and_cache_are_consumed_without_derived_indicators(tmp_path, cache):
    frame = bars()
    frame["HIGH_MAX_20"] = -1.  # A cached predictor cannot bypass causal recalculation.
    folder, manifest, data = local_snapshot(tmp_path, frame, cache=cache)
    frames, identity = load(folder)
    assert frames["BTC/USDT"].index.equals(frame.index)
    assert list(frames["BTC/USDT"].columns) == list(worker.RAW_COLUMNS[:5])
    assert identity["manifest_sha256"] == sha256_file(manifest)
    assert identity["files"]["BTC/USDT"]["sha256"] == sha256_file(data)
    if not cache:
        assert load(folder / "data_inputs")[0]["BTC/USDT"].equals(frames["BTC/USDT"])
    data.write_bytes(data.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load(folder)


def test_input_traversal_and_final_sample_metadata_are_rejected(tmp_path, monkeypatch):
    folder, manifest_path, data = local_snapshot(tmp_path)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["data_snapshots"]["BTC/USDT"]["path"] = "../../outside.csv"
    worker.save(manifest_path, manifest)
    with pytest.raises(ValueError, match="escapes"):
        load(folder)
    manifest["data_snapshots"]["BTC/USDT"]["path"] = data.name
    manifest["data"]["symbols"]["BTC/USDT"]["end"] = "2026-10-21"
    worker.save(manifest_path, manifest)
    monkeypatch.setattr(worker.pd, "read_csv", lambda *a, **k: pytest.fail("protected data read"))
    with pytest.raises(PermissionError, match="final sample"):
        load(folder)


@pytest.mark.parametrize("mutation", ["missing_bar", "duplicate", "unordered", "invalid_ohlc"])
def test_malformed_or_incomplete_ohlcv_is_explicit(tmp_path, mutation):
    frame = bars()
    if mutation == "missing_bar":
        frame = frame.drop(frame.index[8])
    elif mutation == "duplicate":
        frame = pd.concat([frame.iloc[:10], frame.iloc[9:]])
    elif mutation == "unordered":
        frame = frame.iloc[::-1]
    else:
        frame.loc[frame.index[10], "low"] = 10000.
    # Snapshot writer sorts rows; test unordered data at the validator boundary.
    if mutation == "unordered":
        with pytest.raises(ValueError, match="unordered"):
            worker._validate_frame(frame, symbol="BTC/USDT", start=pd.Timestamp("2024-01-01", tz="UTC"),
                end=pd.Timestamp("2024-08-28", tz="UTC"), boundary=pd.Timestamp("2026-10-21", tz="UTC"),
                timeframe="1d")
    else:
        folder, _, _ = local_snapshot(tmp_path, frame)
        with pytest.raises(ValueError):
            load(folder)


def fake_partition(frames, pair, *, name, trading_start, output, **kwargs):
    assert all(frame.index.max() < pd.Timestamp("2026-10-21") for frame in frames.values())
    index = next(iter(frames.values())).index
    index = index[index >= trading_start]
    # The two rankings deliberately reverse between training and validation.
    value = {(20, "train"): .002, (30, "train"): .001,
             (20, "validation"): -.001, (30, "validation"): .003}[pair[0], name]
    series = pd.Series(value, index=index)
    return series, {"return_bars": len(series), "mean_return": value,
                     "fills": 2, "counted_health_cohorts": 2}


def test_monthly_selection_uses_training_only_and_records_true_partition_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_partition", fake_partition)
    output = worker.research_diagnostics({"BTC/USDT": bars()}, ((20, 10), (30, 10)),
        task="monthly-optimize", timeframe="1d", capital_levels=[10000.], output=tmp_path)
    assert output["diagnostics"]["selected_candidate"] == "entry=20,exit=10"
    assert output["diagnostics"]["validation_mean_return"] < 0
    assert output["partitions"]["train_end_inclusive"] < output["partitions"]["validation_start"]
    assert output["diagnostics"]["validation_sample_size"] == 72
    assert output["diagnostics"]["multiple_testing"]["sample_size"] == 2


def test_quarterly_uses_all_fixed_candidates_and_official_capacity_without_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_partition", fake_partition)
    captured = []

    def capacity(frames, *, capital_levels, engine_kwargs):
        captured.append((deepcopy(frames), capital_levels, engine_kwargs))
        return {"capital_levels": capital_levels, "points": []}

    monkeypatch.setattr(worker, "run_capacity_curve", capacity)
    output = worker.research_diagnostics({"BTC/USDT": bars()}, ((20, 10), (30, 10)),
        task="quarterly-robust", timeframe="1d", capital_levels=[10000., 100000.], output=tmp_path)
    assert output["diagnostics"]["selected_candidate"] is None
    assert list(output["diagnostics"]["fixed_candidate_diagnostics"]) == ["entry=20,exit=10", "entry=30,exit=10"]
    assert len(captured) == 1
    assert captured[0][2]["trading_start"] == output["partitions"]["validation_start"]
    assert "strategies" not in captured[0][2]


def run_kwargs(tmp_path, protocol):
    return {"task": "monthly-optimize", "protocol": protocol, "data_dir": tmp_path / "unused",
            "start": "2024-01-01", "end": "2024-08-27", "symbols": ["BTC/USDT"],
            "output": tmp_path / "output", "synthetic": True, "candidates": [[20, 10]]}


@pytest.mark.parametrize("task", worker.TASKS)
def test_real_engine_synthetic_runs_leave_protocol_and_configuration_closed(tmp_path, protocol, task, monkeypatch):
    # Other agents may edit unrelated repository files while this focused test
    # runs. Bind the worker/config source here; full source coverage is tested
    # by roadmap_baseline's existing suite and used unmodified by the CLI.
    files = {"scripts/run_research_automation.py": sha256_file(Path(worker.__file__)),
             "config/params.yaml": sha256_file(worker.ROOT / "config/params.yaml")}
    monkeypatch.setattr(worker, "source_manifest", lambda root: dict(files))
    monkeypatch.setattr(worker.DataFetcher, "fetch_ccxt", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(worker.DataFetcher, "fetch_yahoo", lambda *a, **k: pytest.fail("network"))
    original_protocol, original_config = protocol.read_bytes(), deepcopy(worker.config._config)
    kwargs = run_kwargs(tmp_path, protocol)
    kwargs.update(task=task, capital_levels=[10000., 100000.])
    result = worker.run_research_automation(**kwargs)
    assert result["engineering_status"] == "pass", result
    assert result["research_status"] == "synthetic_only"
    assert result["production_evidence"] is False and result["holdout_opened"] is False
    assert protocol.read_bytes() == original_protocol and worker.config._config == original_config
    assert not protocol.with_suffix(".json.opened").exists()
    assert result["candidates"]["entry=20,exit=10"]["train"]["return_bars"] >= 30
    facts = result["candidates"]["entry=20,exit=10"]
    assert facts["train"]["fills"] + facts["validation"]["fills"] > 0
    if task == "quarterly-robust":
        assert len(result["diagnostics"]["capacity"]["result"]["points"]) == 2
    identity = json.loads((kwargs["output"] / "run_manifest.json").read_bytes())["identity"]
    assert identity["source_matches_registered_candidate"] is False
    for relative, expected in result["artifacts"].items():
        assert sha256_file(kwargs["output"] / relative) == expected


def test_missing_inputs_and_insufficient_samples_are_not_engineering_success_evidence(tmp_path, protocol):
    kwargs = run_kwargs(tmp_path, protocol)
    kwargs["synthetic"] = False
    missing = worker.run_research_automation(**kwargs)
    assert missing["engineering_status"] == "blocked_missing_inputs"
    assert missing["research_status"] == "insufficient"
    kwargs.update(output=tmp_path / "short", synthetic=True, end="2024-01-31")
    short = worker.run_research_automation(**kwargs)
    assert short["engineering_status"] == "pass"
    assert short["research_status"] == "synthetic_only"
    assert short["underlying_research_status"] == "insufficient"
    assert "candidates" not in short


def test_output_directory_cannot_be_reused_or_overwritten(tmp_path, protocol):
    kwargs = run_kwargs(tmp_path, protocol)
    kwargs["output"].mkdir()
    sentinel = kwargs["output"] / "research_report.json"
    sentinel.write_text("prior immutable report", encoding="utf-8")
    with pytest.raises(FileExistsError):
        worker.run_research_automation(**kwargs)
    assert sentinel.read_text(encoding="utf-8") == "prior immutable report"


def test_changed_identity_during_execution_fails_closed(tmp_path, protocol, monkeypatch):
    def tamper(*args, **kwargs):
        protocol.write_bytes(protocol.read_bytes() + b"\n")
        return {"minimum_validation_health_cohorts": 40}

    monkeypatch.setattr(worker, "research_diagnostics", tamper)
    result = worker.run_research_automation(**run_kwargs(tmp_path, protocol))
    assert result["engineering_status"] == "fail"
    assert result["research_status"] == "not_evaluated"
    assert "changed during execution" in result["reason"]


def test_engine_exception_has_failed_receipt_instead_of_research_pass(tmp_path, protocol, monkeypatch):
    def failed_engine(*args, **kwargs):
        raise RuntimeError("injected execution failure")

    monkeypatch.setattr(worker, "research_diagnostics", failed_engine)
    report = worker.run_research_automation(**run_kwargs(tmp_path, protocol))
    assert report["engineering_status"] == "fail"
    assert report["research_status"] == "not_evaluated"
    assert report["error_type"] == "RuntimeError"


def test_synthetic_generator_is_deterministic_without_changing_global_rng():
    left, right = pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-08-28", tz="UTC")
    original = np.random.get_state()
    first = worker.synthetic_inputs(["BTC/USDT"], left, right, "1d")
    second = worker.synthetic_inputs(["BTC/USDT"], left, right, "1d")
    assert first["BTC/USDT"].equals(second["BTC/USDT"])
    after = np.random.get_state()
    assert original[0] == after[0] and np.array_equal(original[1], after[1])
    assert original[2:] == after[2:]


def test_cli_reports_guard_failure_with_nonzero_exit(tmp_path, protocol, capsys):
    code = worker.main(["--task", "monthly-optimize", "--protocol", str(protocol),
        "--data-dir", str(tmp_path / "unused"), "--start", "2026-10-21", "--end", "2026-10-22",
        "--symbols", "BTC/USDT", "--output", str(tmp_path / "failed"), "--synthetic"])
    assert code == 2
    assert '"engineering_status":"fail"' in capsys.readouterr().out
    assert json.loads((tmp_path / "failed/research_report.json").read_bytes())["holdout_opened"] is False
