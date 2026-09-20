"""Executable section-5 policy register; local evidence is never trading admission."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "docs/archive/2026-09-roadmap-rebaseline/sources"
ACTIVE_DOCUMENTS = {
    "docs/strategy_development_roadmap.md": (
        ["train/validation", "roadmap_policy_contract.md"], ["参数优化默认按 OOS 排序"]),
    "docs/backtest_optimization_roadmap.md": (
        ["最终 OOS/holdout 不参与排名", "roadmap_policy_contract.md"],
        ["筛选指标改为 OOS Sharpe/PF"]),
    "docs/phase6_operations.md": (["不可降低的下限为 56", "连续自然日"], ["默认至少 56"]),
    "docs/backtest_metrics_detailed_development_plan.md": (
        ["metric-result/v2", "invalid_input"], []),
}


def rule(number, title, sources, code, tests):
    return {"id": f"POL-{number:02}", "title": title,
            "sources": [{"source_path": path, "old_id": old_id} for path, old_id in sources],
            "code": code, "tests": tests}


POLICIES = [
    rule(1, "train/validation selection; single-use final holdout",
         [("docs/strategy_development_roadmap.md", "S1-3"),
          ("docs/backtest_optimization_roadmap.md", "D5")],
         ["analysis/optimize.py", "analysis/walk_forward.py", "analysis/research_validation.py",
          "analysis/strategy_review.py", "scripts/register_strategy_successor.py"],
         ["tests/test_phase5_research_validation.py::test_t5_1_frozen_holdout_is_disjoint_and_single_use",
          "tests/test_roadmap_research_reporting.py::test_final_partition_changes_do_not_change_optimizer_ranking",
          "tests/test_strategy_successor.py::test_tampered_maturity_cannot_open_final_sample",
          "tests/test_strategy_successor.py::test_successor_has_new_window_snapshot_and_preserves_parent",
          "tests/test_strategy_successor.py::test_string_false_is_not_complete_label_evidence",
          "tests/test_strategy_successor.py::test_pending_protocol_with_access_log_cannot_be_registered"]),
    rule(2, "56 continuous calendar days and two regimes are irreducible floors",
         [("docs/phase6_operations.md", "T-6.2")], ["core/admission_gates.py"],
         ["tests/test_roadmap_policy.py::test_paper_policy_cannot_lower_production_floor",
          "tests/test_roadmap_policy.py::test_longer_frozen_paper_protocol_is_preserved",
          "tests/test_roadmap_policy.py::test_paper_protocol_never_truncates_invalid_duration",
          "tests/test_roadmap_policy.py::test_direct_admission_rejects_incomplete_or_weakened_paper_report",
          "tests/test_roadmap_admission_recovery.py::test_paper_rejects_sparse_invalid_or_incomplete_evidence",
          "tests/test_roadmap_admission_recovery.py::test_paper_frequency_and_required_window_are_enforced"]),
    rule(3, "approved recovery policy, consumed sample boundary and manual lock",
         [("docs/research/strategy_remediation_contract_20260914.md", "恢复与持久化")],
         ["core/strategy_health.py"],
         ["tests/test_health_extended_recovery.py::test_explicit_and_preexisting_manual_locks_remain_terminal",
          "tests/test_roadmap_admission_recovery.py::test_successful_probation_consumes_tail_losses_but_new_losses_trigger",
          "tests/test_roadmap_admission_recovery.py::test_old_active_probation_checkpoint_migrates_consumed_boundary"]),
    rule(4, "immutable original order risk; precision only reduces approval",
         [("docs/research/strategy_remediation_contract_20260914.md", "最小名义金额与批准风险")],
         ["core/entry_risk.py", "core/risk/entry_policy.py", "core/live_broker/fill_projection.py"],
         ["tests/test_entry_risk_contract.py::test_reduced_budget_gap_cannot_be_replaced_by_base_risk",
          "tests/test_entry_risk_contract.py::test_invalid_explicit_budget_cannot_fall_back",
          "tests/test_entry_risk_contract.py::test_venue_quantity_rounding_tightens_budget_and_restart_restores_reservation",
          "tests/test_entry_risk_contract.py::test_old_checkpoint_is_rechecked_against_frozen_budget"]),
    rule(5, "real fills, finite shared liquidity and persistent target reduction",
         [("docs/backtest_optimization_roadmap.md", "A4"),
          ("docs/research/strategy_remediation_contract_20260914.md", "组合预算与风险转移")],
         ["core/broker/matching.py", "core/broker/fill_service.py", "live_trading/risk_actions.py", "backtest/engine.py"],
         ["tests/test_backtest_stop_pass_and_liquidity.py::TestParticipationCapIsPerBar::test_a_second_pass_over_one_bar_gets_no_fresh_allowance",
          "tests/test_r_series_trading_facts.py::test_ver01_original_gtc_reduction_continues_through_real_engine",
          "tests/test_live_risk_action_lifecycle.py::test_partial_reduce_keeps_residual_protection_and_pending_order_across_restart"]),
    rule(6, "null plus status and reason; versioned invalid-input/legacy migration",
         [("docs/backtest_metrics_detailed_development_plan.md", "BM0")],
         ["core/metric_result.py", "backtest/reporting/serialization.py"],
         ["tests/test_roadmap_research_reporting.py::test_strict_json_preserves_unavailable_values_and_legacy_adapter",
          "tests/test_roadmap_research_reporting.py::test_metric_v2_pandas_and_numpy_missing_values",
          "tests/test_roadmap_system_metrics.py::test_execution_missing_reference_and_missing_stream_are_not_zero",
          "tests/test_roadmap_system_metrics.py::test_execution_bad_facts_never_return_valid_headlines"]),
    rule(7, "authoritative persisted trading events and lot facts, not research audit output",
         [("docs/authoritative_ledger.md", "Scope and invariants"),
          ("docs/backtest_optimization_roadmap.md", "B1")],
         ["core/events/store.py", "core/lots.py", "core/live_broker/fill_projection.py", "research/audit/ledger.py"],
         ["tests/test_p1_authoritative_ledger.py::TestAppendOnlyEventStore::test_persists_ordered_events_is_idempotent_and_rejects_mutation",
          "tests/test_p1_authoritative_ledger.py::TestAuthoritativeLedger::test_snapshot_is_fully_rebuilt_and_reversal_pnl_is_correct",
          "tests/test_r_series_trading_facts.py::test_merged_partial_entries_and_exits_conserve_authoritative_lot_facts"]),
    rule(8, "distinct benchmark identity; frozen BTC/ETH first-open cash sleeves",
         [("docs/research/strategy_remediation_contract_20260914.md", "报告与冻结实验"),
          ("docs/backtest_optimization_roadmap.md", "B2")],
         ["core/benchmarks.py"],
         ["tests/test_roadmap_system_metrics.py::test_benchmarks_keep_distinct_policies_and_reproduce_frozen_first_open",
          "tests/test_roadmap_system_metrics.py::test_rebalanced_benchmark_charges_actual_drift_turnover"]),
    rule(9, "source document path plus old ID is the traceability key",
         [("docs/live_trading_remediation_plan.md", "BT-01"),
          ("docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md", "BT-01")],
         ["scripts/verify_roadmap_policy.py"],
         ["tests/test_roadmap_policy.py::test_source_path_and_old_id_preserve_distinct_legacy_tasks",
          "tests/test_roadmap_policy.py::test_registry_rejects_broken_or_ambiguous_evidence",
          "tests/test_roadmap_policy.py::test_evidence_digest_detects_changed_inputs",
          "tests/test_roadmap_policy.py::test_current_registry_covers_all_nine_rules_with_real_sources_and_tests"]),
]


def trace_key(source_path: str, old_id: str) -> tuple[str, str]:
    """Normalize separators, retaining the full original path as part of identity."""
    path = PurePosixPath(source_path.replace("\\", "/"))
    if not source_path.strip() or path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise ValueError("source_path must be a relative repository path")
    if not old_id.strip():
        raise ValueError("old_id is required")
    return path.as_posix(), old_id.strip()


def file_at(root: Path, name: str) -> Path:
    relative, _ = trace_key(name, "file")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"missing or outside repository: {name}")
    return path


def test_node_exists(root: Path, node: str) -> None:
    filename, *names = node.split("::")
    if not names:
        raise ValueError(f"test must name a concrete test node: {node}")
    body = ast.parse(file_at(root, filename).read_text(encoding="utf-8-sig")).body
    for name in names:
        matches = [item for item in body
                   if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and item.name == name]
        if len(matches) != 1:
            raise ValueError(f"missing or ambiguous test node: {node}")
        body = matches[0].body


def validate_registry(root: Path = ROOT, policies=None) -> dict:
    policies = POLICIES if policies is None else policies
    ids, inputs, rows = set(), set(), []
    document_checks = []
    if root.resolve() == ROOT.resolve():
        inputs.add("scripts/run_portable_tests.py")
        inputs.add("docs/roadmap_policy_contract.md")
        for name, (required, retired) in ACTIVE_DOCUMENTS.items():
            content = file_at(root, name).read_text(encoding="utf-8-sig")
            if any(text not in content for text in required) or any(text in content for text in retired):
                raise ValueError("active document conflicts with unified policy: " + name)
            inputs.add(name)
            document_checks.append({"path": name, "status": "passed"})
    for policy in policies:
        if policy["id"] in ids:
            raise ValueError("duplicate policy ID: " + policy["id"])
        ids.add(policy["id"])
        if not all(policy.get(field) for field in ("title", "sources", "code", "tests")):
            raise ValueError("policy requires title, sources, code and tests")
        source_keys, sources = set(), []
        for source in policy["sources"]:
            key = trace_key(source["source_path"], source["old_id"])
            if key in source_keys:
                raise ValueError(f"duplicate source key in {policy['id']}: {key}")
            source_keys.add(key)
            archived = f"{ARCHIVE}/{key[0]}"
            resolved = archived if (root / archived).is_file() else key[0]
            path = file_at(root, resolved)
            if key[1] not in path.read_text(encoding="utf-8-sig"):
                raise ValueError(f"old ID not found in source: {key}")
            inputs.add(resolved)
            sources.append({"source_path": key[0], "old_id": key[1], "evidence_path": resolved})
        for path in policy["code"]:
            file_at(root, path)
            inputs.add(path)
        for node in policy["tests"]:
            test_node_exists(root, node)
            inputs.add(node.split("::", 1)[0])
        rows.append({**policy, "sources": sources, "status": "references_verified"})
    return {"rules": rows, "active_document_checks": document_checks, "evidence_inputs_sha256": {
        name: hashlib.sha256(file_at(root, name).read_bytes()).hexdigest() for name in sorted(inputs)}}


def verify_input_identity(root: Path, hashes: dict) -> None:
    if not hashes:
        raise ValueError("evidence input identity is missing")
    for name, expected in hashes.items():
        if hashlib.sha256(file_at(root, name).read_bytes()).hexdigest() != expected:
            raise ValueError("evidence input changed: " + name)


def run_checks(output: Path, *, root: Path = ROOT, run_tests: bool = False) -> dict:
    evidence = validate_registry(root)
    report = {"schema_version": "quanttrading.roadmap-policy/v1",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "unified_roadmap.section5.local_policy_contracts",
              "status": "references_verified_tests_not_run", "live_admission": False,
              "external_evidence": "not_evaluated", **evidence}
    output.mkdir(parents=True, exist_ok=True)
    if run_tests:
        nodes = list(dict.fromkeys(node for row in POLICIES for node in row["tests"]))
        xml_path, log_path = output / "policy_tests.xml", output / "policy_tests.log"
        command = [sys.executable, str(root / "scripts/run_portable_tests.py"),
                   "-q", "-o", "addopts=", *nodes, f"--junitxml={xml_path.resolve()}"]
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, check=False)
        cases = list(ET.parse(xml_path).iter("testcase")) if xml_path.is_file() else []
        unavailable = sum(any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                          for case in cases)
        all_present = all(any(
            case.get("classname") == node.split("::")[0][:-3].replace("/", ".")
            + ("." + ".".join(node.split("::")[1:-1]) if len(node.split("::")) > 2 else "")
            and case.get("name", "").split("[", 1)[0] == node.split("::")[-1]
            for case in cases) for node in nodes)
        verify_input_identity(root, evidence["evidence_inputs_sha256"])
        passed = completed.returncode == 0 and bool(cases) and unavailable == 0 and all_present
        report["status"] = "passed" if passed else "failed"
        report["test_run"] = {"exit_code": completed.returncode, "testcases": len(cases),
                              "unavailable_or_failed": unavailable, "all_nodes_present": all_present,
                              "log": log_path.name, "junit": xml_path.name,
                              "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                              "junit_sha256": hashlib.sha256(xml_path.read_bytes()).hexdigest()
                              if xml_path.is_file() else None}
        for row in report["rules"]:
            row["status"] = "passed" if passed else "suite_failed"
    (output / "policy_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/roadmap_v3/POLICY/20260920-section5")
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    report = run_checks(args.output, run_tests=args.run_tests)
    print(json.dumps({"status": report["status"], "rules": len(report["rules"]),
                      "test_run": report.get("test_run"), "live_admission": False}, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
