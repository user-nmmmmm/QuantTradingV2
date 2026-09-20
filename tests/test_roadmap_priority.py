"""Section-7 priority and acceptance-dependency checks using synthetic tasks."""
from copy import deepcopy

import pytest

from scripts.roadmap_priority import STATUSES, validate_tasks


def task(task_id="SYS-01", *, dependencies=(), priority="P1", batch="B0", status="待闭环"):
    return {
        "id": task_id,
        "kind": "capability",
        "priority": priority,
        "batch": batch,
        "status": status,
        "dependencies": list(dependencies),
        "contract": "Preserve the approved accounting contract.",
        "compatibility": "Existing facts remain readable.",
        "acceptance": ["Reconcile fixed inputs and reject inconsistent facts."],
        "sources": ["docs/example_contract.md"],
    }


def test_dependency_readiness_does_not_complete_or_authorize_a_task():
    tasks = [task("SYS-01", status="已验收"), task("SYS-02", dependencies=["SYS-01"])]
    original = deepcopy(tasks)
    report = validate_tasks(tasks)
    assert report["ready_for_acceptance"] == ["SYS-02"]
    assert report["open_by_priority"]["P1"] == ["SYS-02"]
    assert report["formal_freeze_blockers"] == ["SYS-02"]
    assert report["long_run_blockers"] == ["SYS-02"]
    assert report["blocked_tasks"] == {}
    assert report["live_authorization"] is False
    assert tasks == original


def test_dag_order_is_deterministic_and_priorities_do_not_override_dependencies():
    tasks = [
        task("SYS-04", priority="P0", dependencies=["SYS-03"]),
        task("SYS-02", priority="P1", batch="B2"),
        task("SYS-03", priority="P2", batch="B8"),
        task("SYS-01", priority="P1", batch="B2"),
        task("SYS-05", priority="P3", batch="BX", dependencies=["SYS-04", "SYS-03"]),
    ]
    report = validate_tasks(tasks)
    assert report["task_order"] == ["SYS-01", "SYS-02", "SYS-03", "SYS-04", "SYS-05"]
    assert report["blocked_tasks"] == {
        "SYS-04": ["SYS-03"], "SYS-05": ["SYS-03", "SYS-04"],
    }
    assert report["formal_freeze_blockers"] == ["SYS-01", "SYS-02", "SYS-04"]
    permuted = deepcopy(list(reversed(tasks)))
    permuted[0]["dependencies"].reverse()
    assert validate_tasks(permuted) == report


def test_unrelated_p0_does_not_block_parallel_development_or_extension_readiness():
    report = validate_tasks([
        task("SYS-01", priority="P0"),
        task("SYS-02", priority="P1", batch="B6"),
        task("SYS-03", priority="P3", batch="BX"),
    ])
    assert report["ready_for_acceptance"] == ["SYS-01", "SYS-02", "SYS-03"]
    assert report["blocked_tasks"] == {}
    assert report["formal_freeze_blockers"] == ["SYS-01", "SYS-02"]


def test_ready_queue_prioritizes_p0_after_its_lower_priority_dependencies_are_accepted():
    report = validate_tasks([
        task("SYS-01", priority="P0", dependencies=["SYS-02"]),
        task("SYS-02", priority="P2", status="已验收"),
        task("SYS-03", priority="P1"),
    ])
    assert report["task_order"] == ["SYS-03", "SYS-02", "SYS-01"]
    assert report["ready_for_acceptance"] == ["SYS-01", "SYS-03"]


def test_blockers_are_all_transitive_unaccepted_dependencies_without_duplicates():
    report = validate_tasks([
        task("SYS-01", status="已验收"),
        task("SYS-02", dependencies=["SYS-01"]),
        task("SYS-03", dependencies=["SYS-02"]),
        task("SYS-04", dependencies=["SYS-02"]),
        task("SYS-05", dependencies=["SYS-03", "SYS-04", "SYS-01"]),
    ])
    assert report["ready_for_acceptance"] == ["SYS-02"]
    assert report["blocked_tasks"]["SYS-05"] == ["SYS-02", "SYS-03", "SYS-04"]
    assert "SYS-01" not in report["blocked_tasks"]


