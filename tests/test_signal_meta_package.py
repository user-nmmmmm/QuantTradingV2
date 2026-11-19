"""B1: the signal-meta modules live in ``signal_meta/``; fingerprints keep their old keys."""
from __future__ import annotations

from pathlib import Path

import pytest

from backtest import signal_meta_replay
from signal_meta import signal_adaptive_types, signal_ev_types

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "signal_actuals", "signal_adaptive", "signal_adaptive_types", "signal_axis_attribution", "signal_ev_ledger",
    "signal_ev_types", "signal_label_versions", "signal_meta_layer", "signal_observation",
    "signal_observation_types", "signal_outcomes", "signal_regime_model", "temporal_financing", "temporal_labels",
)


def test_modules_live_in_signal_meta_and_not_in_core():
    for name in MODULES:
        assert (ROOT / "signal_meta" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"
    # temporal_data stays in core (now core/market): the market-data layer needs it
    assert (ROOT / "core" / "market" / "temporal_data.py").is_file()


class _Policy:
    def to_dict(self):
        return {"p": 1}


def test_replay_identity_keeps_the_pre_move_keys_and_reads_the_moved_file(monkeypatch):
    monkeypatch.setattr(signal_meta_replay, "fingerprint", lambda value: value)
    identity = signal_meta_replay._identity(_Policy())
    assert set(identity["sources"]) == {
        "backtest/signal_meta_replay.py", "backtest/signal_ghost.py", "core/signal_adaptive_types.py",
        "core/broker/__init__.py", "core/broker/matching.py", "core/broker/fill_service.py",
        "core/broker/financing.py", "core/portfolio.py",
    }
    moved = (ROOT / "signal_meta" / "signal_adaptive_types.py").read_text(encoding="utf-8")
    assert identity["sources"]["core/signal_adaptive_types.py"] == moved


@pytest.mark.parametrize("module, function, expected", [
    (signal_ev_types, "implementation_identity",
     {"signal_ev_types.py", "signal_ev_ledger.py", "signal_meta_layer.py", "signal_label_versions.py"}),
    (signal_adaptive_types, "adaptive_implementation_identity",
     {"signal_adaptive_types.py", "signal_regime_model.py", "signal_axis_attribution.py", "signal_adaptive.py",
      "signal_ev_types.py", "signal_ev_ledger.py", "signal_meta_layer.py", "signal_label_versions.py"}),
])
def test_model_identities_still_hash_the_same_set_of_files(monkeypatch, module, function, expected):
    class Policy:
        def to_dict(self):
            return {}

    monkeypatch.setattr(module, "fingerprint", lambda value: value)
    result = getattr(module, function)(Policy())
    assert set(result["sources"]) == expected
    for name, source in result["sources"].items():
        assert source == (ROOT / "signal_meta" / name).read_text(encoding="utf-8")


def test_label_reconciliation_modules_moved_from_analysis_to_signal_meta():
    # B3 (ADR 0017): backtest.engine needs them, and backtest must not import analysis
    for name in ("paper_labels", "paper_label_execution", "label_account_reconciliation"):
        assert (ROOT / "signal_meta" / f"{name}.py").is_file(), name
        assert not (ROOT / "analysis" / f"{name}.py").exists(), f"analysis/{name}.py should have moved"
