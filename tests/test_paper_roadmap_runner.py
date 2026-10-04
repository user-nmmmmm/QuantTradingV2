from dataclasses import asdict, replace
import json

import pandas as pd
import pytest

from analysis.paper_portfolio import PaperSpec, default_paper_specs
from core.reproducibility import sha256_file
from scripts.run_paper_roadmap import build_jobs, freeze_inputs, load_fine_input, validate_frozen_observations


def test_job_registration_retains_fixed_family_and_matching_cost_control():
    specs = default_paper_specs()
    aware = next(s for s in specs if s.name == "trend_60_cost_aware")
    control = replace(aware, name="trend_60_optimizer_no_cost", cost_penalty_scale=0.)
    specs += [control] + [PaperSpec(name=name, signal="cash", rebalance_every=1)
        for name in ("label_fixed_ev", "label_barrier_ev")]
    jobs = build_jobs(specs)
    assert len(jobs) == 110 and len({j["id"] for j in jobs}) == 110
    assert sum(j["purpose"] == "comparison" for j in jobs) == 102
    assert set(asdict(aware).items()) - set(asdict(control).items()) == {
        ("name", aware.name), ("cost_penalty_scale", 1.)}
    assert all(j["cost_multiplier"] in (1., 1.5) for j in jobs)


def source_fixture(tmp_path):
    index = pd.date_range("2020-01-01", periods=5)
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.}, index=index)
    manifest = {"market_type": "spot", "timeframe": "1d", "exchange": "binance", "symbols": {}}
    for symbol in ("BTC/USDT", "ETH/USDT"):
        path = tmp_path / (symbol.replace("/", "_") + ".csv")
        frame.to_csv(path, index_label="timestamp")
        manifest["symbols"][symbol] = {"file": path.name, "sha256": sha256_file(path),
            "rows": len(frame), "first": index[0].isoformat(), "last": index[-1].isoformat()}
    path = tmp_path / "_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    observations = tmp_path / "observation.json"
    observations.write_text('{"candidates": [], "outcomes": []}', encoding="utf-8")
    return path, observations


def test_frozen_inputs_preserve_unknown_availability_and_old_bytes(tmp_path):
    manifest, observations = source_fixture(tmp_path)
    output = tmp_path / "study"
    frames, _, snapshots, store = freeze_inputs(manifest, observations, output, "2026-10-03T00:00:00Z")
    assert snapshots["temporal"]["availability"]["unknown"] == 10
    assert snapshots["historically_available_rows"] == 0
    assert not snapshots["point_in_time_complete"]
    source = tmp_path / "BTC_USDT.csv"
    original = source.read_bytes()
    source.write_text("changed source", encoding="utf-8")
    assert store.read_file(snapshots["raw"]["snapshot_id"], source.name) == original
    assert frames["BTC/USDT"].close.tolist() == [100.] * 5
    with pytest.raises(ValueError, match="hash mismatch"):
        freeze_inputs(manifest, observations, tmp_path / "another", "2026-10-03T00:00:00Z")


def test_fine_manifest_does_not_infer_availability(tmp_path):
    path, _ = source_fixture(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["timeframe"] = "1h"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit complete available_at"):
        load_fine_input(path, tmp_path / "fine", "2026-10-03T00:00:00Z")


def test_old_fixed_outcomes_must_match_new_frozen_prices():
    index = pd.date_range("2020-01-02", periods=5, tz="UTC")
    frames = {"BTC/USDT": pd.DataFrame({"open": 100., "close": 102.}, index=index)}
    candidate = {"candidate_id": "c", "symbol": "BTC/USDT", "direction": "long",
        "timestamp": "2020-01-01T00:00:00Z", "context": {"available_at": "2020-01-02T00:00:00Z"}}
    outcome = {"candidate_id": "c", "symbol": "BTC/USDT", "direction": "long", "horizon_bars": 5,
        "entry_time": index[0].isoformat(), "exit_bar": index[-1].isoformat(),
        "available_at": "2020-01-07T00:00:00Z", "status": "matured", "entry_reference": 100., "exit_reference": 102.}
    inputs = {"candidates": [candidate], "outcomes": [outcome]}
    assert validate_frozen_observations(frames, inputs)["mature_fixed_labels_price_checked"] == 1
    outcome["exit_reference"] = 103.
    with pytest.raises(ValueError, match="prices differ"):
        validate_frozen_observations(frames, inputs)
