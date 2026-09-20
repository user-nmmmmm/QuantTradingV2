"""Section-7 evidence integrity and closure checks, independent of report archives."""
from copy import deepcopy
import json

import pytest

from scripts.verify_roadmap_completion import (
    INDEX,
    MANIFEST,
    REGISTRY,
    ROOT,
    WORKFLOW,
    audit,
    file_digest,
    load_json,
    validate_envelope,
    verify_junit,
    verify_reference,
)


def write_text(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "sha256": file_digest(target)}


def write_json(root, path, content):
    return write_text(root, path, json.dumps(content, ensure_ascii=False))


@pytest.fixture
def completion_bundle(tmp_path):
    task = {
        "id": "SYS-01", "title": "Synthetic accepted accounting task", "kind": "capability",
        "priority": "P0", "batch": "B0", "status": "已验收", "dependencies": [],
        "contract": "Reconcile the frozen facts.", "compatibility": "Read existing facts.",
        "acceptance": ["Account for each fact and reject invalid data."],
        "sources": ["docs/contract.md"],
    }
    receipt = write_json(tmp_path, "evidence/receipt.json", {
        "task_id": "SYS-01", "run_id": "fixed-run-01", "engineering_status": "pass",
        "research_status": "fail", "operational_status": "pending",
        "checks": [{"name": "fixed facts reconcile", "status": "pass"}],
    })
    contract = write_text(tmp_path, "docs/contract.md", '<a id="sys-01"></a>\nFrozen contract\n')
    contract["anchor"] = "sys-01"
    source_root = "evidence/frozen"
    source_member = write_text(tmp_path, source_root + "/source/engine.py", "FROZEN_VALUE = 1\n")
    inputs = write_text(tmp_path, source_root + "/tests/test_fixed_inputs.py",
                        "FIXED_SEED = 23\nFIXED_VALUES = [2, 3]\n")
    source = write_json(tmp_path, "evidence/source_identity.json", {"source_sha256": {
        "source/engine.py": source_member["sha256"],
        "tests/test_fixed_inputs.py": inputs["sha256"],
    }})
    evidence = write_json(tmp_path, "evidence/reconciliation.json", {"difference": 0})
    junit = write_text(tmp_path, "evidence/tests.xml", (
        '<testsuites><testsuite tests="2" failures="0" errors="0">'
        '<testcase classname="tests.accounting" name="positive"/>'
        '<testcase classname="tests.accounting" name="negative"/>'
        '</testsuite></testsuites>'
    ))
    envelope = {
        "task_id": "SYS-01", "kind": "engineering",
        "accepted_scope": "Fixed-input historical engineering evidence.",
        "compatibility": "Existing account facts retain the same interpretation.",
        "limitations": ["No live account or independent research admission."],
        "workflow": {name: "Recorded evidence for " + name for name in WORKFLOW},
        "legacy_receipt": receipt, "contract": contract, "source_identity": source,
        "source_snapshot_root": source_root,
        "fixed_inputs": [inputs],
        "checks": [
            {"category": category, "status": "pass", "reason": "Verified fixed facts.",
             "evidence": [deepcopy(junit if category in {"positive", "negative", "fault"}
                                   else evidence)]}
            for category in ("positive", "negative", "fault", "reconciliation", "integration")
        ],
    }
    return tmp_path, task, envelope


def test_engineering_closure_preserves_research_failure_and_does_not_revalidate_source(completion_bundle):
    root, task, envelope = completion_bundle
    result = validate_envelope(root, task, envelope)
    assert result["status"] == "historical_evidence_verified"
    assert result["current_source_revalidated"] is False
    assert set(result["categories"]) == {
        "positive", "negative", "fault", "reconciliation", "integration",
    }
    assert load_json(root / envelope["legacy_receipt"]["path"])["research_status"] == "fail"


@pytest.mark.parametrize("role", ["source_identity", "legacy_receipt", "contract", "input", "check"])
def test_changed_or_missing_artifact_cannot_keep_a_passing_envelope(completion_bundle, role):
    root, task, envelope = completion_bundle
    ref = (envelope["fixed_inputs"][0] if role == "input" else
           envelope["checks"][3]["evidence"][0] if role == "check" else envelope[role])
    path = root / ref["path"]
    path.write_bytes(path.read_bytes() + b"\nchanged")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_envelope(root, task, envelope)
    path.unlink()
    with pytest.raises(ValueError, match="missing evidence"):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("path", ["../outside.json", "/outside.json", "C:/outside.json",
                                 r"..\outside.json", r"\\host\share\outside.json"])