@pytest.mark.parametrize("status", STATUSES)
def test_only_accepted_status_counts_as_complete(status):
    report = validate_tasks([task(status=status)])
    expected = [] if status == "已验收" else ["SYS-01"]
    assert report["ready_for_acceptance"] == expected
    assert report["formal_freeze_blockers"] == expected
    assert report["live_authorization"] is False


def test_p2_p3_do_not_create_unrelated_formal_freeze_blockers():
    report = validate_tasks([
        task("SYS-01", priority="P2"), task("SYS-02", priority="P3", batch="BX"),
    ])
    assert report["formal_freeze_blockers"] == report["long_run_blockers"] == []
    assert report["ready_for_acceptance"] == ["SYS-01", "SYS-02"]
    assert report["live_authorization"] is False


@pytest.mark.parametrize("bad", [None, {}, [], [None], ["SYS-01"]])
def test_malformed_task_collection_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_tasks(bad)


@pytest.mark.parametrize("bad_id", [None, 1, "", "sys-01", "SYS-1", "SYS-01 ", "SYS-01\n"])
def test_malformed_task_identity_is_rejected(bad_id):
    with pytest.raises(ValueError, match="malformed task ID"):
        validate_tasks([task(bad_id)])


def test_duplicate_task_identity_is_rejected():
    with pytest.raises(ValueError, match="duplicate task ID"):
        validate_tasks([task(), task()])


@pytest.mark.parametrize("field,value", [
    ("priority", "P4"), ("priority", None), ("priority", []),
    ("batch", "B9"), ("batch", "b0"), ("status", "pass"), ("status", True),
    ("kind", "unknown"), ("kind", None),
])
def test_unsupported_metadata_is_rejected(field, value):
    row = task()
    row[field] = value
    with pytest.raises(ValueError, match=f"unsupported {field}"):
        validate_tasks([row])


def test_extension_must_use_an_independent_branch():
    with pytest.raises(ValueError, match="P3.*BX"):
        validate_tasks([task(priority="P3", batch="B5")])


@pytest.mark.parametrize("field", ["contract", "compatibility", "acceptance", "sources"])
@pytest.mark.parametrize("bad_value", [None, "", " ", [], [""], [None], {}])
def test_missing_or_empty_completion_metadata_is_rejected(field, bad_value):
    row = task()
    row[field] = bad_value
    with pytest.raises(ValueError, match=field):
        validate_tasks([row])
    del row[field]
    with pytest.raises(ValueError, match=field):
        validate_tasks([row])


@pytest.mark.parametrize("dependencies", [None, "SYS-02", {}, [None], [""], ["SYS-2"]])
def test_malformed_dependencies_are_rejected(dependencies):
    row = task()
    row["dependencies"] = dependencies
    with pytest.raises(ValueError, match="dependenc"):
        validate_tasks([row])


def test_dependencies_must_be_explicit_even_when_empty():
    row = task()
    del row["dependencies"]
    with pytest.raises(ValueError, match="dependencies"):
        validate_tasks([row])


@pytest.mark.parametrize("dependencies,error", [
    (["SYS-01"], "self dependency"),
    (["SYS-02", "SYS-02"], "duplicate dependency"),
    (["SYS-99"], "unknown dependency"),
])
def test_invalid_dependency_relationships_are_rejected(dependencies, error):
    with pytest.raises(ValueError, match=error):
        validate_tasks([task(dependencies=dependencies), task("SYS-02")])


def test_cycle_is_rejected_even_with_an_independent_accepted_task():
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_tasks([
            task("DOC-01", status="已验收"),
            task("SYS-01", dependencies=["SYS-03"]),
            task("SYS-02", dependencies=["SYS-01"]),
            task("SYS-03", dependencies=["SYS-02"]),
        ])


def test_accepted_task_cannot_hide_unaccepted_dependency():
    with pytest.raises(ValueError, match="accepted task has unaccepted dependencies: SYS-01"):
        validate_tasks([
            task("SYS-01", status="待证据"),
            task("SYS-02", status="已验收", dependencies=["SYS-01"]),
            task("SYS-03", status="已验收", dependencies=["SYS-02"]),
        ])
