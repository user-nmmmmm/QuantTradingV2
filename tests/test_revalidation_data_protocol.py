"""Strict coverage and timestamp gates for the new reproducible research runner."""
import json

import pandas as pd
import pytest

from core.reproducibility import sha256_file
from scripts import run_revalidation60 as runner


def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "SYMBOLS", ["BTC/USDT", "ETH/USDT"])
    folder = tmp_path / "data_inputs"
    folder.mkdir()
    inventory = {"complete": True, "symbols": {}}
    for symbol in runner.SYMBOLS:
        path = folder / (symbol.replace("/", "_") + ".csv")
        pd.DataFrame({"open": [100, 101], "high": [102, 103], "low": [99, 100],
                      "close": [101, 102], "volume": [1000, 1200]},
                     index=pd.date_range("2026-06-29", periods=2, name="timestamp")).to_csv(path)
        inventory["symbols"][symbol] = {"file": path.name, "sha256": sha256_file(path), "truncated_before_end": False}
    (tmp_path / "data_manifest.json").write_text(json.dumps(inventory))
    return inventory


def test_end_date_includes_june30_and_does_not_invent_prelisting_data(tmp_path, monkeypatch):
    inputs(tmp_path, monkeypatch)
    frames, _, _ = runner.load_inputs(tmp_path)
    assert frames["BTC/USDT"].index[-1] == pd.Timestamp("2026-06-30")
    assert frames["BTC/USDT"].index[0] == pd.Timestamp("2026-06-29")


def test_missing_coin_and_hash_mismatch_fail_closed(tmp_path, monkeypatch):
    inventory = inputs(tmp_path, monkeypatch)
    inventory["symbols"].pop("ETH/USDT")
    (tmp_path / "data_manifest.json").write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="60/60"):
        runner.load_inputs(tmp_path)


def test_nonfinite_prices_cannot_be_blessed_by_a_matching_hash(tmp_path, monkeypatch):
    inventory = inputs(tmp_path, monkeypatch)
    path = tmp_path / "data_inputs/BTC_USDT.csv"
    path.write_text(path.read_text().replace(",102,", ",inf,", 1))
    inventory["symbols"]["BTC/USDT"]["sha256"] = sha256_file(path)
    (tmp_path / "data_manifest.json").write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="Invalid"):
        runner.load_inputs(tmp_path)


def test_engine_serialized_close_events_are_supported():
    row = {"opening_strategy_id": "TrendBreakout", "timestamp": "2026-06-30", "exit_reason": "signal",
           "risk_action_id": None, "realized_pnl": 2.0}
    assert runner.event_row(row) == row
    assert runner.concentration({"close_events": [row]})["cohort_count"] == 1
