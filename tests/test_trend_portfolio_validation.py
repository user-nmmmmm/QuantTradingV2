from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from analysis.trend_portfolio_validation import (
    AcceptanceConfig,
    cohort_statistics,
    evaluate_trend_portfolio,
    exit_day_cohorts,
    selection_bias_evidence,
)


def event(index, pnl=10.0, reason="ChandelierStop", timestamp=None):
    return {"close_event_id": f"event-{index}",
            "timestamp": timestamp or (pd.Timestamp("2025-10-01") + pd.Timedelta(days=index)).isoformat(),
            "realized_pnl": pnl, "exit_reason": reason, "symbol": "BTC-USDT",
            "opening_strategy_id": "TrendPortfolioV2"}


def primary():
    return {"run_id": "candidate", "role": "primary", "variant": "V2-C", "venue": "binance",
            "timeframe": "1d", "start": "2025-01-01", "end": "2026-01-01",
            "stability": 3, "cooldown": 0, "symbols": ["BTC-USDT", "ETH-USDT"],
            "return_pct": 30.0, "max_drawdown_pct": 5.0,
            "buyhold_risk_matched_return_pct": 10.0, "accounting_check": {"ok": True},
            "financing_total": 0.0, "financing_gross": 0.0, "cohort_financing_allocated": False,
            "close_event_records": [event(i, -1 if i % 5 == 0 else 10) for i in range(60)],
            "trades": [{"side": "buy", "fill_time": "2025-10-01", "qty": 1.0}],
            "daily_returns": [0.02, 0.01, -0.001] * 100}


def suite():
    main = primary()
    rows = [main]
    for multiplier in (1.5, 2.0):
        rows.append({**deepcopy(main), "role": "cost", "cost_multiplier": multiplier,
                     "return_pct": 28.0 if multiplier == 1.5 else 25.0})
    for i, value in enumerate((1.0, 2.0, -1.0)):
        rows.append({**deepcopy(main), "role": "rolling", "return_pct": value,
                     "start": f"2025-0{i + 1}-01"})
    for stability in (2, 5):
        rows.append({**deepcopy(main), "role": "neighbor", "stability": stability,
                     "return_pct": 25.0})
    rows.append({**deepcopy(main), "role": "venue", "venue": "okx", "return_pct": 20.0})
    rows.append({**deepcopy(main), "role": "recent", "start": "2025-01-01", "return_pct": 2.0})
    return rows


def trial_metadata():
    return {"historical_trials_complete": True, "registered_before_results": True,
            "total_trials": 3, "trial_sharpes": [0.05, 0.1, 0.2], "current_run_count": 3}


def gates_for(rows, **kwargs):
    return evaluate_trend_portfolio(rows, **kwargs)["primary_runs"][0]["gates"]


def test_same_utc_exit_day_collapses_symbols_controllers_and_partial_closes():
    rows = [event(1, 10, timestamp="2025-10-01T23:00:00+00:00"),
            {**event(2, -3, "CircuitBreaker", timestamp="2025-10-02T07:30:00+08:00"),
             "symbol": "ETH-USDT", "opening_strategy_id": "Router"}]
    evidence = exit_day_cohorts(rows)
    assert evidence["cohort_count"] == 1
    assert evidence["groups"][0]["net_pnl"] == 7
    assert evidence["groups"][0]["close_event_count"] == 2


def test_time_exit_attribution_and_terminal_mark_are_separate():
    evidence = exit_day_cohorts([event(0, -10), event(1, 100, "MaxHoldingPeriod"),
                                event(2, 1000, "EndOfBacktest")])
    assert evidence["net_closed_pnl"] == 90
    assert evidence["max_holding_period_pnl"] == 100
    assert evidence["net_closed_pnl_without_max_holding_period"] == -10
    assert evidence["excluded_terminal_pnl"] == 1000
    assert evidence["cohort_count"] == 2


@pytest.mark.parametrize("bad", [event(0), {**event(1), "realized_pnl": float("nan")},
                                 {**event(2), "timestamp": None}])
def test_duplicate_and_invalid_events_cannot_certify_evidence(bad):
    evidence = exit_day_cohorts([event(0), bad])
    assert evidence["status"] == "invalid"
    assert len(evidence["invalid_events"]) == 1


def test_bootstrap_is_deterministic_and_serializable_with_zero_loss_draws():
    evidence = exit_day_cohorts([event(i) for i in range(50)])
    first = cohort_statistics(evidence)
    assert first == cohort_statistics(evidence)
    assert first["lower_unbounded"]
    assert first["pf_confidence_interval"] == [None, None]
    json.dumps(first, allow_nan=False)
    zeros = cohort_statistics(exit_day_cohorts([event(i, 0) for i in range(30)]))
    assert zeros["pf_confidence_interval"] == [0.0, 0.0]
    assert not zeros["lower_unbounded"]


