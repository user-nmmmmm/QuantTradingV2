"""Validate roadmap task metadata and acceptance dependencies without changing state.

Readiness here means only that dependencies have been accepted. It neither validates
the task's own evidence nor authorizes a research freeze, long run or live trading.
"""
from __future__ import annotations

import heapq
import re


PRIORITIES = ("P0", "P1", "P2", "P3")
BATCHES = tuple(f"B{number}" for number in range(9)) + ("BX",)
STATUSES = (
    "待闭环", "部分实现待验收", "待验证", "待证据", "后续扩展", "已验收",
)
KINDS = ("documentation", "capability", "fix", "verification")
ACCEPTED = "已验收"
TASK_ID = re.compile(r"(?:DOC|SYS|FIX|VER)-[0-9]{2,}\Z")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(_text(item) for item in value)


def validate_tasks(tasks: list[dict]) -> dict:
    """Return a deterministic dependency and priority report, or raise ValueError.

    ``task_order`` is a topological order of all IDs. Among available tasks it uses
    priority, batch, then ID. Ready tasks also use that priority order; other ID
    lists preserve the topological order. Batches are
    organizational labels, never implicit dependencies. An unrelated P0 task does
    not prohibit work on a dependency-ready P1/P2/P3 task.

    ``ready_for_acceptance`` contains open tasks whose prerequisites are accepted;
    those tasks still require their own completion evidence. ``blocked_tasks`` maps
    each other open task to all its unaccepted transitive prerequisites. Formal
    freeze and long-run blockers are every open P0/P1 task; clearing these lists is
    a necessary priority condition only. This function grants no live authorization.

    Evidence fields are checked for nonempty metadata, not artifact correctness.
    The input is not mutated, and no files, network or process state are consulted.
    """
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a nonempty list of task objects")

    by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None:
            raise ValueError(f"malformed task ID: {task_id!r}")
        if task_id in by_id:
            raise ValueError(f"duplicate task ID: {task_id}")
        for field, allowed in (
            ("kind", KINDS), ("priority", PRIORITIES), ("batch", BATCHES),
            ("status", STATUSES),
        ):
            if task.get(field) not in allowed:
                raise ValueError(f"{task_id}: unsupported {field}: {task.get(field)!r}")
        if task["priority"] == "P3" and task["batch"] != "BX":
            raise ValueError(f"{task_id}: P3 must use independent branch BX")
        for field in ("contract", "compatibility"):
            if not _text(task.get(field)):
                raise ValueError(f"{task_id}: {field} must be nonempty text")
        for field in ("acceptance", "sources"):
            if not _text_list(task.get(field)):
                raise ValueError(f"{task_id}: {field} must be a nonempty list of text")
        dependencies = task.get("dependencies")
        if not isinstance(dependencies, list):
            raise ValueError(f"{task_id}: dependencies must be an explicit list")
        seen_dependencies: set[str] = set()
        for dependency in dependencies:
            if not isinstance(dependency, str) or TASK_ID.fullmatch(dependency) is None:
                raise ValueError(f"{task_id}: malformed dependency ID: {dependency!r}")
            if dependency == task_id:
                raise ValueError(f"{task_id}: self dependency")
            if dependency in seen_dependencies:
                raise ValueError(f"{task_id}: duplicate dependency: {dependency}")
            seen_dependencies.add(dependency)
        by_id[task_id] = task

    def rank(task_id: str) -> tuple[int, int, str]:
        task = by_id[task_id]
        return PRIORITIES.index(task["priority"]), BATCHES.index(task["batch"]), task_id

    dependents: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    remaining: dict[str, int] = {}
    for task_id in sorted(by_id):
        task = by_id[task_id]
        remaining[task_id] = len(task["dependencies"])
        for dependency in sorted(task["dependencies"]):
            if dependency not in by_id:
                raise ValueError(f"{task_id}: unknown dependency: {dependency}")
            dependents[dependency].append(task_id)

    available = [rank(task_id) for task_id, count in remaining.items() if count == 0]
    heapq.heapify(available)
    task_order: list[str] = []
    while available:
        _, _, task_id = heapq.heappop(available)
        task_order.append(task_id)
        for dependent in dependents[task_id]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(available, rank(dependent))
    if len(task_order) != len(by_id):
        unresolved = sorted(task_id for task_id, count in remaining.items() if count)
        raise ValueError("dependency cycle prevents ordering: " + ", ".join(unresolved))

    unmet_by_id: dict[str, set[str]] = {}
    for task_id in task_order:
        task = by_id[task_id]
        unmet: set[str] = set()
        for dependency in task["dependencies"]:
            unmet.update(unmet_by_id[dependency])
            if by_id[dependency]["status"] != ACCEPTED:
                unmet.add(dependency)
        if task["status"] == ACCEPTED and unmet:
            raise ValueError(
                f"{task_id}: accepted task has unaccepted dependencies: "
                + ", ".join(sorted(unmet))
            )
        unmet_by_id[task_id] = unmet

    positions = {task_id: index for index, task_id in enumerate(task_order)}
    open_by_priority: dict[str, list[str]] = {priority: [] for priority in PRIORITIES}
    ready_for_acceptance: list[str] = []
    blocked_tasks: dict[str, list[str]] = {}
    blockers: list[str] = []
    for task_id in task_order:
        task = by_id[task_id]
        if task["status"] == ACCEPTED:
            continue
        open_by_priority[task["priority"]].append(task_id)
        if unmet_by_id[task_id]:
            blocked_tasks[task_id] = sorted(unmet_by_id[task_id], key=positions.__getitem__)
        else:
            ready_for_acceptance.append(task_id)
        if task["priority"] in ("P0", "P1"):
            blockers.append(task_id)

    return {
        "task_order": task_order,
        "open_by_priority": open_by_priority,
        "ready_for_acceptance": sorted(ready_for_acceptance, key=rank),
        "blocked_tasks": blocked_tasks,
        "formal_freeze_blockers": blockers,
        "long_run_blockers": list(blockers),
        "live_authorization": False,
    }
