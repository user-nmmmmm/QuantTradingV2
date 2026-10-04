"""Frozen-input integrity and real matching-book account-state regressions."""

from dataclasses import dataclass
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from core.broker import Order, OrderType
from core.domain import OrderStatus
from core.reproducibility import canonical_frame_csv, sha256_file, sha256_frame
from research.ml_selection import protocol
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.selector import ResearchSelector


def configured():
    return {"schema": "ml-selection-research/v1", "timeframe": "1d", "account_mode": "spot",
            "evaluation_kind": "retrospective", "data_registration": "data.json",
            "baseline_registration": "baseline.json", "start": "2024-01-01", "end": "2024-07-01",
            "splits": {"train_end": "2024-03-01", "validation_end": "2024-05-01"},
            "models": ["ridge"], "rl": {"enabled": False}}


def frozen_inputs(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("original_source = 1\n", encoding="utf-8")
    monkeypatch.setattr(protocol, "ROOT", tmp_path)
    monkeypatch.setattr(protocol, "source_identity", lambda: {
        path.name: sha256_file(path) for path in tmp_path.glob("*.py")})
    frame = pd.DataFrame({"open": [100., 101., 102.], "high": [102., 103., 104.],
                          "low": [99., 100., 101.], "close": [101., 102., 103.],
                          "volume": [1000., 1000., 1000.]},
                         index=pd.date_range("2024-01-01", periods=3))
    relative = "input/engine/A_USDT.csv"
    input_path = tmp_path / relative
    input_path.parent.mkdir(parents=True)
    input_path.write_text(canonical_frame_csv(frame), encoding="utf-8")
    registration_path, baseline_path = tmp_path / "data.json", tmp_path / "baseline.json"
    registration = {"symbols": ["A/USDT"], "input_files": {relative: sha256_file(input_path)},
                    "engine_frame_hashes": {"A/USDT": sha256_frame(frame)}}
    protocol.save_json(registration_path, registration)
    protocol.save_json(baseline_path, {"arms": {"smart": {
        "parameters": {"account": {"mode": "spot"}, "execution": {"commission_rate_taker": .001}},
        "engine_options": {"initial_capital": 1000.0, "warmup_period": 0}}}})
    settings = configured()
    settings.update(data_registration=str(registration_path), baseline_registration=str(baseline_path))
    _, parameters, options, evidence = protocol.load_inputs(settings)
    folder = tmp_path / "run"
    frozen = protocol.freeze_protocol(folder, settings, parameters, options, evidence)
    return folder, frozen, settings, frame, input_path, registration_path, baseline_path


def test_current_registration_cannot_replace_original_frozen_data_consistently(tmp_path, monkeypatch):
    folder, frozen, settings, frame, input_path, registration_path, _ = frozen_inputs(tmp_path, monkeypatch)
    assert protocol.validate_run(folder)["protocol_id"] == frozen["protocol_id"]
    # A rewritten registration is internally consistent but belongs to a NEW experiment.
    changed = frame.copy()
    changed["volume"] *= 2
    input_path.write_text(canonical_frame_csv(changed), encoding="utf-8")
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    registration["input_files"]["input/engine/A_USDT.csv"] = sha256_file(input_path)
    registration["engine_frame_hashes"]["A/USDT"] = sha256_frame(changed)
    protocol.save_json(registration_path, registration)
    assert protocol.load_inputs(settings)[0]["A/USDT"].volume.iloc[0] == 2000.0
    with pytest.raises(ValueError, match="frozen registration changed"):
        protocol.validate_run(folder)


def test_frozen_input_bytes_and_baseline_are_checked_on_resume(tmp_path, monkeypatch):
    folder, _, _, _, input_path, _, baseline_path = frozen_inputs(tmp_path, monkeypatch)
    original = input_path.read_bytes()
    input_path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="frozen input hash mismatch"):
        protocol.validate_run(folder)
    input_path.write_bytes(original)
    protocol.validate_run(folder)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["arms"]["smart"]["engine_options"]["initial_capital"] = 2000
    protocol.save_json(baseline_path, baseline)
    with pytest.raises(ValueError, match="frozen registration changed: baseline_registration"):
        protocol.validate_run(folder)


def test_new_source_file_invalidates_frozen_inventory(tmp_path, monkeypatch):
    folder, *_ = frozen_inputs(tmp_path, monkeypatch)
    (tmp_path / "new_runtime.py").write_text("new_source = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source file inventory"):
        protocol.validate_run(folder)


@pytest.mark.parametrize("section,value,match", [
    ("rl", {"gamma": 1.1}, "rl.gamma"),
    ("rl", {"episodes": True}, "rl.episodes"),
    ("rl", {"validation_patience": 0}, "validation_patience"),
    ("rl", {"learning_rate": float("nan")}, "finite"),
    ("selection", {"score_scale": float("inf")}, "finite"),
    ("stress", {"cost_multipliers": [0.5]}, "minimum"),
    ("gates", {"max_drawdown": 1.5}, "maximum"),
    ("dataset", {"horizon_bars": 0}, "integer"),
    ("dataset", {"commission_rate": 1}, "100%"),
    ("rl", {"unknown_control": 1}, "unknown rl"),
])
def test_invalid_training_controls_rejected_before_freeze(tmp_path, section, value, match):
    settings = configured()
    settings[section] = value
    path = tmp_path / "configuration.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        protocol.load_settings(path)


