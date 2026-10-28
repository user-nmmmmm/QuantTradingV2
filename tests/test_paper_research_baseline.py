"""PR25-01 account arithmetic, artifact safety and failure interpretation."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import capture_paper_research_baseline as baseline


def test_fixed_account_checks_partial_exit_fees_and_open_inventory():
    receipt = baseline.replay_fixed_account()
    assert receipt["engineering"] == "pass"
    assert receipt["actual"][-1]["cash"] == pytest.approx(9909.69)
    assert receipt["actual"][-1]["equity"] == pytest.approx(10014.69)
    assert receipt["actual"][-1]["fees"] == pytest.approx(0.31)
    assert receipt["actual"][-1]["qty"] == 1
    assert receipt["actual"][-1]["realized_net_pnl"] == pytest.approx(9.79)
    assert receipt["actual"][-1]["unrealized_net_pnl"] == pytest.approx(4.9)
    assert receipt["accounting_check"]["checks_performed"] == 3
    assert receipt["research"] == "not_evaluated"
    assert receipt["facts_sha256"] == baseline.replay_fixed_account()["facts_sha256"]


def test_detects_cash_corruption_even_when_internal_equity_uses_same_cash(monkeypatch):
    from core.broker import Broker

    original = Broker.process_orders

    def corrupt_cash(self, bars):
        trades = original(self, bars)
        self.portfolio.cash += 1
        return trades

    monkeypatch.setattr(Broker, "process_orders", corrupt_cash)
    receipt = baseline.replay_fixed_account()
    assert receipt["engineering"] == "fail"
    assert any(row["metric"] == "cash" for row in receipt["differences"])
    assert receipt["accounting_check"]["ok"] is False


def _stub_capture(monkeypatch):
    monkeypatch.setattr(baseline, "capture_environment", lambda root: {"status": "pass"})
    monkeypatch.setattr(
        baseline,
        "capture_current_state",
        lambda root: {
            "source_hashes": {"core/example.py": "a" * 64},
            "source_manifest_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "data_manifest_sha256": "d" * 64,
        },
    )

    def snapshot(root, destination, files):
        destination.mkdir()
        return {"directory": "source", "files_verified": len(files)}

    monkeypatch.setattr(baseline, "snapshot_sources", snapshot)


def test_fresh_bundle_contains_inputs_and_refuses_overwrite(tmp_path, monkeypatch):
    _stub_capture(monkeypatch)
    target = tmp_path / "new-run"
    assert baseline.capture(target)["engineering"] == "pass"
    assert {path.name for path in target.iterdir()} == {
        "fixture.json",
        "config.json",
        "current_state.json",
        "environment.json",
        "baseline_diff.json",
        "source",
    }
    before = (target / "baseline_diff.json").read_bytes()
    with pytest.raises(FileExistsError):
        baseline.capture(target)
    assert (target / "baseline_diff.json").read_bytes() == before


def test_source_drift_during_capture_invalidates_baseline(tmp_path, monkeypatch):
    _stub_capture(monkeypatch)
    original = baseline.capture_current_state
    calls = []

    def changed(root):
        state = original(root)
        if calls:
            state["source_hashes"]["untracked_model.py"] = "e" * 64
        calls.append(True)
        return state

    monkeypatch.setattr(baseline, "capture_current_state", changed)
    receipt = baseline.capture(tmp_path / "drift")
    assert receipt["engineering"] == "fail"
    assert receipt["source_drift"] == ["untracked_model.py"]


@pytest.mark.parametrize(
    "error,kind,engineering",
    [
        (ImportError("missing", name="numpy"), "missing_dependency", "pending"),
        (RuntimeError("bad account"), "current_execution_failure", "fail"),
    ],
)
def test_missing_dependency_and_runtime_failure_are_distinct(
    tmp_path, monkeypatch, error, kind, engineering
):
    _stub_capture(monkeypatch)

    def fail():
        raise error

    monkeypatch.setattr(baseline, "replay_fixed_account", fail)
    receipt = baseline.capture(tmp_path / kind)
    assert receipt["engineering"] == engineering
    assert receipt["failure_kind"] == kind
    assert json.loads((tmp_path / kind / "baseline_diff.json").read_text())["failure_kind"] == kind


def test_reject_mixed_repository_and_runtime(tmp_path):
    with pytest.raises(ValueError, match="actual repository"):
        baseline.capture(tmp_path / "output", root=tmp_path)
    assert not (tmp_path / "output").exists()


def test_source_snapshot_binds_actual_bytes_and_detects_drift(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "untracked_model.py"
    source.write_bytes(b"VALUE = 1\n")
    files = {source.name: hashlib.sha256(source.read_bytes()).hexdigest()}
    receipt = baseline.snapshot_sources(root, tmp_path / "snapshot", files)
    assert receipt["files_verified"] == 1
    assert (tmp_path / "snapshot" / source.name).read_bytes() == source.read_bytes()
    source.write_bytes(b"VALUE = 2\n")
    with pytest.raises(ValueError, match="changed while snapshotting"):
        baseline.snapshot_sources(root, tmp_path / "drift", files)


def test_source_snapshot_rejects_parent_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        baseline.snapshot_sources(root, tmp_path / "snapshot", {"../secret.py": "a" * 64})


def test_environment_records_local_ci_difference_and_optional_dependencies():
    environment = baseline.capture_environment(
        Path(__file__).resolve().parents[1], run_checks=False
    )
    assert environment["status"] == "not_run"
    assert environment["checks_executed"] is False
    assert environment["reference_ci_python"] == "3.11"
    assert "numpy" in environment["dependencies"]
    assert "blas" in environment
    assert "solvers" in environment


@pytest.mark.parametrize(
    "error,status",
    [
        (ImportError("missing", name="numpy"), "missing_dependency"),
        (RuntimeError("manifest error"), "current_execution_failure"),
    ],
)
def test_missing_source_identity_still_produces_explicit_three_receipts(
    tmp_path, monkeypatch, error, status
):
    monkeypatch.setattr(baseline, "capture_environment", lambda root: {"status": "fail"})

    def fail(root):
        raise error

    monkeypatch.setattr(baseline, "capture_current_state", fail)
    baseline.capture(tmp_path / "failed")
    state = json.loads((tmp_path / "failed/current_state.json").read_text())
    assert state["status"] == status
    assert (tmp_path / "failed/environment.json").is_file()
    assert (tmp_path / "failed/baseline_diff.json").is_file()
