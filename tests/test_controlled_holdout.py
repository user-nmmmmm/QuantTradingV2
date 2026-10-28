"""Actual-read single-consumption, recovery and prospective clock counterexamples."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import importlib.util
import sys
from uuid import uuid4
import threading

import pandas as pd
import pytest

from analysis import controlled_holdout as module
from analysis.controlled_holdout import (
    AtomicHoldoutVault,
    adjudicate_candidate,
    freeze_candidate_contract,
    observation_coverage,
)
from analysis.research_validation import HoldoutProtocol, HoldoutVault
from core.foundation.reproducibility import sha256_file


def _score(data):
    return {
        "research": "not_supported",
        "metric": float(data.holdout.value.sum()),
        "scored_timestamps": [t.isoformat() for t in data.holdout.index],
        "warmup_has_label": "label" in data.warmup.columns,
    }


_FAIL = False


def _recoverable_score(data):
    if _FAIL:
        raise RuntimeError("deterministic simulated interruption")
    return _score(data)


@pytest.fixture
def holdout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "model.bin").write_bytes(b"frozen-model")
    (source / "config.json").write_text('{"threshold":1}', encoding="utf-8")
    csv = tmp_path / "input.csv"
    pd.DataFrame(
        {"value": range(1, 11), "label": range(11, 21)},
        index=pd.date_range("2020-01-01", periods=10, tz="UTC"),
    ).to_csv(csv)
    protocol = HoldoutProtocol(
        "synthetic-v2",
        "2020-01-01",
        "2020-01-04",
        "2020-01-05",
        "2020-01-07",
        "2020-01-08",
        "2020-01-10",
    )
    name = "frozen_eval_" + uuid4().hex
    callback_path = source / (name + ".py")
    callback_path.write_text(
        'def score(data, threshold=1):\n    return {"research": "not_supported", "metric": float(data.holdout.value.sum()), "scored_timestamps": [t.isoformat() for t in data.holdout.index], "warmup_has_label": "label" in data.warmup.columns}\n',
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(name, callback_path)
    callback_module = importlib.util.module_from_spec(spec)
    sys.modules[name] = callback_module
    spec.loader.exec_module(callback_module)
    vault = AtomicHoldoutVault(tmp_path / "access.sqlite3")
    vault.test_evaluator = callback_module.score
    return vault, protocol, csv, source


def register(fixture, evaluator=_score):
    vault, protocol, csv, source = fixture
    evaluator = vault.test_evaluator
    vault.register(
        protocol,
        data_path=csv,
        source_root=source,
        source_paths=["."],
        evaluator_id=f"{evaluator.__module__}:{evaluator.__qualname__}",
        label_columns=["label"],
        warmup_rows=2,
    )
    vault.freeze()
    return vault


def test_actual_read_gate_single_consumption_and_feature_only_warmup(holdout):
    vault = register(holdout)
    result = vault.evaluate(evaluation_id="fixed-evaluation", evaluator=vault.test_evaluator)
    assert result["status"] == "completed"
    assert result["result"]["metric"] == 27
    assert not result["result"]["warmup_has_label"]
    assert len(result["result"]["scored_timestamps"]) == 3
    assert not result["live_authorization"]
    with pytest.raises(RuntimeError, match="consumed"):
        vault.evaluate(
            evaluation_id="fixed-evaluation", evaluator=vault.test_evaluator, resume=True
        )
    with pytest.raises(RuntimeError):
        vault.register(
            holdout[1],
            data_path=holdout[2],
            source_root=holdout[3],
            source_paths=["model.bin"],
            evaluator_id="x:y",
        )


def test_legacy_vault_exposes_separate_v2_gate_without_rewriting_v1(tmp_path):
    legacy = HoldoutVault(tmp_path / "legacy.json")
    assert legacy.controlled_access().path.name == "legacy.json.access.sqlite3"
    assert not legacy.path.exists()


@pytest.mark.parametrize("mutation", ["model", "config", "data"])
def test_model_configuration_and_actual_input_drift_refuse_reads(holdout, mutation):
    vault = register(holdout)
    path = (
        holdout[2]
        if mutation == "data"
        else holdout[3] / ("model.bin" if mutation == "model" else "config.json")
    )
    path.write_bytes(b"changed bytes")
    with pytest.raises(PermissionError, match="identity changed"):
        vault.evaluate(evaluation_id="x", evaluator=vault.test_evaluator)
    assert vault.inspect()["status"] == "frozen"


def test_unmatured_or_unknown_purpose_and_future_clock_do_not_invoke_evaluator(holdout):
    vault = register(holdout)
    for kwargs in ({"purpose": "please tune"}, {"now": "2020-01-09T00:00:00Z"}):
        with pytest.raises(PermissionError):
            vault.evaluate(evaluation_id="x", evaluator=vault.test_evaluator, **kwargs)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future clock"):
        vault.evaluate(evaluation_id="x", evaluator=vault.test_evaluator, now=future)
    assert vault.inspect()["status"] == "frozen"


def test_failed_attempt_remains_consumed_and_same_id_recovery_preserves_history(
    holdout, monkeypatch
):
    vault = register(holdout)
    read_csv = module.pd.read_csv

    def failure(*args, **kwargs):
        raise RuntimeError("deterministic simulated interruption")

    monkeypatch.setattr(module.pd, "read_csv", failure)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        vault.evaluate(evaluation_id="fixed", evaluator=vault.test_evaluator)
    state = vault.inspect()
    assert state["status"] == "failed_consumed" and len(state["failures"]) == 1
    with pytest.raises(RuntimeError, match="same-ID"):
        vault.evaluate(evaluation_id="new-attempt", evaluator=vault.test_evaluator, resume=True)
    monkeypatch.setattr(module.pd, "read_csv", read_csv)
    result = vault.evaluate(evaluation_id="fixed", evaluator=vault.test_evaluator, resume=True)
    assert result["status"] == "completed" and len(result["failures"]) == 1
    assert result["access_event_count"] == 6


def test_only_one_simultaneous_reader_can_obtain_the_evaluation_lease(holdout, monkeypatch):
    vault = register(holdout)
    entered, finish = threading.Event(), threading.Event()
    original = module.sha256_file

    def block(path):
        if entered.is_set():
            return original(path)
        entered.set()
        finish.wait(3)
        return original(path)

    monkeypatch.setattr(module, "sha256_file", block)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(vault.evaluate, evaluation_id="first", evaluator=vault.test_evaluator)
        assert entered.wait(3)
        second = pool.submit(vault.evaluate, evaluation_id="second", evaluator=vault.test_evaluator)
        # Second waits for transaction then must observe live lease/completed state.
        finish.set()
        result = first.result(timeout=10)
        assert result["status"] == "completed"
        with pytest.raises(RuntimeError):
            second.result(timeout=10)


def test_damaged_log_or_missing_tail_refuses_existing_state(holdout):
    vault = register(holdout)
    import sqlite3

    with sqlite3.connect(vault.path) as db:
        db.execute("DELETE FROM access_log WHERE sequence=1")
    with pytest.raises(ValueError, match="truncated"):
        vault.inspect()


def _candidate(holdout, now):
    _, protocol, _, source = holdout
    start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    return {
        "candidate_id": "new",
        "parent_candidate_id": "old",
        "research": "supported",
        "model_sha256": sha256_file(source / "model.bin"),
        "config_sha256": sha256_file(source / "config.json"),
        "data_policy_id": "strict/local",
        "benchmark_ids": ["cash"],
        "risk_policy_id": "fixed-risk",
        "cost_policy_id": "fixed-fees",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "required_market_states": ["trend", "range"],
        "minimum_observation_days": 2,
        "access_policy_id": "single-use",
        "stop_rules": ["stop on drift"],
        "adjudication_rule_id": "metric-v1",
        "holdout_protocol_hash": protocol.fingerprint,
    }


def test_no_support_abstains_without_creating_new_candidate(holdout, tmp_path):
    now = datetime.now(timezone.utc)
    contract = _candidate(holdout, now)
    contract["research"] = "not_supported"
    result = freeze_candidate_contract(
        contract, output=tmp_path / "not-created.json", source_root=holdout[3], source_paths=["."]
    )
    assert result["status"] == "abstained" and not (tmp_path / "not-created.json").exists()


def test_new_candidate_cannot_inherit_old_days_or_backdate_and_freeze_is_immutable(
    holdout, tmp_path
):
    now = datetime.now(timezone.utc)
    contract = _candidate(holdout, now)
    path = tmp_path / "new.json"
    result = freeze_candidate_contract(
        contract, output=path, source_root=holdout[3], source_paths=["."]
    )
    assert result["inherited_observation_days"] == 0
    with pytest.raises(FileExistsError):
        freeze_candidate_contract(contract, output=path, source_root=holdout[3], source_paths=["."])
    with pytest.raises(ValueError, match="backdated"):
        freeze_candidate_contract(
            contract,
            output=tmp_path / "bad.json",
            source_root=holdout[3],
            source_paths=["."],
            now="2020-01-01T00:00:00Z",
        )
    with pytest.raises(PermissionError, match="contract changed"):
        altered = {**result, "minimum_observation_days": 1}
        observation_coverage(altered, [])


def test_before_due_only_coverage_is_checked_no_evaluator_invocation(holdout, tmp_path):
    vault = register(holdout)
    contract = _candidate(holdout, datetime.now(timezone.utc))
    path = tmp_path / "new.json"
    freeze_candidate_contract(contract, output=path, source_root=holdout[3], source_paths=["."])

    def forbidden(data):
        raise AssertionError("must not read returns before observations mature")

    result = adjudicate_candidate(path, [], vault=vault, evaluation_id="x", evaluator=forbidden)
    assert result["status"] == "pending" and not result["performance_accessed"]
    assert vault.inspect()["status"] == "frozen"


def test_observation_raw_hash_duplicates_day_gaps_and_maturity(holdout, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path = tmp_path / "candidate.json"
    contract = _candidate(holdout, now)
    frozen = freeze_candidate_contract(
        contract, output=path, source_root=holdout[3], source_paths=["."]
    )
    as_of = now + timedelta(days=4)
    monkeypatch.setattr(module, "_now", lambda: as_of.isoformat())
    rows = []
    for sequence, day in enumerate((1, 2)):
        raw = tmp_path / f"raw-{day}.json"
        raw.write_text('{"market":1}')
        at = now + timedelta(days=day)
        rows.append(
            {
                "candidate_id": "new",
                "session_id": "s",
                "sequence": sequence,
                "event_at": at.isoformat(),
                "observed_at": (at + timedelta(seconds=1)).isoformat(),
                "raw_path": str(raw),
                "raw_sha256": sha256_file(raw),
                "market_state": "trend" if day == 1 else "range",
                "label_mature_at": (at + timedelta(days=1)).isoformat(),
                "cost_mature_at": (at + timedelta(hours=2)).isoformat(),
            }
        )
    coverage = observation_coverage(frozen, rows, as_of=as_of.isoformat())
    assert coverage["status"] == "complete"
    assert (
        observation_coverage(frozen, rows + rows[:1], as_of=as_of.isoformat())["status"]
        == "pending"
    )
    assert observation_coverage(frozen, rows[:1], as_of=as_of.isoformat())["status"] == "pending"
    (tmp_path / "raw-2.json").write_text("changed")
    assert observation_coverage(frozen, rows, as_of=as_of.isoformat())["status"] == "pending"


def test_legacy_closed_endpoints_preserve_end_minus_and_plus_one_nanosecond(holdout):
    vault, protocol, path, source = holdout
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True)
    end = pd.Timestamp("2020-01-10T00:00:00Z")
    frame.loc[end - pd.Timedelta(1, "ns")] = [50, 99]
    frame.loc[end + pd.Timedelta(1, "ns")] = [100, 100]
    frame.sort_index().to_csv(path)
    register(holdout)
    result = vault.evaluate(evaluation_id="boundary", evaluator=vault.test_evaluator)
    axis = result["result"]["scored_timestamps"]
    assert (end - pd.Timedelta(1, "ns")).isoformat() in axis
    assert end.isoformat() in axis
    assert (end + pd.Timedelta(1, "ns")).isoformat() not in axis
    assert result["interval_policy"] == "closed_both_endpoints_legacy_holdout_protocol"


def test_same_qualname_different_closure_or_defaults_cannot_consume(holdout):
    vault = register(holdout)
    original = vault.test_evaluator

    def factory(offset):
        def score(data):
            return offset

        return score

    imposter = factory(2)
    imposter.__module__ = original.__module__
    imposter.__qualname__ = original.__qualname__
    with pytest.raises(PermissionError, match="top-level|identity"):
        vault.evaluate(evaluation_id="imposter", evaluator=imposter)
    original.__defaults__ = (2,)
    with pytest.raises(PermissionError, match="identity"):
        vault.evaluate(evaluation_id="changed-default", evaluator=original)
    assert vault.inspect()["status"] == "frozen"


def test_callback_source_must_be_in_the_registered_closure(holdout):
    vault, protocol, path, source = holdout
    callback = vault.test_evaluator
    with pytest.raises(PermissionError, match="included in registered closure"):
        vault.register(
            protocol,
            data_path=path,
            source_root=source,
            source_paths=["model.bin", "config.json"],
            evaluator_id=f"{callback.__module__}:{callback.__qualname__}",
        )


def _install_waiting_callback(holdout, seconds):
    vault = holdout[0]
    original = vault.test_evaluator
    path = __import__("pathlib").Path(original.__code__.co_filename)
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\ndef wait_score(data):\n    import time\n    time.sleep({seconds})\n    return score(data)\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(original.__module__, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[original.__module__] = module
    spec.loader.exec_module(module)
    vault.test_evaluator = module.wait_score
    return module.wait_score


def _launch_final_process(holdout, identity):
    import os
    import subprocess

    source = holdout[3]
    callback = holdout[0].test_evaluator
    script = """import importlib,json,sys
