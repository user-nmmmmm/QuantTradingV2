"""Support and participation checks do not manufacture independent market data."""

from copy import deepcopy

import pytest

from research.ml_selection.support_diagnostics import (
    selection_support_diagnostics,
    participation_matched_baseline,
    compare_selection_support,
)


def rows():
    return [
        {
            "event_id": f"E{n}",
            "timestamp": f"2024-01-0{1 + n // 2}T12:00:00Z",
            "book": "trend",
            "symbol": "A" if n % 2 else "B",
            "state": "bull",
            "eligible": True,
            "selected": n % 2 == 0,
            "reason": "order_accepted" if n % 2 == 0 else "model_gate",
        }
        for n in range(4)
    ]


def test_seed_repetition_never_increases_dates_events_or_concentration_ess():
    original = rows()
    repeated = [{**row, "seed": seed} for seed in range(10) for row in original]
    a, b = selection_support_diagnostics(original), selection_support_diagnostics(repeated)
    assert a["independent_date_count"] == b["independent_date_count"] == 2
    assert a["unique_market_events"] == b["unique_market_events"] == 4
    assert a["kish_event_concentration_ess"] == b["kish_event_concentration_ess"] == 4.0
    assert a["kish_date_concentration_ess"] == b["kish_date_concentration_ess"] == 2.0
    assert b["repeated_event_rows"] == 36
    assert b["economic_value"] is None and not b["formal_admission"]


def test_all_gate_causes_and_unknown_are_distinct():
    reasons = [
        "no_signal",
        "ineligible",
        "model_gate",
        "cash_limit",
        "health_paused",
        "risk_limit",
        "order_accepted",
        "unknown",
    ]
    candidates = [
        {"event_id": f"E{n}", "timestamp": "2024-01-01T00:00:00Z", "reason": reason}
        for n, reason in enumerate(reasons)
    ]
    report = selection_support_diagnostics(candidates)
    assert all(
        report["stage_counts"][key] == 1
        for key in [
            "no_signal",
            "ineligible",
            "model_rejected",
            "capital_competition",
            "health_paused",
            "risk_rejected",
            "unfilled",
            "unknown_unvisited_gate",
        ]
    )
    assert len(report["strata"]) == 1


def test_existing_minimum_support_is_preserved_not_lowered_for_zero_trades():
    report = selection_support_diagnostics(rows(), minimum_dates=10, minimum_events=20)
    assert report["status"] == "insufficient"
    assert report["existing_support_policy"]["minimum_dates"] == 10
    assert not report["existing_support_policy"]["P1_P2_admission_thresholds_changed"]
    assert selection_support_diagnostics([])["status"] == "insufficient"


def test_kish_concentration_is_independent_of_row_count():
    inputs = rows()
    inputs[0]["support_weight"] = 100.0
    assert selection_support_diagnostics(inputs)["kish_event_concentration_ess"] < 1.1
    inputs[0]["support_weight"] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        selection_support_diagnostics(inputs)


def test_matched_control_has_same_participation_per_day_and_is_not_an_account_result():
    original = rows()
    control = participation_matched_baseline(original, original, seed=42)
    assert control == participation_matched_baseline(original, original, seed=42)
    for day in ("2024-01-01", "2024-01-02"):
        assert sum(row["selected"] for row in control if row["timestamp"].startswith(day)) == 1
    assert all(not row["counterfactual_account_evaluated"] for row in control)
    compare = compare_selection_support(
        {name: control for name in ("rule", "ridge", "frozen_rl", "matched_participation")}
    )
    assert compare["status"] == "diagnostic"
    assert compare["independent_date_count"] == 2
    assert compare["economic_result"] is None and compare["ranking_allocation_value"] is None


def test_no_shared_universe_no_comparison_and_no_zero_fill_for_missing_model():
    original = rows()
    with pytest.raises(ValueError, match="same unique opportunities"):
        participation_matched_baseline(original, original[:-1])
    report = compare_selection_support({"rule": original})
    assert report["status"] == "insufficient"
    assert report["missing_arms"] == ["frozen_rl", "matched_participation", "ridge"]
    alternate = deepcopy(original)
    alternate[0]["event_id"] = "different"
    report = compare_selection_support({"rule": original, "ridge": alternate})
    assert not report["same_opportunity_universe"]


def test_filled_final_status_does_not_hide_preceding_observed_cash_limit():
    inputs = [
        {
            "event_id": "E",
            "timestamp": "2024-01-01T12:00:00Z",
            "filled_qty": 0.5,
            "selected": True,
            "reason": "order_accepted",
            "gate_facts": [{"reason": "cash_limit"}, {"reason": "order_accepted"}],
        }
    ]
    report = selection_support_diagnostics(inputs)
    assert report["stage_counts"]["filled"] == 1
    assert report["observed_gate_chain_counts"]["capital_competition"] == 1
    assert report["observed_capital_limited_events"] == 1
    assert report["economic_value"] is None


def test_missing_nan_signal_ids_use_actual_opportunities_instead_of_one_fake_event():
    inputs = rows()
    for row in inputs:
        row.pop("event_id")
        row["signal_id"] = float("nan")
    report = selection_support_diagnostics(inputs)
    assert report["independent_date_count"] == 2
    assert report["unique_market_events"] == 4


def test_missing_historical_selection_actions_stay_unknown_not_zero_participation():
    original = rows()
    for row in original:
        row.pop("selected")
    report = selection_support_diagnostics(original)
    assert report["selection_action_known_rows"] == 0
    assert report["selected_rows"] is None
    assert report["participation_rate"] is None
    result = compare_selection_support(
        {name: original for name in ("rule", "ridge", "frozen_rl", "matched_participation")}
    )
    assert result["status"] == "insufficient"
    assert len(result["unknown_action_arms"]) == 4
    with pytest.raises(ValueError, match="known selected"):
        participation_matched_baseline(rows(), original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timestamp", "2024-01-03T12:00:00Z"),
        ("book", "other"),
        ("symbol", "other"),
        ("eligible", False),
    ],
)
def test_same_event_ids_cannot_hide_different_opportunities(field, value):
    original = rows()
    reference = deepcopy(original)
    reference[0][field] = value
    with pytest.raises(ValueError, match="different opportunity"):
        participation_matched_baseline(original, reference)
    comparison = compare_selection_support({"rule": original, "ridge": reference})
    assert not comparison["same_opportunity_universe"]
    assert comparison["status"] == "insufficient"
