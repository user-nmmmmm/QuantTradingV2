"""Bounded subprocess studies using one preregistered SearchScope journal.

One active worker is deliberate and is <= every valid max_workers budget.
Windows uses Job Object private-commit limits and kills the job on close;
POSIX uses per-worker address-space RLIMIT_AS and a separate process group.
Neither limit is a proof about external services or hand-run child processes.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import pickle
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from analysis.research_evidence import source_identity
from analysis.paper_research_typed_inputs import (
    validate_job_input_bindings,
    write_typed_data_manifest,
    verify_typed_data_snapshot,
    typed_transport_runtime_sources,
    registered_typed_worker_inputs,
)
from analysis.search_scope import (
    SearchScope,
    _load_json,
    _strict_json,
    data_manifest,
    load_protocol,
)
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file


ROOT = Path(__file__).resolve().parents[1]


def _digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _windows_job_api():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def _windows_job_limits_type():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("ProcessTime", ctypes.c_longlong),
            ("JobTime", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinWorkingSet", ctypes.c_size_t),
            ("MaxWorkingSet", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperations",
                "WriteOperations",
                "OtherOperations",
                "ReadBytes",
                "WriteBytes",
                "OtherBytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return Extended


def _windows_job_information(memory_mb):
    info = _windows_job_limits_type()()
    info.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000100 | 0x00000200 | 0x00000008
    info.BasicLimitInformation.ActiveProcessLimit = 4
    info.ProcessMemoryLimit = int(memory_mb * 1024 * 1024)
    info.JobMemoryLimit = int(memory_mb * 1024 * 1024)
    return info


def _worker_request(job, source_root, result_path, memory_mb, typed_inputs):
    request = {
        "job": _strict_json(job),
        "source_root": str(Path(source_root).resolve()),
        "result_path": str(result_path),
        "memory_mb": memory_mb,
    }
    if job.get("input_bindings"):
        if typed_inputs is None:
            raise ValueError("bound jobs require a control-plane verified typed input snapshot")
        runtime = typed_transport_runtime_sources()
        if typed_inputs.get("runtime_sources", runtime) != runtime:
            raise ValueError("typed decoder runtime differs from control-plane registered sources")
        request["typed_inputs"] = {**_strict_json(typed_inputs), "runtime_sources": runtime}
    return request


def _worker_outcome(wall_seconds, memory_mb, thread_limits):
    return {
        "status": "failed",
        "exit_code": None,
        "resource_enforcement": (
            "windows_job_aggregate_private_commit"
            if os.name == "nt"
            else "posix_address_space_per_worker"
        ),
        "wall_seconds_limit": wall_seconds,
        "memory_mb_limit": memory_mb,
        "numerical_thread_limits": thread_limits,
    }


def _spawn_worker(source_root, worker_env, stdout, stderr):
    command = [sys.executable, str(ROOT / "scripts/paper_research_worker.py")]
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        cwd=source_root,
        start_new_session=os.name == "posix",
        creationflags=(0x00000004 | 0x08000000) if os.name == "nt" else 0,
        env=worker_env,
    )


def _read_worker_outcome(process, result_path, outcome):
    outcome["exit_code"] = process.returncode
    if process.returncode != 0:
        outcome["reason"] = "worker failed or was terminated by resource limit; inspect stderr"
    elif not result_path.is_file():
        outcome["reason"] = "worker exited without required result"
    else:
        result = _load_json(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("worker result must be a finite JSON object")
        outcome.update(status="completed", result=result, result_sha256=sha256_file(result_path))


def _terminate_worker(process, job_handle, outcome):
    if process.poll() is None:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if job_handle is not None:
        job_handle.close()
    outcome["exit_code"] = process.returncode


def _communicate_worker(process, request, result_path, outcome, wall_seconds, memory_mb):
    job_handle = None
    try:
        if os.name == "nt":
            job_handle = _WindowsJob(process, memory_mb)
            job_handle.resume_suspended_process(process.pid)
        # First input is withheld until the suspended process has joined its Job Object.
        process.communicate(
            (json.dumps(request, allow_nan=False) + "\n").encode("utf-8"), timeout=wall_seconds
        )
        _read_worker_outcome(process, result_path, outcome)
    except subprocess.TimeoutExpired:
        outcome.update(status="interrupted", reason="registered wall-time limit exceeded")
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            outcome.update(status="interrupted", reason="parent interrupted")
        else:
            outcome.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    finally:
        _terminate_worker(process, job_handle, outcome)


def _prepare_study_scope(
    protocol, jobs, data_map, output, source_root, source_paths, sources, resume
):
    if resume:
        registration = _load_json(
            (output / "evidence/registration.json").read_text(encoding="utf-8")
        )
        if registration["parameters"]["research_protocol"] != protocol:
            raise ValueError("resume cannot change protocol or budget")
        scope = SearchScope.resume(
            output=output / "evidence",
            data_map=data_map,
            source_root=source_root,
            source_paths=source_paths,
        )
        if _load_json((output / "jobs.json").read_text(encoding="utf-8")) != jobs:
            raise ValueError("resume job table changed")
    else:
        scope = SearchScope.register(
            protocol=protocol,
            data_map=data_map,
            output=output / "evidence",
            source_root=source_root,
            source_paths=source_paths,
        )
        _write(output / "jobs.json", jobs)
        _write(output / "protocol.json", protocol)
        _snapshot_study_inputs(
            output, source_root, sources, data_map, protocol["data_identity_version"]
        )
    _verify_study_snapshots(scope, output)
    if any(job.get("input_bindings") for job in jobs):
        registered_typed_worker_inputs(data_map, output / "worker_inputs", sources)
    _completed_artifacts(scope, output)
    return scope


def _run_study_attempt(
    scope, protocol, job, number, data_map, output, source_root, sources, study_started
):
    scope.record(candidate=job["id"], status="started")
    attempt = scope.audit()["attempts"][job["id"]]
    directory = output / "jobs" / f"{number:04d}" / f"attempt-{attempt:04d}"
    transport = (
        registered_typed_worker_inputs(data_map, output / "worker_inputs", sources)
        if job.get("input_bindings")
        else None
    )
    remaining = protocol["resource_budget"]["wall_time_seconds"] - (
        time.monotonic() - study_started
    )
    if remaining <= 0:
        directory.mkdir(parents=True, exist_ok=False)
        outcome = {
            "status": "interrupted",
            "reason": "study deadline expired during identity audit",
            "exit_code": None,
        }
        _write(directory / "process.json", outcome)
    else:
        outcome = run_bounded_job(
            job,
            output=directory,
            source_root=source_root,
            wall_seconds=remaining,
            memory_mb=protocol["resource_budget"]["memory_mb"],
            typed_inputs=transport,
        )
    return directory, outcome


def _record_study_outcome(scope, job, directory, output, outcome):
    detail = {
        key: value for key, value in outcome.items() if key not in {"status", "reason", "result"}
    }
    detail["process_artifact"] = (directory / "process.json").relative_to(output).as_posix()
    detail["process_sha256"] = sha256_file(directory / "process.json")
    if outcome["status"] == "completed":
        detail.update(_result_bindings(outcome["result"]))
        scope.record(candidate=job["id"], status="completed", **detail)
    else:
        scope.record(
            candidate=job["id"], status=outcome["status"], reason=outcome["reason"], **detail
        )


def _execute_study_jobs(
    scope, protocol, jobs, data_map, output, source_root, sources, study_started
):
    fatal = stop_reason = None
    for number, job in enumerate(jobs):
        audit = scope.audit()
        if audit["status"] != "ok":
            fatal = "; ".join(audit["errors"])
            break
        state = audit["states"][job["id"]]
        if state in {"completed", "abstained"}:
            continue
        if audit["attempts_used"] >= audit["attempt_budget"]:
            break
        if time.monotonic() - study_started >= protocol["resource_budget"]["wall_time_seconds"]:
            stop_reason = "study worker deadline exhausted; unstarted jobs retained"
            break
        directory, outcome = _run_study_attempt(
            scope, protocol, job, number, data_map, output, source_root, sources, study_started
        )
        try:
            _record_study_outcome(scope, job, directory, output, outcome)
        except (ValueError, OSError) as exc:
            fatal = f"worker outcome retained but journal rejected: {exc}"
            break
    return fatal, stop_reason


def _study_result_artifacts(scope, output):
    artifacts, raw_results = {}, {}
    fatal = None
    for event in scope.run.events:
        if event.get("phase") == "run" and event.get("status") == "completed":
            name = event["candidate"]
            process_path = output / event["process_artifact"]
            result_path = process_path.with_name("result.json")
            if not result_path.is_file() or sha256_file(result_path) != event["result_sha256"]:
                fatal = "completed result absent or modified: " + name
            else:
                artifacts[name] = {
                    "path": result_path.relative_to(output).as_posix(),
                    "sha256": event["result_sha256"],
                }
                raw_results[name] = _load_json(result_path.read_text(encoding="utf-8"))
    return artifacts, raw_results, fatal


def _study_statistics(scope, protocol, raw_results):
    if "statistics" not in protocol:
        return {
            "status": "not_evaluated",
            "reason": "No registered return-panel adapter was requested",
        }
    from analysis.research_evidence import registered_candidate_panel_evidence

    series = {name: _result_series(row) for name, row in raw_results.items() if "returns" in row}
    accounting = {
        name: row["accounting"] for name, row in raw_results.items() if "accounting" in row
    }
    return registered_candidate_panel_evidence(scope, series, accounting_by_arm=accounting)


def _study_coverage(jobs, audit):
    counter = Counter(audit.get("states", {}).values())
    return {
        "planned_jobs": len(jobs),
        "completed": counter["completed"],
        "failed": counter["failed"],
        "interrupted": counter["interrupted"],
        "abstained": counter["abstained"],
        "pending": counter["planned"] + counter["started"],
    }


def _study_report_header(protocol):
    return {
        "schema": "paper-research-study/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "research": "not_evaluated",
        "external_evidence": "pending",
        "live_authorization": False,
        "admission_eligible": False,
        "historical_global_search_complete": False,
        "resource_enforcement": "one active worker; OS memory bound plus subprocess wall timeout",
        "source_manifest_sha256": protocol["source_manifest_sha256"],
        "source_snapshot": "source",
        "fixed_inputs_manifest": "fixed_inputs/manifest.json",
        "limitations": [
            "A job is not a trading authorization or a strategy result.",
            "Existing historic runners are not silently converted to prospective studies.",
            "Windows memory uses aggregate job private commit; POSIX uses per-worker virtual address space.",
        ],
    }


def _build_study_report(scope, protocol, jobs, output, study_started, fatal, stop_reason):
    audit = scope.audit()
    artifacts, raw_results, artifact_error = _study_result_artifacts(scope, output)
    fatal = artifact_error or fatal
    report = _study_report_header(protocol)
    report.update(
        engineering="pass"
        if not fatal and audit["status"] == "ok" and audit["results_complete"]
        else "fail",
        coverage=_study_coverage(jobs, audit),
        audit=audit,
        results=artifacts,
        raw_results=raw_results,
        component_statuses={
            name: {
                key: row.get(key)
                for key in ("status", "engineering", "research", "external_evidence", "reason")
            }
            for name, row in raw_results.items()
        },
        statistics=_study_statistics(scope, protocol, raw_results),
        stop_reason=stop_reason,
        study_wall_seconds=time.monotonic() - study_started,
        fatal_error=fatal,
    )
    accounted = sum(
        report["coverage"][key]
        for key in ("completed", "failed", "interrupted", "abstained", "pending")
    )
    if accounted != len(jobs):
        report["engineering"] = "fail"
        report["fatal_error"] = "incomplete job accounting"
    _write(output / f"report-{uuid4().hex}.json", report)
    return report


class _WindowsJob:
    def __init__(self, process, memory_mb):
        import ctypes
        from ctypes import wintypes

        self.api = _windows_job_api()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = _windows_job_information(memory_mb)
        try:
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            if not self.api.AssignProcessToJobObject(
                self.handle, wintypes.HANDLE(int(process._handle))
            ):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except BaseException:
            self.close()
            raise

    def resume_suspended_process(self, process_id):
        """Popen closes its thread handle; find and resume the suspended PID.

        The launcher has not run yet, so its base interpreter cannot escape the
        Job Object through a pre-assignment process-creation race.
        """
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        api = self.api
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.OpenThread.restype = wintypes.HANDLE
        api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "thread snapshot failed")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == process_id:
                    thread = api.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise OSError(
                            ctypes.get_last_error(), "cannot open suspended worker thread"
                        )
                    try:
                        if api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise OSError(ctypes.get_last_error(), "cannot resume worker thread")
                    finally:
                        api.CloseHandle(thread)
                    return
                more = api.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("suspended worker has no primary thread")
        finally:
            api.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_bounded_job(
    job, *, output, source_root=ROOT, wall_seconds=60, memory_mb=512, typed_inputs=None
):
    """Enforce wall time plus OS memory bound before imports and task execution."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if wall_seconds <= 0 or memory_mb <= 0:
        raise ValueError("positive wall and memory budgets required")
    result_path = (output / "result.json").resolve()
    request = _worker_request(job, source_root, result_path, memory_mb, typed_inputs)
    started = time.monotonic()
    thread_limits = {
        name: "1"
        for name in (
            "OPENBLAS_NUM_THREADS",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        )
    }
    outcome = _worker_outcome(wall_seconds, memory_mb, thread_limits)
    worker_env = {**os.environ, **thread_limits}
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        process = _spawn_worker(source_root, worker_env, stdout, stderr)
        _communicate_worker(process, request, result_path, outcome, wall_seconds, memory_mb)
    outcome["wall_seconds"] = time.monotonic() - started
    _write(
        output / "process.json", {key: value for key, value in outcome.items() if key != "result"}
    )
    return outcome