def test_explicit_timezone_boundaries_are_supported():
    settings = configured()
    settings["start"] = "2024-01-01T00:00:00Z"
    settings["end"] = "2024-07-01T00:00:00+00:00"
    assert protocol.validate_settings(settings) is settings


def test_registered_duplicate_timestamps_are_rejected_even_with_matching_hashes(tmp_path, monkeypatch):
    _, _, settings, frame, input_path, registration_path, _ = frozen_inputs(tmp_path, monkeypatch)
    duplicate = pd.concat([frame.iloc[:1], frame])
    input_path.write_text(canonical_frame_csv(duplicate), encoding="utf-8")
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    registration["input_files"]["input/engine/A_USDT.csv"] = sha256_file(input_path)
    registration["engine_frame_hashes"]["A/USDT"] = sha256_frame(duplicate)
    protocol.save_json(registration_path, registration)
    with pytest.raises(ValueError, match="unique ordered midnight"):
        protocol.load_inputs(settings)


@dataclass
class Candidate:
    symbol: str = "A/USDT"
    score: float = 1.0
    strategy_name: str = "OriginalStrategy"


class CaptureModel:
    def __init__(self):
        self.inputs = []

    def predict(self, frame):
        self.inputs.append(frame.copy())
        return np.full(len(frame), .02)


def selector_context(*, initial_capital=100.0):
    dates = pd.date_range("2024-01-02", periods=3, tz="UTC")
    dataset = pd.DataFrame({"symbol": "A/USDT", "as_of": dates,
                            "eligible": True, "exclusion_reason": "",
                            **{name: 0.0 for name in FEATURE_COLUMNS}})
    model = CaptureModel()
    selector = ResearchSelector(dataset, model=model, initial_capital=initial_capital)
    state = {"equity": initial_capital}
    portfolio = SimpleNamespace(cash=initial_capital, positions={}, initial_capital=initial_capital,
                                get_total_value=lambda prices: state["equity"])
    context = {"event": SimpleNamespace(timestamp=pd.Timestamp("2024-01-01"), timeframe="1d"),
               "portfolio": portfolio, "broker": SimpleNamespace(pending_orders=[], active_orders=[]),
               "risk_manager": SimpleNamespace(), "current_prices": {"A/USDT": 100.0}}
    return selector, model, state, context


def test_drawdown_keeps_peak_when_no_entry_candidates_exist():
    selector, model, state, context = selector_context()
    state["equity"] = 120.0
    assert selector.select([], **context) == []
    assert selector.high_water == 120.0
    state["equity"] = 90.0
    context["event"].timestamp = pd.Timestamp("2024-01-02")
    selector.select([Candidate()], **context)
    assert model.inputs[-1]["portfolio_drawdown"].iloc[0] == pytest.approx(.25)


def test_drawdown_starts_from_initial_capital_and_uses_authoritative_risk_state():
    selector, model, state, context = selector_context()
    state["equity"] = 90.0
    selector.select([Candidate()], **context)
    assert model.inputs[-1]["portfolio_drawdown"].iloc[0] == pytest.approx(.10)
    context["risk_manager"] = SimpleNamespace(high_water_equity=150.0, last_drawdown=.40)
    selector.select([Candidate()], **context)
    assert selector.high_water == 150.0
    assert model.inputs[-1]["portfolio_drawdown"].iloc[0] == pytest.approx(.40)
    # Approved recovery can rebase risk peak; the selector follows that state.
    context["risk_manager"] = SimpleNamespace(high_water_equity=100.0, last_drawdown=.10)
    selector.select([Candidate()], **context)
    assert selector.high_water == 100.0
    assert model.inputs[-1]["portfolio_drawdown"].iloc[0] == pytest.approx(.10)


def test_pending_state_uses_unique_outstanding_real_book_orders_and_partial_fills():
    selector, model, _, context = selector_context()
    pending = Order("A/USDT", "buy", 3.0, id="pending", remaining_qty=3.0,
                    status=OrderStatus.ACCEPTED)
    partial = Order("A/USDT", "buy", 3.0, OrderType.LIMIT, id="partial", remaining_qty=1.0,
                    filled_qty=2.0, status=OrderStatus.PARTIALLY_FILLED)
    filled = Order("A/USDT", "buy", 3.0, id="filled", remaining_qty=0.0, status=OrderStatus.FILLED)
    canceled = Order("A/USDT", "buy", 3.0, id="canceled", remaining_qty=3.0, status=OrderStatus.CANCELED)
    empty = Order("A/USDT", "buy", 3.0, id="empty", remaining_qty=0.0, status=OrderStatus.ACCEPTED)
    context["broker"] = SimpleNamespace(broker=SimpleNamespace(
        pending_orders=[pending, filled], active_orders=[pending, partial, canceled, empty]))
    selector.select([Candidate()], **context)
    assert model.inputs[-1]["pending_count_fraction"].iloc[0] == pytest.approx(2 / 8)