from analysis.controlled_holdout import AtomicHoldoutVault
callback=getattr(importlib.import_module(sys.argv[2]),sys.argv[3])
try:
 result=AtomicHoldoutVault(sys.argv[1]).evaluate(evaluation_id=sys.argv[4],evaluator=callback)
 print(json.dumps({'status':result['status']}))
except (RuntimeError,PermissionError,ValueError) as error:
 print(json.dumps({'status':'blocked','error_type':type(error).__name__,'reason':str(error)}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(source), *(str(p) for p in sys.path if p)])
    env["OPENBLAS_NUM_THREADS"] = "1"
    # Base executable makes the Popen PID the actual evaluator PID on Windows,
    # so this test can terminate exactly its own child without leaving a launcher.
    return subprocess.Popen(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            script,
            str(holdout[0].path),
            callback.__module__,
            callback.__name__,
            identity,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def test_two_actual_processes_cannot_both_consume_final_data(holdout):
    _install_waiting_callback(holdout, 0.3)
    vault = register(holdout)
    processes = [
        _launch_final_process(holdout, "process-1"),
        _launch_final_process(holdout, "process-2"),
    ]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode(errors="replace")
            results.append(json.loads(stdout))
        assert sorted(row["status"] for row in results) == ["blocked", "completed"], results
        assert vault.inspect()["status"] == "completed"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_actual_process_crash_resumes_only_same_consumed_evaluation(holdout):
    import time

    _install_waiting_callback(holdout, 2)
    vault = register(holdout)
    process = _launch_final_process(holdout, "fixed-crash-id")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if vault.inspect()["status"] == "evaluating":
            break
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"evaluation subprocess exited before consumption: {process.returncode}; "
                        f"stdout={stdout.decode(errors='replace')}; stderr={stderr.decode(errors='replace')}")
        time.sleep(0.02)
    else:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail("evaluation subprocess did not obtain consumption state; "
                    f"stdout={stdout.decode(errors='replace')}; stderr={stderr.decode(errors='replace')}")
    process.kill()
    process.communicate(timeout=5)
    assert vault.inspect()["status"] == "evaluating"
    with pytest.raises(RuntimeError):
        vault.evaluate(evaluation_id="different-id", evaluator=vault.test_evaluator, resume=True)
    result = vault.evaluate(
        evaluation_id="fixed-crash-id", evaluator=vault.test_evaluator, resume=True
    )
    assert result["status"] == "completed"
    assert result["failures"][-1]["error_type"] == "ProcessInterrupted"