def _validate_jobs(protocol, jobs, source_root, source_hashes):
    jobs = _strict_json(jobs)
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("complete nonempty job table required")
    declared = list(dict.fromkeys(protocol["candidate_ids"] + protocol["benchmark_ids"]))
    ids = [job.get("id") for job in jobs]
    if len(ids) != len(set(ids)) or set(ids) != set(declared):
        raise ValueError("job IDs must match every declared candidate and benchmark exactly once")
    # The entire job table is part of the signed study declaration.
    if protocol.get("job_table_sha256") != _digest(jobs):
        raise ValueError("job table differs from preregistered contract")
    for job in jobs:
        kind = job.get("kind")
        if kind not in {"account_fixture", "json_summary", "python_callable"}:
            raise ValueError("unsupported job adapter")
        if kind == "python_callable":
            name = job.get("callable", "")
            if ":" not in name:
                raise ValueError("explicit module:function needed")
            module, qualified = name.split(":", 1)
            relative = module.replace(".", "/") + ".py"
            if (
                not qualified
                or relative not in source_hashes
                or not (Path(source_root) / relative).is_file()
            ):
                raise ValueError("callable module must be a byte-bound local source")
        if kind == "json_summary" and (
            not isinstance(job.get("values"), list) or not job["values"]
        ):
            raise ValueError("json_summary needs declared values")
    if any(job.get("input_bindings") for job in jobs):
        for name, expected in typed_transport_runtime_sources().items():
            if source_hashes.get(name) != expected:
                raise ValueError(
                    "actual typed decoder runtime must be byte-bound in the source closure: " + name
                )
    worker = "scripts/paper_research_worker.py"
    if worker not in source_hashes or source_hashes[worker] != sha256_file(ROOT / worker):
        raise ValueError("actual worker bootstrap must be byte-bound in the source closure")
    return jobs


