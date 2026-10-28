"""Adversarial checks for finite-family contracts and interrupted-run history."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from analysis.research_evidence import ResearchEvidenceRun, source_identity
from analysis.search_scope import (
    SearchScope, audit_attempts, audit_run, data_manifest, load_protocol,
    source_closure, verify_source_closure,
)
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file


def digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


@pytest.fixture
def study(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.json").write_text('{"fee":0.001}', encoding="utf-8")
    (root / "model.bin").write_bytes(b"fixed-model-v1")
    (root / "research").mkdir()
    (root / "research" / "engine.py").write_text("version = 1\n", encoding="utf-8")
    paths = ["config.json", "model.bin", "research"]
    data = {"bars": {"prices": [10, 12, 11]}}
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "fixed-fixture",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L01"],
        "source_manifest_sha256": digest(source_identity(root, paths=paths)),
        "config_sha256": sha256_file(root / "config.json"),
        "data_manifest_sha256": digest(data_manifest(data)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["candidate"],
        "benchmark_ids": ["benchmark"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "account_bridge_error",
        "economic_or_precision_target": {
            "threshold": 1e-8,
            "rationale": "independent account arithmetic",
        },
        "training_selection_rule": "no fitting; fixed fixture",
        "evaluation_windows": [
            {
                "id": "fixture",
                "role": "engineering_fixture",
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-03T00:00:00Z",
            }
        ],
        "cost_policy_id": "fixture_fees/v1",
        "risk_policy_id": "fixture_cash/v1",
        "resampling": {"method": "not_applicable", "reason": "deterministic arithmetic"},
        "attempt_budget": 4,
        "resource_budget": {"wall_time_seconds": 60, "memory_mb": 512, "max_workers": 1},
        "stop_rules": ["stop on account or identity mismatch"],
        "final_access_policy_id": None,
    }
    return root, paths, data, protocol, tmp_path / "run"


def register(study, **changes):
    root, paths, data, protocol, output = study
    protocol = {**protocol, **changes}
    return SearchScope.register(
        protocol=protocol, data_map=data, output=output, source_root=root, source_paths=paths
    )


def finish(scope, candidate="candidate"):
    scope.record(candidate=candidate, status="started")
    scope.record(candidate=candidate, status="completed", result={"max_error": 0.0})


def test_protocol_detaches_values_and_preserves_non_admission(study):
    scope = register(study)
    study[3]["candidate_ids"].append("later_search")
    assert scope.protocol["candidate_ids"] == ["candidate"]
    finish(scope)
    scope.record(candidate="benchmark", status="abstained", reason="fixture unavailable")
    audit = scope.audit()
    assert audit["status"] == "ok" and audit["local_family_complete"]
    assert not audit["results_complete"] and not audit["historical_global_search_complete"]
    assert not audit["admission_eligible"]
    assert audit["research"] == "not_evaluated"
    assert sorted(p.name for p in scope.output.glob("*.jsonl")) == ["attempts.jsonl"]


def test_failed_attempt_survives_resume_and_consumes_budget(study):
    scope = register(study, attempt_budget=3)
    scope.record(candidate="candidate", status="started")
    scope.record(candidate="candidate", status="failed", reason="solver failed")
    root, paths, data, _, output = study
    resumed = SearchScope.resume(output=output, data_map=data, source_root=root, source_paths=paths)
    finish(resumed)
    finish(resumed, "benchmark")
    audit = resumed.audit()
    assert audit["results_complete"] and audit["attempts_used"] == 3
    assert len(audit["failed_or_interrupted_attempts"]) == 1
    assert [e["status"] for e in resumed.run.events if e.get("candidate") == "candidate"] == [
        "planned",
        "started",
        "failed",
        "started",
        "completed",
    ]


def test_recovery_marks_unfinished_attempt_interrupted(study):
    scope = register(study)
    scope.record(candidate="candidate", status="started")
    root, _, data, _, output = study
    resumed = SearchScope.resume(output=output, data_map=data, source_root=root)
    assert resumed.audit()["states"]["candidate"] == "interrupted"
    finish(resumed)
    assert resumed.audit()["attempts"]["candidate"] == 2
    with pytest.raises(ValueError, match="changed since"):
        scope.record(candidate="candidate", status="failed", reason="stale writer")


def test_budget_exhaustion_cannot_erase_failed_arm(study):
    scope = register(study, attempt_budget=2)
    scope.record(candidate="candidate", status="started")
    scope.record(candidate="candidate", status="failed", reason="no convergence")
    finish(scope, "benchmark")
    before = (scope.output / "attempts.jsonl").read_bytes()
    with pytest.raises(ValueError, match="attempt budget"):
        scope.record(candidate="candidate", status="started")
    assert (scope.output / "attempts.jsonl").read_bytes() == before
    assert scope.audit()["local_family_complete"] and not scope.audit()["results_complete"]


@pytest.mark.parametrize("mutation", ["tail_line", "middle_line", "partial_tail", "registration"])
def test_lost_or_altered_bytes_fail_closed(study, mutation):
    scope = register(study)
    finish(scope)
    journal = scope.output / "attempts.jsonl"
    lines = journal.read_bytes().splitlines(keepends=True)
    if mutation == "tail_line":
        journal.write_bytes(b"".join(lines[:-1]))
    elif mutation == "middle_line":
        journal.write_bytes(b"".join(lines[:1] + lines[2:]))
    elif mutation == "partial_tail":
        journal.write_bytes(b"".join(lines)[:-5])
    else:
        path = scope.output / "registration.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["parameters"]["research_protocol"]["attempt_budget"] += 100
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert scope.audit()["status"] == "invalid"
    root, _, data, _, output = study
    with pytest.raises(ValueError):
        SearchScope.resume(output=output, data_map=data, source_root=root)


def test_missing_arm_detected_even_with_rebuilt_checkpoint(study):
    scope = register(study)
    scope.run.events = [event for event in scope.run.events if event["candidate"] != "benchmark"]
    journal = scope.output / "attempts.jsonl"
    journal.write_text(
        "".join(canonical_json(event) + "\n" for event in scope.run.events), encoding="utf-8"
    )
    scope._checkpoint()
    assert scope.audit()["status"] == "invalid"
    assert "missing declared arm" in scope.audit()["errors"][0]


@pytest.mark.parametrize("kind", ["model", "configuration", "new_source", "dependency"])
def test_local_byte_drift_refuses_new_attempt(study, kind):
    root, paths, _, protocol, _ = study
    if kind == "dependency":
        (root / "requirements.lock.txt").write_text("numpy==1.0", encoding="utf-8")
        paths.append("requirements.lock.txt")
        protocol["source_manifest_sha256"] = digest(source_identity(root, paths=paths))
    scope = register(study)
    path = {
        "model": root / "model.bin",
        "configuration": root / "config.json",
        "new_source": root / "research" / "untracked.py",
        "dependency": root / "requirements.lock.txt",
    }[kind]
    path.write_bytes(b"changed bytes")
    assert scope.audit()["status"] == "invalid"
    with pytest.raises(ValueError, match="manifest"):
        scope.record(candidate="candidate", status="started")


@pytest.mark.parametrize("kind", ["json", "frame"])
def test_inplace_data_drift_detected_during_run_and_resume(study, kind):
    root, paths, data, protocol, output = study
    if kind == "frame":
        data["bars"] = pd.DataFrame(
            {"close": [10.0, 12.0]}, index=pd.date_range("2024-01-01", periods=2)
        )
        protocol["data_manifest_sha256"] = digest(data_manifest(data))
    scope = register(study)
    if kind == "frame":
        data["bars"].iloc[0, 0] += 1
    else:
        data["bars"]["prices"][0] += 1
    assert scope.audit()["status"] == "invalid"
    with pytest.raises(ValueError):
        scope.record(candidate="candidate", status="started")
    with pytest.raises(ValueError):
        SearchScope.resume(output=output, data_map=data, source_root=root, source_paths=paths)


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_ids": ["candidate", "candidate"]},
        {"attempt_budget": True},
        {"attempt_budget": 1},
        {"historical_global_search_complete": True},
        {"source_manifest_sha256": "unknown"},
        {
            "resource_budget": {
                "wall_time_seconds": float("nan"),
                "memory_mb": 512,
                "max_workers": 1,
            }
        },
        {"primary_metric": ""},
        {"resampling": {"method": "not_applicable"}},
        {
            "evaluation_windows": [
                {"id": "x", "role": "final", "start": "2024-01-01", "end": "2024-02-01"}
            ]
        },
    ],
)
def test_invalid_protocol_rejected_before_output_creation(study, changes):
    with pytest.raises(ValueError):
        register(study, **changes)
    assert not study[4].exists()


def test_hash_claims_are_verified_before_registration(study):
    protocol = deepcopy(study[3])
    protocol["source_manifest_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="source manifest"):
        register(study, **protocol)
    protocol = deepcopy(study[3])
    protocol["data_manifest_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="data manifest"):
        register(study, **protocol)
    protocol = deepcopy(study[3])
    protocol["config_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="config"):
        register(study, **protocol)


def test_illegal_transitions_and_nonfinite_details_do_not_append(study):
    scope = register(study)
    before = (scope.output / "attempts.jsonl").read_bytes()
    for candidate, status in [
        ("undeclared", "started"),
        ("candidate", "completed"),
        ("candidate", "planned"),
    ]:
        with pytest.raises(ValueError):
            scope.record(candidate=candidate, status=status)
    with pytest.raises(ValueError, match="finite JSON"):
        scope.record(candidate="candidate", status="started", metric=float("inf"))
    with pytest.raises(ValueError, match="reserved"):
        scope.record(candidate="candidate", status="started", identity="forged")
    assert (scope.output / "attempts.jsonl").read_bytes() == before


def test_output_is_not_overwritten_and_closure_cannot_change_on_resume(study):
    scope = register(study)
    with pytest.raises(FileExistsError):
        register(study)
    root, _, data, _, output = study
    with pytest.raises(ValueError, match="closure"):
        SearchScope.resume(
            output=output, data_map=data, source_root=root, source_paths=["config.json"]
        )
    assert scope.audit()["status"] == "ok"


@pytest.mark.parametrize("payload", ['{"schema":"a","schema":"b"}', '{"x":NaN}', '{"x":1e999}'])
def test_json_reader_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path, payload):
    path = tmp_path / "protocol.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_protocol(path)


def test_default_closure_detects_added_code(study):
    root, _, data, protocol, output = study
    (root / "config").mkdir()
    (root / "config" / "fixture.json").write_bytes((root / "config.json").read_bytes())
    protocol["source_manifest_sha256"] = digest(source_identity(root))
    scope = SearchScope.register(protocol=protocol, data_map=data, output=output, source_root=root)
    assert scope.run.registration["source_scope"]["paths"] is None
    (root / "research" / "new_model.py").write_text("new = True", encoding="utf-8")
    assert scope.audit()["status"] == "invalid"


def test_generator_closure_keeps_all_declared_files(study):
    root, paths, data, protocol, output = study
    scope = SearchScope.register(
        protocol=protocol,
        data_map=data,
        output=output,
        source_root=root,
        source_paths=(p for p in paths),
    )
    assert scope.audit()["status"] == "ok"
    assert set(scope.run.registration["source_hashes"]) == set(source_identity(root, paths=paths))


def test_stale_writer_lock_refuses_record_and_resume(study):
    scope = register(study)
    lock = scope.output / ".search_scope_writer.lock"
    lock.write_text("unknown interrupted process", encoding="utf-8")
    with pytest.raises(ValueError, match="writer"):
        scope.record(candidate="candidate", status="started")
    root, _, data, _, output = study
    with pytest.raises(ValueError, match="writer"):
        SearchScope.resume(output=output, data_map=data, source_root=root)
    assert lock.exists()


def test_registration_rechecks_bytes_captured_by_underlying_recorder(study, monkeypatch):
    import analysis.research_evidence as evidence

    root, _, _, _, _ = study
    original = evidence.source_identity

    def changed_between_reads(*args, **kwargs):
        (root / "model.bin").write_bytes(b"changed during registration")
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence, "source_identity", changed_between_reads)
    with pytest.raises(ValueError, match="source manifest"):
        register(study)
    assert not (study[4] / "journal_state.json").exists()


def test_mutated_inmemory_protocol_cannot_increase_budget(study):
    scope = register(study)
    scope.protocol["attempt_budget"] = 999
    assert scope.audit()["status"] == "invalid"
    with pytest.raises(ValueError, match="protocol changed"):
        scope.record(candidate="candidate", status="started")


def test_abstained_and_failed_reason_must_be_finite_text(study):
    scope = register(study)
    with pytest.raises(ValueError):
        scope.record(candidate="candidate", status="abstained")
    with pytest.raises(ValueError, match="finite JSON"):
        scope.record(candidate="candidate", status="abstained", reason=float("nan"))


# B1 journal/closure audits retain their original regressions after integration.
def ev(sequence, candidate, status, **extra):
    return {"sequence": sequence, "candidate": candidate, "status": status, "phase": "run", **extra}


def finish_b1_run(run, **statuses):
    for name, status in statuses.items():
        run.record(candidate=name, phase="run", status="started")
        run.record(candidate=name, phase="run", status=status)


@pytest.fixture
def run(tmp_path):
    return ResearchEvidenceRun(candidates=["a", "b", "c"], parameters={"seed": 1}, data_map={}, output=tmp_path / "run",
                               source_hashes={})


def test_complete_family_is_local_complete_but_never_historically_complete(run, tmp_path):
    finish_b1_run(run, a="completed", b="completed", c="completed")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is True and report["problems"] == []
    assert report["historical_global_search_complete"] is False
    assert report["status_counts"] == {"completed": 3}


def test_failed_interrupted_and_abstained_arms_are_kept_not_dropped(run, tmp_path):
    finish_b1_run(run, a="completed", b="failed", c="abstained")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is True
    assert report["kept_unsuccessful"] == ["b", "c"] and report["registered"] == ["a", "b", "c"]
    run.record(candidate="a", phase="run", status="started")
    run.record(candidate="a", phase="run", status="interrupted")
    assert audit_run(tmp_path / "run")["final_status"]["a"] == "interrupted"
    assert audit_run(tmp_path / "run")["attempt_counts"]["a"] == 2


def test_arm_that_never_finished_blocks_completeness(run, tmp_path):
    finish_b1_run(run, a="completed", b="completed")
    run.record(candidate="c", phase="run", status="started")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False and report["not_terminal"] == ["c"]


def test_registered_arm_missing_from_the_log_is_detected():
    report = audit_attempts(["a", "b"], [ev(0, "a", "planned"), ev(1, "a", "completed")])
    assert report["local_complete"] is False
    assert report["missing_planned"] == ["b"] and report["not_terminal"] == ["b"]


def test_truncated_log_line_is_reported(run, tmp_path):
    finish_b1_run(run, a="completed", b="completed", c="completed")
    log = tmp_path / "run/attempts.jsonl"
    log.write_bytes(log.read_bytes() + b'{"sequence": 99, "candidate": "a", "sta')
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False
    assert any("not valid JSON" in p for p in report["problems"]) and any("newline" in p for p in report["problems"])


def test_dropped_event_in_the_middle_breaks_the_sequence(run, tmp_path):
    finish_b1_run(run, a="completed", b="completed", c="completed")
    log = tmp_path / "run/attempts.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    del lines[4]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = audit_run(tmp_path / "run")
    assert report["local_complete"] is False and any("sequence" in p for p in report["problems"])


def test_events_from_another_run_identity_are_rejected(run, tmp_path):
    finish_b1_run(run, a="completed", b="completed", c="completed")
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
    finish_b1_run(run, a="completed", b="completed", c="completed")
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