def test_whole_vault_one_nanosecond_not_before_refuses_access_and_evaluator_is_not_called(holdout):
    vault, protocol, data_path, source = holdout
    original = vault.test_evaluator
    path = __import__("pathlib").Path(original.__code__.co_filename)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\nCALLED = []\ndef guarded_score(data):\n    CALLED.append(True)\n    return score(data)\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(original.__module__, path)
    callback_module = importlib.util.module_from_spec(spec)
    sys.modules[original.__module__] = callback_module
    spec.loader.exec_module(callback_module)
    callback = callback_module.guarded_score
    end = pd.Timestamp("2020-01-10T00:00:00Z")
    not_before = end + pd.Timedelta(1, "ns")
    vault.register(
        protocol,
        data_path=data_path,
        source_root=source,
        source_paths=["."],
        evaluator_id=f"{callback.__module__}:{callback.__qualname__}",
        not_before=not_before.isoformat(),
    )
    vault.freeze()
    assert pd.Timestamp(vault.inspect()["not_before"]) == not_before
    with pytest.raises(PermissionError, match="not matured"):
        vault.evaluate(
            evaluation_id="cannot-open-one-ns-early", evaluator=callback, now=end.isoformat()
        )
    assert callback_module.CALLED == []
    assert vault.inspect()["status"] == "frozen"