def test_top_winner_stress_removes_positive_cohorts_only():
    evidence = exit_day_cohorts([event(0, 100), event(1, -2), event(2, -3)])
    stats = cohort_statistics(evidence)
    for count in (1, 3, 5, 10):
        assert stats["concentration"][str(count)] == {
            "removed_count": 1, "remaining_net_closed_pnl": -5.0}


def test_complete_descriptive_evidence_still_requires_fresh_data():
    report = evaluate_trend_portfolio(suite(), trial_metadata=trial_metadata())
    assert report["engineering_status"] == "completed"
    assert report["research_status"] == "passed_descriptive_gates"
    assert report["fresh_evidence_status"] == "unavailable"
    assert report["admission_status"] == "not_admitted"
    assert report["live_admission"] is False
    assert all(item["status"] == "pass" for item in report["primary_runs"][0]["gates"].values())
    json.dumps(report, allow_nan=False)


def test_fresh_evidence_allows_review_never_live_admission():
    fresh = {"kind": "forward", "registered_before_evaluation": True,
             "used_for_selection": False, "completed": True, "validation_verified": True,
             "protocol_hash": "protocol", "data_hash": "data", "code_hash": "code"}
    report = evaluate_trend_portfolio(suite(), trial_metadata=trial_metadata(), fresh_evidence=fresh)
    assert report["admission_status"] == "eligible_for_independent_review"
    assert report["live_admission"] is False


def test_failed_research_is_distinct_from_completed_engineering():
    rows = suite()
    rows[0]["return_pct"] = -1
    rows[0]["close_event_records"] = [event(0, -20), event(1, 100, "MaxHoldingPeriod")]
    report = evaluate_trend_portfolio(rows)
    assert report["engineering_status"] == "completed"
    assert report["research_status"] == "failed"
    gates = report["primary_runs"][0]["gates"]
    assert gates["profit_without_time_exits"]["status"] == "fail"
    assert gates["minimum_exit_cohorts"]["observed"] == 2


def test_missing_validation_inputs_never_pass():
    row = primary()
    del row["buyhold_risk_matched_return_pct"]
    del row["trades"]
    gates = gates_for([row])
    for key in ("risk_matched_buyhold_excess", "rolling_windows", "cost_1.5x", "cost_2x",
                "neighbor_plateau", "venue_direction", "post_2022_activity", "selection_bias"):
        assert gates[key]["status"] == "unavailable"


def test_rolling_two_thirds_boundary_and_duplicate_windows():
    rows = suite()
    assert gates_for(rows)["rolling_windows"]["status"] == "pass"
    rolling = [row for row in rows if row["role"] == "rolling"]
    rolling[1]["return_pct"] = 0
    assert gates_for(rows)["rolling_windows"]["status"] == "fail"
    rolling[1]["start"] = rolling[0]["start"]
    assert gates_for(rows)["rolling_windows"]["status"] == "unavailable"


def test_cohorts_are_never_pooled_across_venues_or_rolling_windows():
    rows = suite()
    rows[0]["close_event_records"] = rows[0]["close_event_records"][:10]
    report = evaluate_trend_portfolio(rows)
    assert report["primary_runs"][0]["cohorts"]["cohort_count"] == 10
    assert report["primary_runs"][0]["gates"]["minimum_exit_cohorts"]["status"] == "fail"


def test_venue_and_cost_results_must_match_timeframe_universe_and_period():
    rows = suite()
    next(row for row in rows if row["role"] == "venue")["timeframe"] = "4h"
    next(row for row in rows if row.get("cost_multiplier") == 1.5)["symbols"] = ["BTC-USDT"]
    next(row for row in rows if row.get("cost_multiplier") == 2)["end"] = "2025-12-01"
    gates = gates_for(rows)
    for key in ("venue_direction", "cost_1.5x", "cost_2x"):
        assert gates[key]["status"] == "unavailable"


def test_double_cost_explicit_catastrophic_flip_threshold():
    rows = suite()
    stressed = next(row for row in rows if row.get("cost_multiplier") == 2)
    stressed["return_pct"] = 19.9  # More than ten percentage points lost.
    assert gates_for(rows)["cost_2x"]["status"] == "fail"
    stressed["return_pct"] = 20
    assert gates_for(rows)["cost_2x"]["status"] == "pass"


