from __future__ import annotations

import json

import pytest

from analysis.research_evidence import ResearchEvidenceRun
from analysis.search_scope import audit_attempts, audit_run, source_closure, verify_source_closure


def ev(sequence, candidate, status, **extra):
    return {"sequence": sequence, "candidate": candidate, "status": status, "phase": "run", **extra}


def finish(run, **statuses):
    for name, status in statuses.items():
        run.record(candidate=name, phase="run", status="started")
        run.record(candidate=name, phase="run", status=status)


@pytest.fixture
def run(tmp_path):
    return ResearchEvidenceRun(candidates=["a", "b", "c"], parameters={"seed": 1}, data_map={}, output=tmp_path / "run",
                               source_hashes={})


def test_complete_family_is_local_complete_but_never_historically_complete(run, tmp_path):
    finish(run, a="completed", b="completed", c="completed")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is True and report["problems"] == []
    assert report["historical_global_search_complete"] is False
    assert report["status_counts"] == {"completed": 3}


def test_failed_interrupted_and_abstained_arms_are_kept_not_dropped(run, tmp_path):
    finish(run, a="completed", b="failed", c="abstained")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is True
    assert report["kept_unsuccessful"] == ["b", "c"] and report["registered"] == ["a", "b", "c"]
    run.record(candidate="a", phase="run", status="started")
    run.record(candidate="a", phase="run", status="interrupted")
    assert audit_run(tmp_path / "run")["final_status"]["a"] == "interrupted"
    assert audit_run(tmp_path / "run")["attempt_counts"]["a"] == 2


def test_arm_that_never_finished_blocks_completeness(run, tmp_path):
    finish(run, a="completed", b="completed")
    run.record(candidate="c", phase="run", status="started")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False and report["not_terminal"] == ["c"]


def test_registered_arm_missing_from_the_log_is_detected():
    report = audit_attempts(["a", "b"], [ev(0, "a", "planned"), ev(1, "a", "completed")])
    assert report["local_complete"] is False
    assert report["missing_planned"] == ["b"] and report["not_terminal"] == ["b"]


def test_truncated_log_line_is_reported(run, tmp_path):
    finish(run, a="completed", b="completed", c="completed")
    log = tmp_path / "run/attempts.jsonl"
    log.write_bytes(log.read_bytes() + b'{"sequence": 99, "candidate": "a", "sta')
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False
    assert any("not valid JSON" in p for p in report["problems"]) and any("newline" in p for p in report["problems"])


def test_dropped_event_in_the_middle_breaks_the_sequence(run, tmp_path):
    finish(run, a="completed", b="completed", c="completed")
    log = tmp_path / "run/attempts.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    del lines[4]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False and any("sequence" in p for p in report["problems"])


def test_events_from_another_run_identity_are_rejected(run, tmp_path):
    finish(run, a="completed", b="completed", c="completed")
    log = tmp_path / "run/attempts.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    rows[2]["identity"] = "0" * 64
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert any("different run identity" in p for p in audit_run(tmp_path / "run")["problems"])


def test_unregistered_candidate_unknown_status_and_duplicates():
    events = [ev(0, "a", "planned"), ev(1, "a", "ok"), ev(2, "ghost", "completed")]
    report = audit_attempts(["a", "a"], events)
    assert report["local_complete"] is False
    assert report["unregistered_candidates"] == ["ghost"]
    assert report["unrecognized_status_events"] == [{"sequence": 1, "candidate": "a", "status": "ok"}]
    assert any("duplicate registered" in p for p in report["problems"])


def test_identity_change_after_registration_blocks_completeness(run, tmp_path):
    finish(run, a="completed", b="completed", c="completed")
    run.record(phase="identity", status="changed", data_unchanged=False)
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False and report["identity_changed_events"]


def test_empty_registration_is_never_complete():
    assert audit_attempts([], [])["local_complete"] is False


def test_closure_covers_tests_models_and_listed_data_and_detects_each_change(tmp_path):
    for name, text in {"core/a.py": "x", "tests/test_a.py": "t", "research/m/model.pt": "w", "data/in.csv": "1,2"}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    recorded = source_closure(tmp_path, extra_files=["data/in.csv"])
    assert {"core/a.py", "tests/test_a.py", "research/m/model.pt", "data/in.csv"} <= set(recorded)
    assert verify_source_closure(recorded, tmp_path, extra_files=["data/in.csv"])["unchanged"] is True
    for name in ("tests/test_a.py", "research/m/model.pt", "data/in.csv", "core/a.py"):
        path = tmp_path / name
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "!", encoding="utf-8")
        result = verify_source_closure(recorded, tmp_path, extra_files=["data/in.csv"])
        assert result["unchanged"] is False and result["changed"] == [name]
        path.write_text(original, encoding="utf-8")
    (tmp_path / "core/new_untracked.py").write_text("y", encoding="utf-8")
    assert verify_source_closure(recorded, tmp_path, extra_files=["data/in.csv"])["added"] == ["core/new_untracked.py"]


def test_recorded_hashes_are_not_trusted_without_local_bytes(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core/a.py").write_text("x", encoding="utf-8")
    forged = {"core/a.py": "0" * 64}
    assert verify_source_closure(forged, tmp_path)["changed"] == ["core/a.py"]
    with pytest.raises(FileNotFoundError):
        source_closure(tmp_path, extra_files=["missing.bin"])
