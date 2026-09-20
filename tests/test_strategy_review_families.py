"""Family reports preserve paired windows, single-family scope and incomplete evidence."""
from __future__ import annotations

from copy import deepcopy
import json

import pandas as pd
import pytest

from scripts.run_strategy_review import matrix as registered_matrix
from scripts.summarize_strategy_review_families import FAMILY_COUNTS, assess, run


def _protocol():
    parameters = {
        "state": {"stability_period": 5, "ma_fast": 20},
        "router": {"cooldown_bars": 2},
        "routing": {"TREND_UP": "TrendBreakout", "TREND_DOWN": "Cash", "SIDEWAYS": "Cash", "VOLATILE": "Cash"},
        "strategy_health": {"enabled": True}, "risk": {"max_drawdown_limit": .2},
        "candidate_scoring": {"enabled": True, "weights": {"breakout_extent": 1.0, "trend_strength": .5, "volume_confirmation": .3, "liquidity": .2}},
        "stops": {"use_atr_initial_stop": False, "use_trailing_stop": False, "initial_atr_multiple": 2.0, "trailing_atr_multiple": 3.0},
    }
    return {"parameters": parameters, "arms": registered_matrix(parameters),
        "rolling_windows": [{"name": f"rolling_{index:02d}", "start": f"2024-{index+1:02d}-01", "end": f"2024-{index+1:02d}-20"} for index in range(11)],
        "gates": {"minimum_cohorts": 30}}


def _runs(protocol):
    return pd.DataFrame([{
        "name": f"{arm['arm']}__{window['name']}", "phase": "matrix", "start": window["start"], "end": window["end"],
        "return_pct": 5.0 + index, "max_drawdown_pct": 10.0 - index / 10,
        "initial_capital": 10000, "cohort_count": 40, "cohort_sample": "sufficient",
        "statistical_gate_status": "pass", "cohort_admission_status": "cost_complete",
    } for index, arm in enumerate(protocol["arms"]) for window in protocol["rolling_windows"]])


def _summary(protocol, rows):
    return pd.DataFrame([{"arm": arm["arm"], "windows": sum(rows.name.str.startswith(arm["arm"] + "__")),
        "rolling_gate": "pass" if sum(rows.name.str.startswith(arm["arm"] + "__")) == 11 else "pending",
        "positive_windows": sum(rows.name.str.startswith(arm["arm"] + "__")), "median_return_pct": 5}
        for arm in protocol["arms"]])


def test_complete_families_reuse_one_baseline_without_creating_winner_or_admission():
    protocol = _protocol()
    rows = _runs(protocol)
    report, pairs, arms = assess(protocol, _summary(protocol, rows), rows)
    assert report["pipeline_status"] == "complete" and report["completed_unique_engine_runs"] == 572
    assert {row["family"]: row["registered_configurations"] for row in report["families"]} == FAMILY_COUNTS
    assert len(pairs) == 572 and len(arms) == 55
    assert report["family_window_references"] == 605
    assert sum(row["shared_baseline"] for row in arms) == 4
    assert {row["research_status"] for row in arms} == {"insufficient"}
    assert report["selected_candidate"] is None and not report["added_acceptance_thresholds"]
    assert all(row["rolling_gate_observed"] == "pass" for row in arms)


def test_differences_pair_same_registered_window_and_use_correct_signs():
    protocol = _protocol()
    rows = _runs(protocol)
    _, pairs, _ = assess(protocol, _summary(protocol, rows), rows)
    row = next(item for item in pairs if item["arm"] == "timing_s2_c0" and item["window"] == "rolling_01")
    assert row["return_delta_percentage_points"] == 1
    assert row["drawdown_delta_percentage_points"] == pytest.approx(-.1)
    baseline = [item for item in pairs if item["arm"] == "baseline"]
    assert all(item["return_delta_percentage_points"] == item["drawdown_delta_percentage_points"] == 0 for item in baseline)