def test_reported_benchmark_that_cannot_match_risk_is_not_acceptance_evidence():
    rows = suite()
    rows[0]["benchmark_diagnostic"] = {"risk_match_verified": False}
    assert gates_for(rows)["risk_matched_buyhold_excess"]["status"] == "unavailable"


def test_recent_replay_must_be_positive_and_match_the_declared_recent_period():
    rows = suite()
    recent = next(row for row in rows if row["role"] == "recent")
    recent["return_pct"] = -1
    assert gates_for(rows)["recent_activity"]["status"] == "pass"
    assert gates_for(rows)["recent_window_return"]["status"] == "fail"
    recent["start"] = "2025-02-01"
    assert gates_for(rows)["recent_window_return"]["status"] == "unavailable"


@pytest.mark.parametrize("total,gross", [(10.0, 10.0), (0.0, 20.0), (None, None)])
def test_financing_without_allocation_invalidates_closed_pnl_claims_not_equity(total, gross):
    rows = suite()
    rows[0].update(financing_total=total, financing_gross=gross, cohort_financing_allocated=True)
    gates = gates_for(rows)
    for name in ("cohort_financing_completeness", "cohort_pf_lower_bound", "remove_top_cohorts",
                 "profit_without_time_exits", "post_2022_activity", "recent_activity"):
        assert gates[name]["status"] == "unavailable"
    assert gates["positive_net_return"]["status"] == "pass"
    assert gates["recent_window_return"]["status"] == "pass"


def test_neighbor_plateau_rejects_duplicate_or_unregistered_parameter_points():
    rows = suite()
    neighbors = [row for row in rows if row["role"] == "neighbor"]
    neighbors[1]["stability"] = neighbors[0]["stability"]
    assert gates_for(rows)["neighbor_plateau"]["status"] == "unavailable"
    neighbors[1]["stability"] = 99
    assert gates_for(rows)["neighbor_plateau"]["status"] == "unavailable"


def test_full_registered_grid_uses_adjacent_points_but_counts_diagonal_trials():
    rows = suite()
    for stability, cooldown in ((2, 2), (3, 2), (5, 2)):
        rows.append({**deepcopy(rows[0]), "role": "neighbor", "stability": stability,
                     "cooldown": cooldown, "return_pct": 25 if stability == 3 else -100})
    gate = gates_for(rows)["neighbor_plateau"]
    assert gate["status"] == "pass"
    assert gate["tested_neighbors"] == 3
    assert gate["registered_grid_observations"] == 5
    assert sorted(gate["assessed_points"]) == [[2, 0], [3, 2], [5, 0]]


def test_terminal_valuation_microsecond_after_period_is_not_a_real_trade():
    rows = suite()
    rows[0]["close_event_records"].append(
        event(999, 1000, "EndOfBacktest", timestamp="2026-01-01T00:00:00.000001"))
    gates = gates_for(rows)
    assert gates["event_period_alignment"]["status"] == "pass"
    report = evaluate_trend_portfolio(rows)
    assert report["primary_runs"][0]["cohorts"]["cohort_count"] == 60


def test_old_profits_and_new_fill_without_positive_recent_contribution_fail():
    rows = suite()
    for item in rows[0]["close_event_records"]:
        item["realized_pnl"] = -1
    gates = gates_for(rows)
    assert gates["recent_activity"]["entry_fill_count"] == 1
    assert gates["recent_activity"]["status"] == "fail"
    rows[0]["close_event_records"] = [event(0, timestamp="2027-01-01")]
    assert gates_for(rows)["event_period_alignment"]["status"] == "fail"


def test_unknown_prior_trials_only_produce_conditional_dsr_diagnostic():
    metadata = {**trial_metadata(), "historical_trials_complete": False}
    result = selection_bias_evidence(np.asarray(primary()["daily_returns"]), metadata)
    assert result["status"] == "unavailable"
    assert result["probability"] is None
    assert result["diagnostic_probability"] > .95
    assert result["diagnostic_scope"] == "observed_trials_only"


def test_dsr_accounts_for_cross_trial_dispersion_and_consistent_period_units():
    returns = primary()["daily_returns"]
    low = selection_bias_evidence(returns, trial_metadata())
    high = selection_bias_evidence(returns, {**trial_metadata(), "trial_sharpes": [-10, 0, 10]})
    assert low["expected_max_period_sharpe"] < high["expected_max_period_sharpe"]
    assert low["probability"] > high["probability"]
    assert low["status"] == "pass"
    assert high["status"] == "fail"


def test_threshold_configuration_rejects_invalid_bootstrap_and_fraction():
    with pytest.raises(ValueError):
        AcceptanceConfig(bootstrap_iterations=0)
    with pytest.raises(ValueError):
        AcceptanceConfig(positive_rolling_fraction=2)