@pytest.mark.parametrize("structure_only", [False, True])
def test_path_escape_is_rejected_even_in_structure_only_mode(tmp_path, path, structure_only):
    with pytest.raises(ValueError, match="outside repository"):
        verify_reference(tmp_path, {"path": path, "sha256": "a" * 64},
                         structure_only=structure_only)


@pytest.mark.parametrize("ref", [None, {}, {"path": "a", "sha256": "A" * 64},
                                {"path": "a", "sha256": "0" * 63},
                                {"path": "", "sha256": "0" * 64}])
def test_reference_requires_a_path_and_full_lowercase_digest(tmp_path, ref):
    with pytest.raises(ValueError):
        verify_reference(tmp_path, ref, structure_only=True)


def test_contract_anchor_is_checked_after_digest(completion_bundle):
    root, task, envelope = completion_bundle
    envelope["contract"]["anchor"] = "nonexistent-task"
    with pytest.raises(ValueError, match="missing contract anchor"):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("category", ["positive", "negative", "fault", "reconciliation", "integration"])
def test_p0_closure_requires_each_risk_proportionate_check(completion_bundle, category):
    root, task, envelope = completion_bundle
    envelope["checks"] = [check for check in envelope["checks"] if check["category"] != category]
    with pytest.raises(ValueError, match="missing risk-proportionate checks"):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("status", [False, True, "false", "true", "fail", "pending", None, "PASS"])
def test_check_status_must_be_explicit_pass(completion_bundle, status):
    root, task, envelope = completion_bundle
    envelope["checks"][0]["status"] = status
    with pytest.raises(ValueError, match="nonpassing check"):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("field,value,error", [
    ("task_id", "SYS-99", "task ID mismatch"),
    ("kind", "documentation", "kind does not match"),
    ("compatibility", " ", "compatibility"),
    ("accepted_scope", None, "accepted_scope"),
    ("limitations", [], "limitations"),
    ("limitations", [""], "limitation"),
    ("workflow", {}, "workflow stages"),
    ("source_identity", None, "reference"),
    ("fixed_inputs", [], "input identity"),
    ("fixed_inputs", None, "input identity"),
    ("checks", [], "completion checks"),
])
def test_incomplete_envelope_is_rejected(completion_bundle, field, value, error):
    root, task, envelope = completion_bundle
    envelope[field] = value
    with pytest.raises(ValueError, match=error):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("field,value,error", [
    ("category", "count-only", "unknown completion"),
    ("reason", "", "check reason"),
    ("evidence", [], "locatable evidence"),
    ("allowed_skips", "one", "exact JUnit cases"),
    ("allowed_skips", [None], "exact JUnit cases"),
    ("allowed_skips", ["tests.accounting::unavailable"], "skip_reason"),
])
def test_check_cannot_replace_evidence_with_a_claim(completion_bundle, field, value, error):
    root, task, envelope = completion_bundle
    envelope["checks"][0][field] = value
    with pytest.raises(ValueError, match=error):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("field,value", [("task_id", "SYS-99"), ("engineering_status", False),
                                         ("engineering_status", "false"),
                                         ("engineering_status", "fail"), ("run_id", "")])
