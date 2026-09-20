"""Section-5 regressions for production floors and auditable policy provenance."""
from copy import deepcopy
from datetime import timedelta

import pytest

from core.admission_gates import audit_paper_run, evaluate_phase6, review_admission
from scripts.verify_roadmap_policy import (POLICIES, trace_key, validate_registry,
                                          verify_input_identity)
from tests.test_phase6_operational_readiness import START, passing_bundle


@pytest.mark.parametrize("days,regimes", [(1, 1), (14, 2), (28, 2), (55, 2), (56, 1)])
def test_paper_policy_cannot_lower_production_floor(days, regimes):
    bundle = passing_bundle()
    bundle["minimum_paper_days"] = days
    bundle["minimum_market_regimes"] = regimes
    bundle["paper_observations"] = bundle["paper_observations"][:days]
    if regimes == 1:
        for row in bundle["paper_observations"]:
            row["regime"] = "RANGE"
    report = evaluate_phase6(bundle)
    paper = report["tasks"]["T-6.2"]
    assert paper["minimum_days"] == 56
    assert paper["minimum_regimes"] == 2
    assert not paper["passed"] and not report["admission_passed"]


def admission_for(paper, **policy):
    bundle = passing_bundle()
    tasks = evaluate_phase6(bundle)["tasks"]
    return review_admission(
        p0_issues=bundle["p0_issues"], holdout_report=bundle["holdout_report"],
        shadow_report=tasks["T-6.1"], paper_report=paper,
        reconciliation_report=tasks["T-6.3"], calibration_report=tasks["T-6.4"],
        monitoring_report=tasks["T-6.5"], approval=bundle["admission_approval"], **policy)


def test_longer_frozen_paper_protocol_is_preserved():
    rows = passing_bundle()["paper_observations"]
    short = audit_paper_run(rows)
    assert short["passed"]
    assert not audit_paper_run(rows, minimum_days=84)["passed"]
    assert not admission_for(short, minimum_paper_days=84)["passed"]
    rows += [{"timestamp": (START + timedelta(days=day)).isoformat(),
              "regime": "BEAR", "incident_status": "resolved"} for day in range(56, 84)]
    longer = audit_paper_run(rows, minimum_days=84, minimum_regimes=3)
    assert longer["passed"] and longer["minimum_days"] == 84
    assert admission_for(longer, minimum_paper_days=84, minimum_market_regimes=3)["passed"]
    assert not admission_for(longer, minimum_paper_days=85)["passed"]


@pytest.mark.parametrize("change", ["bare_pass", "lower_days", "lower_regimes", "short_duration",
                                    "missing_coverage", "gap", "duplicate_regime", "bad_count",
                                    "null_start", "nan_days", "bool_days", "pending_incident"])
def test_direct_admission_rejects_incomplete_or_weakened_paper_report(change):
    paper = audit_paper_run(passing_bundle()["paper_observations"])
    assert admission_for(paper)["passed"]
    if change == "bare_pass":
        paper = {"passed": True, "schema_version": 2}
    elif change == "lower_days":
        paper["minimum_days"] = 1
    elif change == "lower_regimes":
        paper["minimum_regimes"] = 1
    elif change == "short_duration":
        paper["elapsed_days"] = 55
    elif change == "missing_coverage":
        del paper["missing_dates"]
    elif change == "gap":
        paper["gaps"] = [{"seconds": 172800}]
    elif change == "duplicate_regime":
        paper["regimes"] = ["RANGE", "RANGE"]
    elif change == "bad_count":
        paper["observation_count"] = 2
    elif change == "null_start":
        paper["start"] = None
    elif change == "nan_days":
        paper["minimum_days"] = float("nan")
    elif change == "bool_days":
        paper["minimum_days"] = True
    elif change == "pending_incident":
        paper["unresolved_incidents"] = 1
    result = admission_for(paper)
    assert not result["passed"] and not result["gates"]["paper_duration_and_regimes"]


@pytest.mark.parametrize("invalid", [0, -1, True, 56.5, "56", float("inf")])
def test_paper_protocol_never_truncates_invalid_duration(invalid):
    bundle = passing_bundle()
    bundle["minimum_paper_days"] = invalid
    with pytest.raises(ValueError, match="positive integers"):
        evaluate_phase6(bundle)


def test_source_path_and_old_id_preserve_distinct_legacy_tasks():
    first = trace_key("docs/live_trading_remediation_plan.md", "BT-01")
    second = trace_key("docs/archive/2026-08-roadmap-consolidation/current_system_remediation_roadmap.md", "BT-01")
    assert first != second
    assert first == trace_key(r"docs\live_trading_remediation_plan.md", " BT-01 ")
    for path in ("../escape.md", "/absolute.md", "C:/outside.md", ""):
        with pytest.raises(ValueError):
            trace_key(path, "BT-01")
    with pytest.raises(ValueError):
        trace_key("docs/a.md", " ")


def small_registry(root):
    (root / "source.md").write_text("BT-01: first meaning", encoding="utf-8")
    (root / "other.md").write_text("BT-01: second meaning", encoding="utf-8")
    (root / "sample.py").write_text("def test_contract():\n    assert True\n", encoding="utf-8")
    return [{"id": "POL-09", "title": "composite key",
             "sources": [{"source_path": "source.md", "old_id": "BT-01"},
                         {"source_path": "other.md", "old_id": "BT-01"}],
             "code": ["sample.py"], "tests": ["sample.py::test_contract"]}]


@pytest.mark.parametrize("change", ["old_id", "test", "duplicate_source", "duplicate_rule", "code"])
def test_registry_rejects_broken_or_ambiguous_evidence(tmp_path, change):
    policies = small_registry(tmp_path)
    assert len(validate_registry(tmp_path, policies)["rules"][0]["sources"]) == 2
    bad = deepcopy(policies)
    if change == "old_id":
        bad[0]["sources"][0]["old_id"] = "BT-99"
    elif change == "test":
        bad[0]["tests"] = ["sample.py::test_absent"]
    elif change == "duplicate_source":
        bad[0]["sources"].append(bad[0]["sources"][0])
    elif change == "duplicate_rule":
        bad += deepcopy(bad)
    elif change == "code":
        bad[0]["code"] = ["missing.py"]
    with pytest.raises(ValueError):
        validate_registry(tmp_path, bad)


def test_evidence_digest_detects_changed_inputs(tmp_path):
    report = validate_registry(tmp_path, small_registry(tmp_path))
    verify_input_identity(tmp_path, report["evidence_inputs_sha256"])
    (tmp_path / "sample.py").write_text("def test_contract():\n    assert False\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        verify_input_identity(tmp_path, report["evidence_inputs_sha256"])


def test_current_registry_covers_all_nine_rules_with_real_sources_and_tests():
    assert [rule["id"] for rule in POLICIES] == [f"POL-{number:02}" for number in range(1, 10)]
    report = validate_registry()
    assert len(report["rules"]) == 9
    assert all(rule["status"] == "references_verified" for rule in report["rules"])