def _run_study_locked(
    *, protocol, jobs, data_map, output, source_root=ROOT, source_paths=None, resume=False
):
    """Run the fixed complete table; recovery preserves failed attempts and files."""
    protocol = load_protocol(protocol)
    protocol.setdefault("data_identity_version", "exact/v2")
    source_root = Path(source_root).resolve()
    source_paths = None if source_paths is None else tuple(source_paths)
    sources = source_identity(source_root, paths=source_paths)
    jobs = _validate_jobs(protocol, jobs, source_root, sources)
    validate_job_input_bindings(
        jobs, data_map, data_identity_version=protocol["data_identity_version"]
    )
    output = Path(output)
    scope = _prepare_study_scope(
        protocol, jobs, data_map, output, source_root, source_paths, sources, resume
    )
    study_started = time.monotonic()
    fatal, stop_reason = _execute_study_jobs(
        scope, protocol, jobs, data_map, output, source_root, sources, study_started
    )
    return _build_study_report(scope, protocol, jobs, output, study_started, fatal, stop_reason)


def _result_series(row):
    import pandas as pd

    spec = row["returns"]
    return pd.Series(
        spec["values"], index=pd.to_datetime(spec["timestamps"], utc=True), dtype=float
    )


def _result_bindings(row):
    if "returns" not in row and "accounting" not in row:
        return {}
    if "returns" not in row or "accounting" not in row:
        raise ValueError("returns and accounting must be supplied together")
    from analysis.research_evidence import panel_result_binding

    return panel_result_binding(_result_series(row), row["accounting"])


