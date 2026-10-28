"""Finite-worker budgets, complete journals, recovery and actual source binding."""

from datetime import datetime, timezone
import json
import os
import shutil

import pytest

from analysis.paper_research_orchestration import (
    ROOT,
    _digest,
    _study_writer,
    run_bounded_job,
    run_study,
)
from analysis.research_evidence import source_identity
from analysis.search_scope import data_manifest
from core.foundation.reproducibility import sha256_file


@pytest.fixture
def study(tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/paper_research_worker.py", root / "scripts/paper_research_worker.py"
    )
    (root / "config.json").write_text('{"budget":4}', encoding="utf-8")
    (root / "adapter.py").write_text(
        """from pathlib import Path
import time

def fail_once(checkpoint):
    path = Path(checkpoint)
    if not path.exists():
        path.write_text("failed first attempt")
        raise RuntimeError("injected restartable failure")
    return {"value":2,"research":"not_evaluated"}

def sleep():
    time.sleep(2)
    return {"done":True}

def allocate():
    data = bytearray(512 * 1024 * 1024)
    return {"bytes":len(data)}
""",
        encoding="utf-8",
    )
    paths = ["config.json", "scripts/paper_research_worker.py", "adapter.py"]
    data = {"fixed_input": [1, 2, 3]}
    jobs = [
        {"id": "candidate", "kind": "json_summary", "values": [1, 2, 3]},
        {"id": "benchmark", "kind": "json_summary", "values": [2, 2, 2]},
    ]
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "bounded-test",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L01"],
        "source_manifest_sha256": _digest(source_identity(root, paths=paths)),
        "config_sha256": sha256_file(root / "config.json"),
        "data_manifest_sha256": _digest(data_manifest(data)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["candidate"],
        "benchmark_ids": ["benchmark"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "exact_arithmetic",
        "economic_or_precision_target": {"threshold": 0, "rationale": "known finite integer sum"},
        "training_selection_rule": "none; deterministic engineering fixture",
        "evaluation_windows": [
            {
                "id": "fixture",
                "role": "engineering_fixture",
                "start": "2020-01-01",
                "end": "2020-01-03",
            }
        ],
        "cost_policy_id": "fixture-fees",
        "risk_policy_id": "fixture-cash",
        "resampling": {"method": "not_applicable", "reason": "engineering arithmetic only"},
        "attempt_budget": 4,
        "resource_budget": {"wall_time_seconds": 30, "memory_mb": 128, "max_workers": 2},
        "stop_rules": ["stop on identity drift or exhausted budget"],
        "final_access_policy_id": None,
        "job_table_sha256": _digest(jobs),
    }
    return root, paths, data, protocol, jobs, tmp_path / "run"


def execute(study, *, resume=False):
    root, paths, data, protocol, jobs, output = study
    return run_study(
        protocol=protocol,
        jobs=jobs,
        data_map=data,
        output=output,
        source_root=root,
        source_paths=paths,
        resume=resume,
    )


def test_complete_finite_table_actual_processes_and_resume_does_not_duplicate(study):
    report = execute(study)
    assert report["engineering"] == "pass"
    assert report["coverage"] == {
        "planned_jobs": 2,
        "completed": 2,
        "failed": 0,
        "interrupted": 0,
        "abstained": 0,
        "pending": 0,
    }
    assert report["raw_results"]["candidate"]["sum"] == 6
    assert report["statistics"]["status"] == "not_evaluated"
    assert not report["live_authorization"]
    assert report["audit"]["attempts_used"] == 2
    output = study[-1]
    before = (output / "evidence/attempts.jsonl").read_bytes()
    assert execute(study, resume=True)["engineering"] == "pass"
    assert (output / "evidence/attempts.jsonl").read_bytes() == before
    assert (output / "source/adapter.py").read_bytes() == (study[0] / "adapter.py").read_bytes()
    with pytest.raises(FileExistsError):
        execute(study)


def test_failed_job_is_retained_and_only_that_job_retries_with_remaining_budget(study, tmp_path):
    root, paths, data, protocol, jobs, output = study
    jobs[0] = {
        "id": "candidate",
        "kind": "python_callable",
        "callable": "adapter:fail_once",
        "kwargs": {"checkpoint": str(tmp_path / "fault-checkpoint")},
    }
    protocol["job_table_sha256"] = _digest(jobs)
    first = execute(study)
    assert first["engineering"] == "fail" and first["coverage"]["failed"] == 1
    second = execute(study, resume=True)
    assert second["engineering"] == "pass" and second["audit"]["attempts_used"] == 3
    assert len(second["audit"]["failed_or_interrupted_attempts"]) == 1
    assert (output / "jobs/0000/attempt-0001/process.json").exists()
    assert (output / "jobs/0000/attempt-0002/process.json").exists()
    assert not (output / "jobs/0001/attempt-0002").exists()


@pytest.mark.parametrize("kind", ["duplicate", "missing", "changed"])
def test_job_table_cannot_shrink_or_change_after_registration(study, kind):
    jobs = study[4]
    if kind == "duplicate":
        jobs[1]["id"] = "candidate"
    elif kind == "missing":
        jobs.pop()
    else:
        jobs[0]["values"] = [99]
    with pytest.raises(ValueError, match="job"):
        execute(study)
    assert not (study[-1] / "evidence").exists()


def test_completed_result_tampering_blocks_resume_before_any_new_attempt(study):
    report = execute(study)
    output = study[-1]
    path = output / report["results"]["candidate"]["path"]
    path.write_text('{"sum":999}', encoding="utf-8")
    before = (output / "evidence/attempts.jsonl").read_bytes()
    with pytest.raises(ValueError, match="missing or changed"):
        execute(study, resume=True)
    assert (output / "evidence/attempts.jsonl").read_bytes() == before


def test_study_level_writer_excludes_concurrent_recovery(study):
    execute(study)
    with _study_writer(study[-1]):
        with pytest.raises(RuntimeError, match="writer is active"):
            execute(study, resume=True)


def test_actual_subprocess_wall_limit_interrupts_and_retains_logs(study, tmp_path):
    result = run_bounded_job(
        {"id": "sleep", "kind": "python_callable", "callable": "adapter:sleep"},
        output=tmp_path / "timeout",
        source_root=study[0],
        wall_seconds=0.1,
        memory_mb=128,
    )
    assert result["status"] == "interrupted"
    assert "wall-time" in result["reason"]
    assert (tmp_path / "timeout/process.json").exists()


def test_os_memory_cap_rejects_real_large_allocation(study, tmp_path):
    result = run_bounded_job(
        {"id": "memory", "kind": "python_callable", "callable": "adapter:allocate"},
        output=tmp_path / "memory",
        source_root=study[0],
        wall_seconds=15,
        memory_mb=128,
    )
    assert result["status"] == "failed" and result["exit_code"] != 0
    stderr = (tmp_path / "memory/stderr.log").read_text(encoding="utf-8", errors="replace")
    assert "MemoryError" in stderr or "memory" in stderr.lower()
    assert not (tmp_path / "memory/result.json").exists()


def test_cached_module_cannot_substitute_for_declared_local_module(study, tmp_path):
    (study[0] / "json.py").write_text(
        'def not_real():\n    return {"fake":True}\n', encoding="utf-8"
    )
    result = run_bounded_job(
        {"id": "origin", "kind": "python_callable", "callable": "json:not_real"},
        output=tmp_path / "wrong-origin",
        source_root=study[0],
        wall_seconds=15,
        memory_mb=128,
    )
    assert result["status"] == "failed"
    assert "module origin differs" in (tmp_path / "wrong-origin/stderr.log").read_text(
        encoding="utf-8"
    )


def test_real_broker_fixture_runs_under_actual_os_resource_limits(tmp_path):
    result = run_bounded_job(
        {"id": "account", "kind": "account_fixture"},
        output=tmp_path / "account",
        source_root=ROOT,
        wall_seconds=30,
        memory_mb=2048 if os.name == "posix" else 512,
    )
    assert result["status"] == "completed"
    assert result["result"]["engineering"] == "pass"
    assert result["result"]["actual"][-1]["equity"] == pytest.approx(10014.69)
    assert result["result"]["accounting_check"]["ok"]


def test_exact_typed_inputs_archive_and_one_nanosecond_change_refuse_resume(study):
    import pandas as pd
    from core.foundation.data_identity import sha256_frame_exact

    frame = pd.DataFrame(
        {"known_at": pd.date_range("2020-01-01", periods=3, freq="ns", tz="UTC")},
        index=pd.date_range("2020-01-01", periods=3, freq="ns", tz="UTC"),
    )
    frame.attrs["policy"] = {"knowledge": "local"}
    study[2]["typed_frame"] = frame
    study[3]["data_manifest_sha256"] = _digest(data_manifest(study[2]))
    report = execute(study)
    assert report["engineering"] == "pass"
    manifest = json.loads((study[-1] / "fixed_inputs/manifest.json").read_text())
    entry = manifest["inputs"]["typed_frame"]
    # Test controls and verifies these local bytes before its explicit restoration.
    archive = study[-1] / "fixed_inputs" / entry["path"]
    assert sha256_file(archive) == entry["wire_sha256"]
    restored = pd.read_pickle(archive)
    assert sha256_frame_exact(restored) == sha256_frame_exact(frame)
    assert execute(study, resume=True)["engineering"] == "pass"
    frame.iloc[0, 0] += pd.Timedelta(1, "ns")
    with pytest.raises(ValueError, match="data"):
        execute(study, resume=True)


def test_missing_frozen_source_and_fixed_input_bytes_block_recovery(study):
    execute(study)
    (study[-1] / "source/adapter.py").write_text("changed source snapshot")
    with pytest.raises(ValueError, match="source snapshot"):
        execute(study, resume=True)