def test_historical_receipt_must_support_this_task_and_run(completion_bundle, field, value):
    root, task, envelope = completion_bundle
    receipt = load_json(root / envelope["legacy_receipt"]["path"])
    receipt[field] = value
    envelope["legacy_receipt"] = write_json(root, "evidence/receipt.json", receipt)
    with pytest.raises(ValueError, match="historical"):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("checks", [
    [], [{"status": "fail"}], [{"status": "pending"}], [{"status": "insufficient"}],
    [{"exit_code": False}], [{"exit_code": "0"}], [{"exit_code": 0.0}],
    [{"status": "not_applicable"}], [{"status": "pass", "exit_code": 1}],
])
def test_passing_receipt_cannot_hide_missing_or_failed_check_results(completion_bundle, checks):
    root, task, envelope = completion_bundle
    receipt = load_json(root / envelope["legacy_receipt"]["path"])
    receipt["checks"] = checks
    envelope["legacy_receipt"] = write_json(root, "evidence/receipt.json", receipt)
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("check", [
    {"status": "pass"}, {"exit_code": 0},
    {"status": "not_applicable", "reason": "No persistent state in this isolated contract."},
])
def test_supported_historical_receipt_check_formats_remain_compatible(completion_bundle, check):
    root, task, envelope = completion_bundle
    receipt = load_json(root / envelope["legacy_receipt"]["path"])
    receipt["checks"] = [check]
    envelope["legacy_receipt"] = write_json(root, "evidence/receipt.json", receipt)
    assert validate_envelope(root, task, envelope)["status"] == "historical_evidence_verified"


def test_arbitrary_hashed_receipt_cannot_masquerade_as_source_identity(completion_bundle):
    root, task, envelope = completion_bundle
    envelope["source_identity"] = deepcopy(envelope["legacy_receipt"])
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