def _snapshot_study_inputs(output, source_root, sources, data_map, data_identity_version):
    import pandas as pd

    snapshot = output / "source"
    snapshot.mkdir()
    for name, expected in sources.items():
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / name, destination)
        if sha256_file(destination) != expected:
            raise ValueError("source changed during snapshot: " + name)
    fixed = output / "fixed_inputs"
    fixed.mkdir()
    manifest = {}
    logical = data_manifest(data_map, version=data_identity_version)
    for number, (name, value) in enumerate(data_map.items()):
        typed = isinstance(value, (pd.DataFrame, pd.Series))
        filename = f"{number:04d}.pkl" if typed else f"{number:04d}.json"
        raw = (
            pickle.dumps(value, protocol=5)
            if typed
            else canonical_json(_strict_json(value)).encode("utf-8")
        )
        (fixed / filename).write_bytes(raw)
        manifest[name] = {
            "kind": "trusted_local_pandas_archive" if typed else "json",
            "path": filename,
            "wire_sha256": sha256_file(fixed / filename),
            "data_sha256": logical[name],
        }
    transport = write_typed_data_manifest(data_map, output / "worker_inputs") if data_map else None
    _write(
        fixed / "manifest.json",
        {
            "schema": "paper-study-fixed-inputs/v1",
            "worker_inputs_manifest_sha256": None
            if transport is None
            else transport["manifest_sha256"],
            "data_identity_version": data_identity_version,
            "inputs": manifest,
            "restoration": "Runner never unpickles automatically. Resume accepts original exact API inputs or a safe JSON/CSV typed CLI manifest.",
        },
    )


