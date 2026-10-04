"""Financing inputs are sealed with reports and restored without mutable heads."""
import json
from types import SimpleNamespace

import pytest

from backtest.engine import BacktestEngine
from core.signal_observation_types import fingerprint
from core.temporal_financing import TemporalFinancing
from main import _restore_temporal_financing, _temporal_report_payload, sha256_file
from tests.test_temporal_financing import funding_records


def test_engine_accepts_sealed_financing_mapping_path_and_provider(tmp_path):
    book = TemporalFinancing("fixture:financing", funding_records())
    path = tmp_path / "financing.json"
    path.write_text(json.dumps(book.export()), encoding="utf-8")
    for input_value in (book, book.export(), path):
        engine = BacktestEngine(temporal_financing=input_value)
        assert engine.temporal_financing.export() == book.export()
    with pytest.raises(ValueError):
        BacktestEngine(temporal_financing={"schema": "unsupported"})


def test_report_freezes_financing_and_replay_checks_both_hashes(tmp_path):
    book = TemporalFinancing("fixture:financing", funding_records())
    engine = SimpleNamespace(temporal_financing=book, temporal_policy=None)
    payload = _temporal_report_payload(engine, {}, {})
    assert payload["financing_evidence"] == book.export()
    path = tmp_path / "temporal_data.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    execution = {"temporal_data_sha256": sha256_file(path),
        "temporal_financing_sha256": fingerprint(book.export())}
    assert _restore_temporal_financing(execution, tmp_path) == book.export()
    payload["financing_evidence"]["records"][1]["data"]["funding_rate"] = .2
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="report hash"):
        _restore_temporal_financing(execution, tmp_path)
    execution["temporal_data_sha256"] = sha256_file(path)
    with pytest.raises(ValueError, match="evidence"):
        _restore_temporal_financing(execution, tmp_path)
    path.unlink()
    with pytest.raises(OSError):
        _restore_temporal_financing(execution, tmp_path)
    assert _restore_temporal_financing({}, tmp_path) is None