def test_source_manifest_binds_actual_frozen_implementation_bytes(completion_bundle):
    root, task, envelope = completion_bundle
    frozen_code = root / envelope["source_snapshot_root"] / "source/engine.py"
    frozen_code.write_text("FROZEN_VALUE = 999\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)
    frozen_code.unlink()
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


def test_fixed_input_must_belong_to_the_declared_frozen_identity(completion_bundle):
    root, task, envelope = completion_bundle
    envelope["fixed_inputs"] = [write_json(root, "evidence/unbound_inputs.json", {"seed": 23})]
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


def test_source_manifest_cannot_escape_snapshot_root(completion_bundle):
    root, task, envelope = completion_bundle
    manifest = load_json(root / envelope["source_identity"]["path"])
    manifest["source_sha256"]["../outside.py"] = "a" * 64
    envelope["source_identity"] = write_json(root, "evidence/source_identity.json", manifest)
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("result", [{"status": "fail"}, {"passed": False}, {"errors": 1}])
def test_failed_json_evidence_cannot_be_promoted_by_envelope_pass(completion_bundle, result):
    root, task, envelope = completion_bundle
    envelope["checks"][3]["evidence"] = [write_json(root, "evidence/failed_result.json", result)]
    with pytest.raises(ValueError):
        validate_envelope(root, task, envelope)


@pytest.mark.parametrize("xml,error", [
    ('<testsuite tests="0"/>', "empty JUnit"),
    ('<testsuite><testcase name="x"><failure/></testcase></testsuite>', "failed JUnit"),
    ('<testsuite><testcase name="x"><error/></testcase></testsuite>', "failed JUnit"),
    ('<testsuite failures="1"><testcase name="x"/></testsuite>', "failed JUnit suite"),
    ('<testsuite><testcase classname="suite" name="skip"><skipped/></testcase>'
     '<testcase classname="suite" name="pass"/></testsuite>', "undeclared skipped"),
])
def test_junit_rejects_empty_failing_and_undeclared_skipped_results(tmp_path, xml, error):
    write_text(tmp_path, "tests.xml", xml)
    with pytest.raises(ValueError, match=error):
        verify_junit(tmp_path / "tests.xml", [])


def test_junit_allows_only_explicit_skip_with_a_remaining_pass(tmp_path):
    write_text(tmp_path, "tests.xml", (
        '<testsuite><testcase classname="suite" name="skip"><skipped/></testcase>'
        '<testcase classname="suite" name="pass"/></testsuite>'
    ))
    report = verify_junit(tmp_path / "tests.xml", ["suite::skip"])
    assert report["cases"] == 2 and report["skipped"] == ["suite::skip"]
    assert report["skipped_count"] == 1
    with pytest.raises(ValueError, match="undeclared skipped"):
        verify_junit(tmp_path / "tests.xml", ["suite::another_case"])


@pytest.mark.parametrize("copies", [1, 2])
def test_all_skipped_cases_never_count_as_acceptance_even_with_duplicate_names(tmp_path, copies):
    skipped = '<testcase classname="suite" name="skip"><skipped/></testcase>'
    write_text(tmp_path, "tests.xml", "<testsuite>" + skipped * copies + "</testsuite>")
    with pytest.raises(ValueError, match="no passed JUnit cases"):
        verify_junit(tmp_path / "tests.xml", ["suite::skip"])


def test_documentation_uses_structural_checks_without_unrelated_trading_tests(completion_bundle):
    root, task, envelope = completion_bundle
    task.update(id="DOC-01", kind="documentation")
    envelope.update(task_id="DOC-01", kind="documentation")
    envelope["legacy_receipt"] = write_json(root, "evidence/archive.json", {"archived": 4})
    archived_input = write_text(root, "evidence/archive/docs/frozen.md", "Archived document facts\n")
    envelope["source_snapshot_root"] = "evidence/archive"
    envelope["source_identity"] = write_json(root, "evidence/archive_manifest.json", {
        "files": [{"snapshot": "docs/frozen.md", "sha256": archived_input["sha256"]}],
    })
    envelope["fixed_inputs"] = [archived_input]
    evidence = envelope["checks"][3]["evidence"]
    envelope["checks"] = [
        {"category": category, "status": "pass", "reason": "Verified document structure.",
         "evidence": deepcopy(evidence)} for category in ("links", "ids", "coverage", "hashes")
    ]
    result = validate_envelope(root, task, envelope)
    assert result["kind"] == "documentation" and result["junit"] == []
    envelope["checks"].pop()
    with pytest.raises(ValueError, match="hashes"):
        validate_envelope(root, task, envelope)


def test_structure_only_is_explicitly_not_evidence_verification(completion_bundle):
    root, task, envelope = completion_bundle
    (root / envelope["source_identity"]["path"]).unlink()
    result = validate_envelope(root, task, envelope, structure_only=True)
    assert result["status"] == "structure_verified"
    assert result["current_source_revalidated"] is False
    with pytest.raises(ValueError, match="missing evidence"):
        validate_envelope(root, task, envelope)


def build_audit_fixture(bundle):
    root, accepted_task, envelope = bundle
    open_task = deepcopy(accepted_task)
    open_task.update(id="SYS-02", title="Unfinished dependent task", priority="P1",
                     status="待证据", dependencies=["SYS-01"])
    tasks = [accepted_task, open_task]
    conclusions = {"overall_R0_R8": "pending", "strategy_admission": "paused_revalidation"}
    registry = {"tasks": tasks, "counts": {"tasks": 2},
                "latest_execution": {"accepted_tasks": ["SYS-01"], **conclusions}}
    manifest = {"schema_version": "roadmap-completion/v1",
                "scope": "historical_local_acceptance", "tasks": [envelope]}
    rows = []
    for task in tasks:
        receipt = envelope["legacy_receipt"] if task["id"] == "SYS-01" else write_json(
            root, "evidence/open_receipt.json", {
                "task_id": task["id"], "run_id": "open-task-run", "engineering_status": "pass",
                "research_status": "fail", "operational_status": "pending",
            },
        )
        rows.append({
            "task_id": task["id"], "title": task["title"], "status": task["status"],
            "dependencies": task["dependencies"], "engineering_status": "pass",
            "research_status": "fail", "operational_status": "pending",
            "receipt": receipt, "limitations": ["No live admission."],
        })
    index = {"tasks": rows, **conclusions}
    write_json(root, REGISTRY, registry)
    write_json(root, MANIFEST, manifest)
    write_json(root, INDEX, index)
    lines = []
    for task in tasks:
        dependencies = ", ".join(f"[{name}](development_details.md)" for name in task["dependencies"])
        lines.append(
            f"| [{task['id']}](development_details.md) | {task['title']} | {task['priority']} | "
            f"{task['batch']} | {task['status']} | {dependencies or '无'} | reviewer |"
        )
    write_text(root, "docs/development_plan.md", "\n".join(lines))
    navigation = '\n'.join(
        f'- [{task["id"]}](development_details.md#{task["id"].lower()}) '
        f'{task["title"]}（{task["batch"]} / {task["priority"]} / {task["status"]}）'
        for task in tasks
    )
    cards = []
    for task in tasks:
        dependencies = "、".join(
            f"[{name}](development_details.md#{name.lower()})" for name in task["dependencies"]
        )
        cards.append(
            f'<a id="{task["id"].lower()}"></a>\n\n## {task["id"]} {task["title"]}\n\n'
            '| 属性 | 内容 |\n| --- | --- |\n'
            f'| 类型 / 批次 / 优先级 | {task["kind"]} / {task["batch"]} / {task["priority"]} |\n'
            f'| 2026-09-20状态 | {task["status"]} |\n'
            f'| 本轮复核状态 | {task["status"]}；[验收证据](../evidence/receipt.json) |\n'
            f'| 验收依赖 | {dependencies or "无"} |\n'
        )
    write_text(root, "docs/development_details.md", navigation + "\n\n" + "\n".join(cards))
    for path in ("docs/unified_roadmap.md", "docs/roadmap_completion_contract.md",
                 "scripts/verify_roadmap_completion.py", "scripts/roadmap_priority.py",
                 "scripts/roadmap_evidence.py"):
        write_text(root, path, "Synthetic audit input\n")
    return root, registry, manifest, index


def test_audit_preserves_open_tasks_and_research_operational_boundaries(completion_bundle):
    root, _, _, _ = build_audit_fixture(completion_bundle)
    result = audit(root)
    assert result["status"] == "pass"
    assert (result["accepted_count"], result["open_count"]) == (1, 1)
    assert result["live_admission"] is False
    assert result["current_source_revalidated"] is False
    assert result["research_and_operations"] == "not_revalidated"
    assert result["scheduling"]["formal_freeze_blockers"] == ["SYS-02"]


@pytest.mark.parametrize("change,error", [
    ("missing_envelope", "completion envelopes coverage"),
    ("extra_envelope", "completion envelopes coverage"),
    ("duplicate_envelope", "duplicate task ID"),
    ("accepted_list", "accepted task set"),
    ("task_count", "task count"),
    ("missing_index_row", "index/registry task coverage"),
    ("project_conclusion", "project conclusion"),
    ("premature_completion", "incomplete tasks"),
])
def test_audit_rejects_inconsistent_closure_coverage(completion_bundle, change, error):
    root, registry, manifest, index = build_audit_fixture(completion_bundle)
    if change == "missing_envelope":
        manifest["tasks"] = []
    elif change == "extra_envelope":
        manifest["tasks"].append({"task_id": "SYS-02"})
    elif change == "duplicate_envelope":
        manifest["tasks"] *= 2
    elif change == "accepted_list":
        registry["latest_execution"]["accepted_tasks"].append("SYS-02")
    elif change == "task_count":
        registry["counts"]["tasks"] = 99
    elif change == "missing_index_row":
        index["tasks"].pop()
    elif change == "project_conclusion":
        index["strategy_admission"] = "approved"
    elif change == "premature_completion":
        index["overall_R0_R8"] = registry["latest_execution"]["overall_R0_R8"] = "complete"
    write_json(root, REGISTRY, registry)
    write_json(root, MANIFEST, manifest)
    write_json(root, INDEX, index)
    with pytest.raises(ValueError, match=error):
        audit(root)


@pytest.mark.parametrize("field,value,error", [
    ("status", "待证据", "index/registry mismatch: status"),
    ("dependencies", ["SYS-02"], "index/registry mismatch: dependencies"),
    ("title", "Different task", "index/registry mismatch: title"),
    ("engineering_status", "fail", "lacks engineering pass"),
    ("research_status", False, "invalid research_status"),
    ("operational_status", "false", "invalid operational_status"),
])
def test_audit_reports_per_task_index_mismatches(completion_bundle, field, value, error):
    root, _, _, index = build_audit_fixture(completion_bundle)
    index["tasks"][0][field] = value
    write_json(root, INDEX, index)
    result = audit(root)
    assert result["status"] == "fail"
    assert any(row["task_id"] == "SYS-01" and error in row["reason"] for row in result["failures"])


def test_audit_rejects_open_task_without_outstanding_limits(completion_bundle):
    root, _, _, index = build_audit_fixture(completion_bundle)
    index["tasks"][1]["limitations"] = []
    write_json(root, INDEX, index)
    result = audit(root)
    assert result["status"] == "fail"
    assert "outstanding limitations" in result["failures"][0]["reason"]


@pytest.mark.parametrize("field,value", [("research_status", "pass"),
                                         ("research_status", "pending"),
                                         ("operational_status", "pass")])
def test_index_cannot_upgrade_historical_research_or_operations(completion_bundle, field, value):
    root, _, _, index = build_audit_fixture(completion_bundle)
    index["tasks"][0][field] = value
    write_json(root, INDEX, index)
    result = audit(root)
    assert result["status"] == "fail"
    assert any(row["task_id"] == "SYS-01" for row in result["failures"])
    assert result["live_admission"] is False


def test_open_task_receipt_must_belong_to_the_indexed_task(completion_bundle):
    root, _, _, index = build_audit_fixture(completion_bundle)
    index["tasks"][1]["receipt"] = index["tasks"][0]["receipt"]
    write_json(root, INDEX, index)
    result = audit(root)
    assert result["status"] == "fail"
    assert result["failures"] == [{"task_id": "SYS-02", "reason": "receipt/index task ID mismatch"}]


@pytest.mark.parametrize("change,error", [
    ("plan_status", "plan/registry state"),
    ("plan_duplicate", "duplicate plan row"),
    ("detail_anchor", "missing detail anchor"),
    ("detail_status", "detail directory/registry state"),
])
def test_document_state_cannot_drift_from_registry(completion_bundle, change, error):
    root, _, _, _ = build_audit_fixture(completion_bundle)
    if change.startswith("plan"):
        path = root / "docs/development_plan.md"
        content = path.read_text(encoding="utf-8")
        if change == "plan_status":
            content = content.replace("已验收", "待闭环")
        else:
            content += "\n" + content.splitlines()[0]
    else:
        path = root / "docs/development_details.md"
        content = path.read_text(encoding="utf-8")
        if change == "detail_anchor":
            content = content.replace('<a id="sys-01"></a>', "")
        else:
            content = content.replace("已验收", "待闭环")
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        audit(root)


@pytest.mark.parametrize("original,replacement", [
    ("| 本轮复核状态 | 已验收；", "| 本轮复核状态 | 待证据；"),
    ("| 类型 / 批次 / 优先级 | capability / B0 / P0 |",
     "| 类型 / 批次 / 优先级 | capability / B0 / P1 |"),
    ("| 验收依赖 | 无 |", "| 验收依赖 | [SYS-02](development_details.md#sys-02) |"),
])
def test_task_card_cannot_conflict_with_correct_directory_and_registry(completion_bundle, original, replacement):
    root, _, _, _ = build_audit_fixture(completion_bundle)
    path = root / "docs/development_details.md"
    content = path.read_text(encoding="utf-8")
    assert original in content
    path.write_text(content.replace(original, replacement, 1), encoding="utf-8")
    with pytest.raises(ValueError):
        audit(root)


@pytest.mark.parametrize("target", ["registry", "manifest", "counts", "latest_execution", "index"])
@pytest.mark.parametrize("invalid", [None, []])
def test_malformed_top_level_objects_fail_with_controlled_validation(completion_bundle, target, invalid):
    root, registry, manifest, index = build_audit_fixture(completion_bundle)
    if target in {"counts", "latest_execution"}:
        registry[target] = invalid
        write_json(root, REGISTRY, registry)
    else:
        path = {"registry": REGISTRY, "manifest": MANIFEST, "index": INDEX}[target]
        write_json(root, path, invalid)
    with pytest.raises(ValueError, match="object"):
        audit(root)


@pytest.mark.parametrize("content", ['{"id":1,"id":2}', '{"value":NaN}', '{"value":Infinity}'])
def test_json_cannot_hide_duplicate_or_nonfinite_evidence(tmp_path, content):
    write_text(tmp_path, "bad.json", content)
    with pytest.raises(ValueError):
        load_json(tmp_path / "bad.json")


def test_current_registry_completion_structure_requires_no_historical_report_files():
    result = audit(ROOT, structure_only=True)
    assert result["status"] == "structure_verified_evidence_not_checked"
    assert not result["failures"]
    assert result["accepted_count"] + result["open_count"] == result["task_count"]
    assert result["live_admission"] is False
    assert result["current_source_revalidated"] is False
    assert INDEX not in result["input_sha256"]