def _verify_study_snapshots(scope, output):
    for name, expected in scope.run.registration["source_hashes"].items():
        path = (output / "source" / name).resolve()
        if (
            not path.is_relative_to((output / "source").resolve())
            or not path.is_file()
            or sha256_file(path) != expected
        ):
            raise ValueError("frozen source snapshot changed or is incomplete")
    fixed = output / "fixed_inputs"
    manifest = _load_json((fixed / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["inputs"]
    transport_hash = manifest.get("worker_inputs_manifest_sha256")
    if transport_hash is not None:
        transport = verify_typed_data_snapshot(scope._data_map, output / "worker_inputs")
        if transport["manifest_sha256"] != transport_hash:
            raise ValueError("safe worker input manifest changed")
    if manifest["data_identity_version"] != scope.run.registration.get(
        "data_identity_version", "legacy/v1"
    ):
        raise ValueError("fixed input identity version changed")
    if {name: row["data_sha256"] for name, row in entries.items()} != scope.run.registration[
        "data_hashes"
    ]:
        raise ValueError("fixed input logical identities changed")
    for row in entries.values():
        path = (fixed / row["path"]).resolve()
        if (
            not path.is_relative_to(fixed.resolve())
            or not path.is_file()
            or sha256_file(path) != row["wire_sha256"]
        ):
            raise ValueError("frozen fixed input bytes changed or missing")


def _completed_artifacts(scope, output):
    for event in scope.run.events:
        if event.get("phase") != "run" or "process_artifact" not in event:
            continue
        path = (output / event["process_artifact"]).resolve()
        if not path.is_relative_to(output.resolve()):
            raise ValueError("completed artifact escapes study")
        result = path.with_name("result.json")
        if event.get("status") != "completed":
            if not path.is_file() or sha256_file(path) != event["process_sha256"]:
                raise ValueError("failed/interrupted process evidence missing or changed")
            continue
        if (
            not path.is_file()
            or sha256_file(path) != event["process_sha256"]
            or not result.is_file()
            or sha256_file(result) != event["result_sha256"]
        ):
            raise ValueError(
                "completed result/process missing or changed; recovery cannot rerun it"
            )


@contextmanager
def _study_writer(output):
    # Stable lock inode: OS releases it on crash; never unlink while another
    # process may be waiting on the same descriptor/inode.
    with (output / ".study_writer.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another study writer is active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run_study(
    *, protocol, jobs, data_map, output, source_root=ROOT, source_paths=None, resume=False
):
    """Serialize the whole study; OS locks are released automatically on crash."""
    output = Path(output)
    if resume:
        if not output.is_dir():
            raise FileNotFoundError("resume needs an existing study")
    else:
        output.mkdir(parents=True, exist_ok=False)
    with _study_writer(output):
        return _run_study_locked(
            protocol=protocol,
            jobs=jobs,
            data_map=data_map,
            output=output,
            source_root=source_root,
            source_paths=source_paths,
            resume=resume,
        )
