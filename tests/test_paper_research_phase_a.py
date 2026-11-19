"""Phase A integration: fixed contract, real account bridge and retained failures."""

import json

import pytest

from analysis.search_scope import SearchScope
from core.foundation.reproducibility import sha256_file
from scripts import capture_paper_research_baseline as baseline
from scripts import verify_paper_research_phase_a as phase


def test_full_fixed_contract_account_snapshot_and_existing_journal_resume(tmp_path, monkeypatch):
    # Environment validation is independently exercised by the capture CLI.
    monkeypatch.setattr(
        baseline, "capture_environment", lambda root: {"status": "pass", "checks_executed": False}
    )
    output = tmp_path / "phase-a"
    receipt = phase.verify(output, clock=lambda: 0.0)
    assert receipt["engineering"] == "pass"
    assert receipt["max_absolute_account_bridge_error"] < 1e-8
    assert receipt["local_family_complete"]
    assert receipt["research"] == "not_evaluated"
    assert not receipt["live_authorization"]
    inputs = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    resumed = SearchScope.resume(
        output=output / "evidence", data_map=inputs, source_root=phase.ROOT
    )
    assert resumed.audit()["attempts_used"] == 2
    assert resumed.audit()["results_complete"]
    state = json.loads((output / "baseline/current_state.json").read_text(encoding="utf-8"))
    for name, digest in state["source_hashes"].items():
        assert sha256_file(output / "baseline/source" / name) == digest
    assert "research/ml_selection/models.py" in state["source_hashes"]
    assert "config/paper_research_phase_a.json" in state["source_hashes"]
    assert phase.decimal_account_bridge()[-1]["equity"] == 10014.69
    for name, digest in receipt["artifact_hashes"].items():
        assert sha256_file(output / name) == digest
    with pytest.raises(FileExistsError):
        phase.verify(output)


def _isolated_sources(tmp_path, monkeypatch):
    root = tmp_path / "isolated-repo"
    (root / "config").mkdir(parents=True)
    (root / "config/paper_research_phase_a.json").write_bytes(
        (phase.ROOT / "config/paper_research_phase_a.json").read_bytes()
    )
    monkeypatch.setattr(phase, "ROOT", root)
    return root


def _completed_capture(root, output, environment_status):
    output.mkdir()
    result = {
        "engineering": "pass",
        "environment_status": environment_status,
        "source_manifest_sha256": phase._digest(phase.source_identity(root)),
        "actual": phase.decimal_account_bridge(),
    }
    phase._write(output / "baseline_diff.json", result)
    return result


def test_account_failure_is_preserved_in_the_single_attempt_journal(tmp_path, monkeypatch):
    _isolated_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(
        phase, "capture", lambda output: {"engineering": "fail", "environment_status": "pass"}
    )
    target = tmp_path / "failed"
    with pytest.raises(ValueError, match="Fixed account"):
        phase.verify(target)
    rows = [
        json.loads(line)
        for line in (target / "evidence/attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["status"] == "failed"
    assert rows[-1]["candidate"] == "broker_partial_close_v1"
    assert any(
        row["candidate"] == "decimal_account_bridge_v1" and row["status"] == "completed"
        for row in rows
    )
    assert json.loads((target / "failure.json").read_text())["engineering"] == "fail"
    assert not (target / "acceptance.json").exists()


def test_original_failure_survives_identity_rejection_of_failure_journal(tmp_path):
    class DriftedScope:
        def record(self, **event):
            raise ValueError("source closure changed")

        def audit(self):
            return {"status": "invalid", "errors": ["source closure changed"]}

    phase._record_failure(
        DriftedScope(), tmp_path, "candidate", RuntimeError("original execution failed")
    )
    failure = json.loads((tmp_path / "failure.json").read_text())
    assert failure["reason"] == "original execution failed"
    assert failure["error_type"] == "RuntimeError"
    assert "source closure changed" in failure["journal_error"]
    assert json.loads((tmp_path / "search_scope_audit.json").read_text())["status"] == "invalid"


def test_environment_gate_failure_does_not_turn_actual_account_success_into_failed_arm(
    tmp_path, monkeypatch
):
    root = _isolated_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(phase, "capture", lambda output: _completed_capture(root, output, "fail"))
    output = tmp_path / "environment-drift"
    receipt = phase.verify(output, clock=lambda: 0.0)
    assert receipt["engineering"] == "fail"
    assert receipt["account_engineering"] == "pass"
    assert receipt["environment_status"] == "fail"
    assert receipt["checks"]["local_environment"] == "fail"
    audit = json.loads((output / "search_scope_audit.json").read_text())
    assert audit["results_complete"] and audit["attempts_used"] == 2
    assert not audit["failed_or_interrupted_attempts"]


def test_registered_wall_time_budget_is_enforced_without_slow_or_flaky_test(tmp_path, monkeypatch):
    root = _isolated_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(phase, "capture", lambda output: _completed_capture(root, output, "pass"))
    ticks = iter([0.0, 181.0])
    output = tmp_path / "timeout"
    with pytest.raises(ValueError, match="wall time budget"):
        phase.verify(output, clock=lambda: next(ticks))
    failure = json.loads((output / "failure.json").read_text())
    assert "wall time budget" in failure["reason"]
    assert not (output / "acceptance.json").exists()
    audit = json.loads((output / "search_scope_audit.json").read_text())
    assert audit["states"]["broker_partial_close_v1"] == "failed"