def test_missing_baseline_window_does_not_turn_unknown_differences_into_zero():
    protocol = _protocol()
    rows = _runs(protocol)
    rows = rows.loc[rows.name.ne("baseline__rolling_00")]
    report, pairs, _ = assess(protocol, _summary(protocol, rows), rows)
    pending = [row for row in pairs if row["comparison_status"] == "pending"]
    assert report["pipeline_status"] == "pending" and report["completed_unique_engine_runs"] == 571
    assert len(pending) == 52
    assert all(row["return_delta_percentage_points"] is None for row in pending)
    assert all(row["research_status"] == "pending" for row in report["families"])


def test_unsupported_cohort_count_stays_separate_from_realized_account_return():
    protocol = _protocol()
    rows = _runs(protocol)
    rows.loc[rows.name.eq("timing_s2_c0__rolling_00"), ["cohort_count", "statistical_gate_status"]] = [12, "insufficient"]
    _, pairs, details = assess(protocol, _summary(protocol, rows), rows)
    row = next(item for item in pairs if item["source_run"] == "timing_s2_c0__rolling_00")
    assert row["arm_count_support_status"] == "insufficient"
    assert row["return_delta_percentage_points"] == 1
    arm = next(item for item in details if item["arm"] == "timing_s2_c0")
    assert arm["count_supported_windows"] == 10 and arm["statistical_windows_insufficient"] == 1


def test_only_family_parameter_changes_are_enforced_and_fixed_parameters_disclosed():
    protocol = _protocol()
    rows = _runs(protocol)
    report, _, _ = assess(protocol, _summary(protocol, rows), rows)
    timing = next(row for row in report["families"] if row["family"] == "timing")
    assert timing["registered_variable_paths"] == ["router.cooldown_bars", "state.stability_period"]
    assert timing["fixed_parameters"]["risk.max_drawdown_limit"] == .2
    changed = deepcopy(protocol)
    changed["arms"][1]["parameters"]["risk"]["max_drawdown_limit"] = .1
    with pytest.raises(ValueError, match="outside timing"):
        assess(changed, _summary(protocol, rows), rows)


@pytest.mark.parametrize("problem", ["duplicate", "unregistered", "period", "capital", "summary_count", "missing_summary"])
def test_mismatched_results_are_rejected_instead_of_silently_paired(problem):
    protocol = _protocol()
    rows = _runs(protocol)
    summary = _summary(protocol, rows)
    if problem == "duplicate":
        rows = pd.concat([rows, rows.iloc[:1]], ignore_index=True)
    elif problem == "unregistered":
        rows.loc[0, "name"] = "other__rolling_00"
    elif problem == "period":
        rows.loc[0, "end"] = "2024-01-21"
    elif problem == "capital":
        rows.loc[11, "initial_capital"] = 20000
    elif problem == "summary_count":
        summary.loc[0, "windows"] = 10
    else:
        summary = summary.loc[summary.arm.ne("baseline")]
    with pytest.raises(ValueError):
        assess(protocol, summary, rows)


def test_empty_partial_run_has_registered_counts_and_pending_without_false_success():
    protocol = _protocol()
    report, pairs, details = assess(protocol, pd.DataFrame(), pd.DataFrame())
    assert report["completed_unique_engine_runs"] == 0
    assert report["research_status"] == "pending" and report["pipeline_status"] == "pending"
    assert len(pairs) == 572 and len(details) == 55
    assert all(row["return_delta_percentage_points"] is None for row in pairs)


def test_report_outputs_do_not_modify_published_inputs(tmp_path):
    protocol = _protocol()
    (tmp_path / "review_protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    rows = _runs(protocol)
    rows.to_csv(tmp_path / "all_run_summaries.csv", index=False)
    _summary(protocol, rows).to_csv(tmp_path / "matrix_summary.csv", index=False)
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    report = run(tmp_path)
    assert report["pipeline_status"] == "complete"
    assert all((tmp_path / name).read_bytes() == payload for name, payload in original.items())
    assert len(pd.read_csv(tmp_path / "family_paired_window_differences.csv")) == 572
    assert len(pd.read_csv(tmp_path / "family_assessment.csv")) == 5
    assert len(pd.read_csv(tmp_path / "family_arm_assessment.csv")) == 55