def test_shared_protocol_timestamp_gate_keeps_one_nanosecond_windows_and_iso_validation():
    from analysis.search_scope import _timestamp, validate_protocol
    from pathlib import Path
    from copy import deepcopy

    report = Path(__file__).resolve().parents[1] / "config/paper_research_phase_a.json"
    value = deepcopy(json.loads(report.read_text(encoding="utf-8")))
    value.update(
        schema="paper-research-protocol/v1",
        study_id="one-ns-window",
        registered_at="2020-01-01T00:00:00Z",
        source_manifest_sha256="a" * 64,
        config_sha256="b" * 64,
        data_manifest_sha256="c" * 64,
    )
    value["evaluation_windows"] = [
        {
            "id": "one-ns",
            "role": "engineering_fixture",
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-01T00:00:00.000000001Z",
        }
    ]
    assert validate_protocol(value)["evaluation_windows"][0]["end"].endswith("000000001Z")
    assert _timestamp(value["evaluation_windows"][0]["end"], "end") > _timestamp(
        value["evaluation_windows"][0]["start"], "start"
    )
    assert _timestamp("2020-01-01", "day", date_allowed=True).tzinfo is not None
    for malformed in ("tomorrow", "January 1, 2020", "2020-01-01T00:00:00"):
        with pytest.raises(ValueError, match="ISO timestamp"):
            _timestamp(malformed, "invalid")


