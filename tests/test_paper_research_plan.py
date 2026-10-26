"""Structure checks for the paper research plan documents and task registry (T0: documents only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/research/paper_research_task_registry_20261025.json"
DATA = json.loads(REGISTRY.read_text(encoding="utf-8"))
TASKS = {task["id"]: task for task in DATA["tasks"]}


def test_registry_hashes_match_the_plan_documents():
    assert "LF" in DATA["source_documents_sha256_policy"]
    for doc in DATA["source_documents"]:
        raw = (ROOT / doc["path"]).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == doc["sha256"], doc["path"]


def test_task_ids_are_the_23_work_packages_and_dependencies_are_acyclic():
    assert sorted(TASKS) == [f"PR25-{n:02d}" for n in range(23)] and len(DATA["tasks"]) == 23
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(task_id: str) -> None:
        assert task_id in TASKS, f"unknown dependency {task_id}"
        assert task_id not in visiting, f"dependency cycle at {task_id}"
        if task_id in done:
            return
        visiting.add(task_id)
        for dep in TASKS[task_id]["depends_on"]:
            visit(dep)
        visiting.discard(task_id)
        done.add(task_id)

    for task_id in TASKS:
        visit(task_id)


def test_every_task_has_a_card_heading_in_the_development_plan():
    plan = (ROOT / "docs/research/paper_research_development_plan_20261025.md").read_text(encoding="utf-8")
    for task in DATA["tasks"]:
        assert task["development_card_reference"]["heading"] in plan, task["id"]


def test_existing_paths_in_the_registry_exist_in_this_checkout():
    missing = [(task["id"], path) for task in DATA["tasks"] for path in task["existing_paths"] + task["existing_test_paths"]
               if not (ROOT / path).exists()]
    assert missing == []


def test_registry_does_not_claim_execution_or_acceptance():
    assert DATA["execution_started"] is False and DATA["live_authorization"] is False
    assert all(task["acceptance_evidence"] is None or task["planning_status"] != "planned" for task in DATA["tasks"])
