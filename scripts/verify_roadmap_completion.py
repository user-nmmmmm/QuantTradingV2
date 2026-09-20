"""Verify roadmap section 7 without converting engineering evidence into admission.

Historical receipts remain immutable. Versioned envelopes supply explicit evidence
roles for their different schemas; the current registry remains the state authority.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.roadmap_priority import validate_tasks
from scripts.roadmap_evidence import verify_source_binding

REGISTRY = "docs/development_task_registry.json"
INDEX = "reports/roadmap_v3/acceptance_index.json"
MANIFEST = "docs/roadmap_completion_manifest.json"
DIMENSIONS = {"pass", "fail", "pending", "partial", "insufficient", "not_applicable",
              "pending_evidence"}
CATEGORIES = {"positive", "negative", "fault", "reconciliation", "integration",
              "links", "ids", "coverage", "hashes"}
WORKFLOW = {"current_state", "failure_sample", "implementation", "targeted_acceptance",
            "integration"}


def load_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=invalid)


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: nonempty text required")


def object_value(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: JSON object required")
    return value


def repository_path(root: Path, name: str) -> Path:
    text_value(name, "evidence path")
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in name:
        raise ValueError(f"outside repository: {name}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"outside repository: {name}")
    return resolved


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_reference(root: Path, ref: dict, *, structure_only=False) -> Path:
    if not isinstance(ref, dict) or not re.fullmatch("[0-9a-f]{64}", str(ref.get("sha256", ""))):
        raise ValueError("evidence reference requires path and lowercase SHA-256")
    path = repository_path(root, ref.get("path"))
    if not structure_only:
        if not path.is_file():
            raise ValueError(f"missing evidence: {ref['path']}")
        if file_digest(path) != ref["sha256"]:
            raise ValueError(f"evidence hash mismatch: {ref['path']}")
        if ref.get("anchor"):
            content = path.read_text(encoding="utf-8-sig")
            anchor = ref["anchor"]
            if not (f'id="{anchor}"' in content or f"## {anchor.upper()}" in content):
                raise ValueError(f"missing contract anchor: {ref['path']}#{anchor}")
    return path


def verify_junit(path: Path, allowed_skips: list[str]) -> dict:
    """A declared pass cannot conceal a failed, empty or silently skipped suite."""
    document = ET.parse(path)
    cases = list(document.iter("testcase"))
    if not cases:
        raise ValueError(f"empty JUnit evidence: {path.name}")
    if any(True for _ in document.iter("failure")) or any(True for _ in document.iter("error")):
        raise ValueError(f"failed JUnit evidence: {path.name}")
    for suite in document.iter("testsuite"):
        for attr in ("failures", "errors"):
            if int(suite.get(attr, "0")) != 0:
                raise ValueError(f"failed JUnit suite: {path.name}")
    skipped_cases = [case for case in cases if case.find("skipped") is not None]
    skipped = {f"{case.get('classname', '')}::{case.get('name', '')}" for case in skipped_cases}
    if skipped - set(allowed_skips):
        raise ValueError(f"undeclared skipped JUnit evidence: {sorted(skipped)}")
    if len(skipped_cases) == len(cases):
        raise ValueError(f"no passed JUnit cases: {path.name}")
    return {"cases": len(cases), "skipped_count": len(skipped_cases), "skipped": sorted(skipped)}


def verify_result_checks(checks) -> None:
    """Explicit adapters for status checks and legacy process/quality exit codes."""
    if not isinstance(checks, list) or not checks:
        raise ValueError("nonempty historical result checks required")
    for check in checks:
        object_value(check, "historical check")
        if "exit_code" in check and (type(check["exit_code"]) is not int or check["exit_code"] != 0):
            raise ValueError("failed historical process check")
        if "status" in check:
            if check["status"] == "not_applicable":
                text_value(check.get("reason"), "not_applicable reason")
            elif check["status"] != "pass":
                raise ValueError("nonpassing historical result check")
        elif "exit_code" not in check:
            raise ValueError("historical check requires status or exit_code")


def verify_json_result(value) -> None:
    """Read declared result fields, leaving before/after business facts untouched."""
    if isinstance(value, list):
        # Historical quality.json is a list of command executions.
        verify_result_checks(value)
        return
    object_value(value, "JSON evidence")
    if "status" in value and value["status"] not in ("pass", "passed"):
        raise ValueError("nonpassing JSON evidence status")
    if "passed" in value and value["passed"] is not True:
        raise ValueError("nonpassing JSON evidence result")
    for field in ("failures", "errors"):
        if field in value and value[field] not in (0, []):
            raise ValueError(f"JSON evidence has {field}")
    if "checks" in value:
        verify_result_checks(value["checks"])
    if "summary" in value and isinstance(value["summary"], dict):
        verify_json_result(value["summary"])
        if "tests" in value:
            verify_result_checks(value["tests"])


def validate_envelope(root: Path, task: dict, envelope: dict, *, structure_only=False) -> dict:
    if envelope.get("task_id") != task["id"]:
        raise ValueError("envelope task ID mismatch")
    expected_kind = "documentation" if task["kind"] == "documentation" else "engineering"
    if envelope.get("kind") != expected_kind:
        raise ValueError("envelope kind does not match task")
    for field in ("compatibility", "accepted_scope"):
        text_value(envelope.get(field), field)
    limitations = envelope.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise ValueError("explicit limitations required (including unmodeled boundaries)")
    for item in limitations:
        text_value(item, "limitation")
    workflow = envelope.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != WORKFLOW:
        raise ValueError("all five development workflow stages required")
    for stage, description in workflow.items():
        text_value(description, stage)
    references = []
    for field in ("legacy_receipt", "contract", "source_identity"):
        ref = envelope.get(field)
        verify_reference(root, ref, structure_only=structure_only)
        references.append(ref)
    inputs = envelope.get("fixed_inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("fixed configuration/input identity required")
    for ref in inputs:
        verify_reference(root, ref, structure_only=structure_only)
        references.append(ref)
    source_binding = verify_source_binding(root, envelope, structure_only=structure_only)
    checks = envelope.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("completion checks required")
    categories, junit = set(), []
    for check in checks:
        if not isinstance(check, dict) or check.get("category") not in CATEGORIES:
            raise ValueError("unknown completion check category")
        categories.add(check["category"])
        if check.get("status") != "pass":
            raise ValueError("accepted envelope contains a nonpassing check")
        text_value(check.get("reason"), "check reason")
        evidence = check.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("check requires locatable evidence")
        skips = check.get("allowed_skips", [])
        if not isinstance(skips, list) or any(not isinstance(x, str) or not x for x in skips):
            raise ValueError("allowed_skips must name exact JUnit cases")
        if skips:
            text_value(check.get("skip_reason"), "skip_reason")
        for ref in evidence:
            path = verify_reference(root, ref, structure_only=structure_only)
            references.append(ref)
            if not structure_only and path.suffix == ".xml":
                junit.append({"path": ref["path"], **verify_junit(path, skips)})
            elif not structure_only and path.suffix == ".json":
                verify_json_result(load_json(path))
    required = ({"links", "ids", "coverage", "hashes"} if expected_kind == "documentation"
                else {"positive", "negative", "reconciliation", "integration"})
    if task["priority"] == "P0" and expected_kind != "documentation":
        required.add("fault")
    if required - categories:
        raise ValueError(f"missing risk-proportionate checks: {sorted(required - categories)}")
    if not structure_only and expected_kind == "engineering":
        receipt = object_value(load_json(repository_path(root, envelope["legacy_receipt"]["path"])),
                               "historical receipt")
        if receipt.get("task_id") != task["id"] or receipt.get("engineering_status") != "pass":
            raise ValueError("historical receipt does not support engineering closure")
        text_value(receipt.get("run_id"), "historical run_id")
        verify_result_checks(receipt.get("checks"))
    return {"task_id": task["id"], "kind": expected_kind,
            "status": "structure_verified" if structure_only else "historical_evidence_verified",
            "accepted_scope": envelope["accepted_scope"], "categories": sorted(categories),
            "evidence_files": len({ref["path"] for ref in references}), "junit": junit,
            "source_binding": source_binding,
            "current_source_revalidated": False}


def keyed(rows, field):
    if not isinstance(rows, list):
        raise ValueError("task collection must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("task collection contains a nonobject")
        name = row.get(field)
        if not isinstance(name, str) or not name or name in result:
            raise ValueError(f"missing or duplicate task ID: {name}")
        result[name] = row
    return result


def verify_documents(root: Path, tasks: list[dict]) -> None:
    plan = (root / "docs/development_plan.md").read_text(encoding="utf-8-sig")
    details = (root / "docs/development_details.md").read_text(encoding="utf-8-sig")
    rows = {}
    for line in plan.splitlines():
        if re.match(r"\| \[(?:DOC|SYS|FIX|VER)-\d+\]", line):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            task_id = re.search(r"\[([^]]+)\]", cells[0]).group(1)
            if task_id in rows:
                raise ValueError(f"duplicate plan row: {task_id}")
            rows[task_id] = cells
    if set(rows) != {task["id"] for task in tasks}:
        raise ValueError("plan/registry task coverage mismatch")
    for task in tasks:
        cells = rows[task["id"]]
        if len(cells) != 7 or cells[1:5] != [task["title"], task["priority"], task["batch"], task["status"]]:
            raise ValueError(f"plan/registry state mismatch: {task['id']}")
        dependencies = re.findall(r"\[((?:DOC|SYS|FIX|VER)-\d+)\]", cells[5])
        if set(dependencies) != set(task["dependencies"]):
            raise ValueError(f"plan/registry dependency mismatch: {task['id']}")
        if f'<a id="{task["id"].lower()}"></a>' not in details:
            raise ValueError(f"missing detail anchor: {task['id']}")
        navigation = [line for line in details.splitlines()
                      if line.startswith(f'- [{task["id"]}](development_details.md#')]
        if len(navigation) != 1 or f'（{task["batch"]} / {task["priority"]} / {task["status"]}）' not in navigation[0]:
            raise ValueError(f"detail directory/registry state mismatch: {task['id']}")
        card = details.split(f'<a id="{task["id"].lower()}"></a>', 1)[1].split('<a id="', 1)[0]
        properties = {}
        for line in card.splitlines():
            if line.startswith("| "):
                parts = [part.strip() for part in line.strip("|").split("|")]
                if len(parts) == 2:
                    if parts[0] in properties:
                        raise ValueError(f"duplicate detail property: {task['id']}")
                    properties[parts[0]] = parts[1]
        classification = f'{task["kind"]} / {task["batch"]} / {task["priority"]}'
        if properties.get("类型 / 批次 / 优先级") != classification:
            raise ValueError(f"detail card classification mismatch: {task['id']}")
        if properties.get("本轮复核状态", "").split("；", 1)[0] != task["status"]:
            raise ValueError(f"detail card current status mismatch: {task['id']}")
        card_dependencies = re.findall(r"\[((?:DOC|SYS|FIX|VER)-\d+)\]",
                                       properties.get("验收依赖", ""))
        if set(card_dependencies) != set(task["dependencies"]):
            raise ValueError(f"detail card dependency mismatch: {task['id']}")


def audit(root: Path = ROOT, *, structure_only=False) -> dict:
    registry = object_value(load_json(root / REGISTRY), "registry")
    manifest = object_value(load_json(root / MANIFEST), "completion manifest")
    if manifest.get("schema_version") != "roadmap-completion/v1":
        raise ValueError("unsupported completion manifest version")
    if manifest.get("scope") != "historical_local_acceptance":
        raise ValueError("completion manifest must retain historical local scope")
    tasks = registry["tasks"]
    scheduling = validate_tasks(tasks)
    verify_documents(root, tasks)
    if object_value(registry.get("counts"), "counts").get("tasks") != len(tasks):
        raise ValueError("registry task count mismatch")
    accepted = {task["id"] for task in tasks if task["status"] == "已验收"}
    envelopes = keyed(manifest.get("tasks"), "task_id")
    if set(envelopes) != accepted:
        raise ValueError("accepted tasks/completion envelopes coverage mismatch")
    latest = object_value(registry.get("latest_execution"), "latest_execution")
    accepted_list = latest.get("accepted_tasks", [])
    if len(accepted_list) != len(set(accepted_list)) or set(accepted_list) != accepted:
        raise ValueError("latest_execution accepted task set mismatch")
    indexed = {}
    if not structure_only:
        index = object_value(load_json(root / INDEX), "acceptance index")
        indexed = keyed(index.get("tasks"), "task_id")
        if set(indexed) != {task["id"] for task in tasks}:
            raise ValueError("acceptance index/registry task coverage mismatch")
        for field in ("overall_R0_R8", "strategy_admission"):
            if index.get(field) != latest.get(field):
                raise ValueError(f"index/registry project conclusion mismatch: {field}")
        if accepted != set(indexed) and index.get("overall_R0_R8") == "complete":
            raise ValueError("incomplete tasks cannot imply project completion")
        if scheduling["formal_freeze_blockers"] and index.get("strategy_admission") != "paused_revalidation":
            raise ValueError("unresolved P0/P1 tasks require paused_revalidation")
    rows, failures = [], []
    for task in tasks:
        try:
            row = indexed.get(task["id"])
            if row is not None:
                for field in ("status", "dependencies", "title"):
                    if row.get(field) != task.get(field):
                        raise ValueError(f"index/registry mismatch: {field}")
                for field in ("engineering_status", "research_status", "operational_status"):
                    if row.get(field) not in DIMENSIONS:
                        raise ValueError(f"invalid {field}")
                receipt_path = verify_reference(root, row.get("receipt"))
                receipt = object_value(load_json(receipt_path), "historical receipt")
                if task["kind"] != "documentation" and receipt.get("task_id") != task["id"]:
                    raise ValueError("receipt/index task ID mismatch")
                # Historical schemas have explicit descriptive pending aliases.
                # None may be upgraded to pass by editing the index alone.
                for field in ("research_status", "operational_status"):
                    original = receipt.get(field)
                    if row[field] == "pass" and original != "pass":
                        raise ValueError(f"index upgrades unproven {field}")
                    if original == "fail" and row[field] != "fail":
                        raise ValueError(f"index loses historical {field} failure")
                for field in ("receipt_history", "supplemental_evidence"):
                    for ref in row.get(field, []):
                        verify_reference(root, ref)
                if task["id"] in accepted and row["engineering_status"] != "pass":
                    raise ValueError("accepted task lacks engineering pass")
            if task["id"] in accepted:
                envelope = envelopes[task["id"]]
                if row and envelope.get("legacy_receipt") != row.get("receipt"):
                    raise ValueError("envelope/index receipt mismatch")
                rows.append(validate_envelope(root, task, envelope, structure_only=structure_only))
            elif row is not None:
                if not row.get("limitations"):
                    raise ValueError("open task must retain outstanding limitations")
                rows.append({"task_id": task["id"], "status": "open",
                             "limitations": row["limitations"]})
        except (ValueError, OSError, KeyError, TypeError, ET.ParseError) as exc:
            failures.append({"task_id": task["id"], "reason": str(exc)})
    inputs = [REGISTRY, MANIFEST, "docs/development_plan.md", "docs/development_details.md",
              "docs/unified_roadmap.md", "docs/roadmap_completion_contract.md",
              "scripts/verify_roadmap_completion.py", "scripts/roadmap_priority.py",
              "scripts/roadmap_evidence.py"]
    if not structure_only:
        inputs.append(INDEX)
    return {"schema_version": "roadmap-completion-audit/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "fail" if failures else ("structure_verified_evidence_not_checked"
                                               if structure_only else "pass"),
            "scope": "section7_governance_and_historical_local_evidence",
            "live_admission": False, "current_source_revalidated": False,
            "research_and_operations": "not_revalidated", "task_count": len(tasks),
            "accepted_count": len(accepted), "open_count": len(tasks) - len(accepted),
            "status_counts": dict(Counter(task["status"] for task in tasks)),
            "scheduling": scheduling, "tasks": rows, "failures": failures,
            "input_sha256": {name: file_digest(root / name) for name in inputs}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure-only", action="store_true",
                        help="CI metadata check; does not verify ignored historical report files")
    parser.add_argument("--output", type=Path,
                        help="new JSON output path; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new run path")
    try:
        report = audit(structure_only=args.structure_only)
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError) as exc:
        report = {"schema_version": "roadmap-completion-audit/v1", "status": "fail",
                  "live_admission": False, "failures": [{"reason": str(exc)}]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    print(json.dumps({key: report[key] for key in ("status", "live_admission", "failures")},
                     ensure_ascii=True))
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