@pytest.mark.parametrize("referenced_helper", [False, True])
def test_incidental_code_object_references_do_not_change_frozen_evaluator(holdout, referenced_helper):
    if referenced_helper:
        _install_waiting_callback(holdout, 0)
    vault = register(holdout)
    callback = vault.test_evaluator
    code = (callback.__globals__["score"] if referenced_helper else callback).__code__
    # These are the same executable objects, with no changed source or values.
    # marshal's reference flags used to change merely because they were retained.
    retained = [code.co_consts, code.co_names, code.co_varnames, code.co_code, *code.co_consts]
    result = vault.evaluate(evaluation_id="retained-identical-code", evaluator=callback)
    assert result["status"] == "completed"
    assert retained[0] is code.co_consts


def test_real_code_constant_change_still_refuses_final_access(holdout):
    vault = register(holdout)
    callback = vault.test_evaluator
    callback.__code__ = callback.__code__.replace(co_consts=tuple(
        "supported" if value == "not_supported" else value for value in callback.__code__.co_consts))
    with pytest.raises(PermissionError, match="identity differs"):
        vault.evaluate(evaluation_id="changed-executable", evaluator=callback)
    assert vault.inspect()["status"] == "frozen"


def test_legacy_marshal_binding_remains_readable_but_never_upgrades_or_consumes(holdout):
    import marshal

    vault = register(holdout)
    with vault._db() as db:
        value = vault._read(db)
        binding = value["evaluator_binding"]
        binding.pop("code_identity_scheme")
        binding["code_sha256"] = module.sha256_bytes(marshal.dumps(vault.test_evaluator.__code__))
        vault._store(db, value, {"action": "legacy_marshaled_binding_fixture"})
    prior = vault.inspect()
    assert prior["status"] == "frozen" and "code_identity_scheme" not in prior["evaluator_binding"]
    with pytest.raises(PermissionError, match="identity differs"):
        vault.evaluate(evaluation_id="cannot-upgrade", evaluator=vault.test_evaluator)
    assert vault.inspect() == prior


def test_code_identity_preserves_constant_types_nested_code_and_unordered_values():
    def first():
        return {"a", "b"}, (1, True, 1.0, b"a", "a"), lambda: 1

    code = first.__code__
    assert module._code_value(True) != module._code_value(1)
    assert module._code_value(1) != module._code_value(1.0)
    assert module._code_value(b"a") != module._code_value("a")
    assert module._code_value(0.0) != module._code_value(-0.0)
    assert module._code_value(frozenset(["a", "b"])) == module._code_value(frozenset(["b", "a"]))
    nested = next(value for value in code.co_consts if isinstance(value, type(code)))
    changed = nested.replace(co_consts=(None, 2))
    outer = code.replace(co_consts=tuple(changed if value is nested else value for value in code.co_consts))
    assert module._code_identity(code) != module._code_identity(outer)
