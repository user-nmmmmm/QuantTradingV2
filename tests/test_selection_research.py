"""Hand-computed synthetic research diagnostics, never effectiveness evidence."""

from dataclasses import replace
import json
import math

import pytest

from analysis.selection_research import (
    ResearchEquity, ResearchFill, ResearchObservation, evaluate_selection,
)


def cohort(day="2024-01-01", horizon=24., returns=(.1, .2, .3, .4), scores=(1., 2., 3., 4.)):
    import pandas as pd
    as_of = pd.Timestamp(day, tz="UTC")
    maturity = as_of + pd.Timedelta(hours=horizon)
    return [ResearchObservation(f"S{i}/USDT", as_of.isoformat(), as_of.isoformat(), score,
                                 True, .25, horizon, maturity.isoformat(), maturity.isoformat(), label)
            for i, (score, label) in enumerate(zip(scores, returns))]


def evaluate(rows=None, **kwargs):
    options = dict(observations=cohort() if rows is None else rows, split="retrospective",
                   evaluated_at="2024-02-01T00:00:00Z", window_start="2024-01-01T00:00:00Z",
                   window_end="2024-01-31T00:00:00Z", quote_currency="USDT", quantiles=2)
    options.update(kwargs)
    return evaluate_selection(**options)


def test_spearman_quantile_means_coverage_and_target_turnover_by_hand():
    result = evaluate()
    group = result["cohorts"][0]
    assert group["rank_ic"]["value"] == pytest.approx(1.)
    assert group["quantile_returns"]["1"]["value"] == pytest.approx(.15)
    assert group["quantile_returns"]["2"]["value"] == pytest.approx(.35)
    assert group["top_minus_bottom"]["value"] == pytest.approx(.2)
    assert group["coverage"]["value"] == 1.
    assert result["target_weight_turnover"]["value"] == 1.
    assert result["decay"][0]["ic_ir"]["value"] is None
    assert result["decay"][0]["ic_ir"]["sample_size"] == 1
    assert result["model_selection_allowed"] is False
    json.dumps(result, allow_nan=False)


def test_ir_uses_sample_std_without_implicit_annualization():
    rows = cohort() + cohort("2024-01-02", returns=(.2, .4, .1, .3))
    result = evaluate(rows)
    # Second rank IC = 0, hence mean = .5, sample std = sqrt(.5).
    decay = result["decay"][0]
    assert decay["mean_rank_ic"]["value"] == pytest.approx(.5)
    assert decay["ic_ir"]["value"] == pytest.approx(.5 / math.sqrt(.5))
    assert decay["ic_ir"]["unit"] == "unannualized_ic_mean_over_sample_std"


@pytest.mark.parametrize("scores,returns", [((1., 1., 1., 1.), (.1, .2, .3, .4)),
                                          ((1., 2., 3., 4.), (.1, .1, .1, .1))])
def test_constant_rank_inputs_are_insufficient_not_zero_or_infinity(scores, returns):
    result = evaluate(cohort(scores=scores, returns=returns))
    ic = result["cohorts"][0]["rank_ic"]
    assert ic["value"] is None
    assert ic["status"] == "insufficient"
    assert ic["reason"] == "constant_scores_or_returns"


def test_constant_ic_across_dates_has_null_ir():
    result = evaluate(cohort() + cohort("2024-01-02"))
    assert result["decay"][0]["mean_rank_ic"]["value"] == pytest.approx(1.)
    assert result["decay"][0]["ic_ir"]["value"] is None


def test_future_labels_are_not_inspected_and_partial_maturity_does_not_select_survivors():
    rows = cohort()
    rows[-1] = replace(rows[-1], mature_at="2024-03-01", label_available_at="2024-03-01",
                        net_forward_return=float("nan"))
    result = evaluate(rows)
    group = result["cohorts"][0]
    assert group["mature_paired_members"] == 3
    assert group["rank_ic"]["status"] == "pending"
    assert group["rank_ic"]["value"] is None
    assert all(metric["value"] is None for metric in group["quantile_returns"].values())
    rows[-1] = replace(rows[-1], net_forward_return=1e100)
    assert evaluate(rows) == result


def test_delayed_label_availability_and_missing_mature_labels_are_distinct():
    rows = cohort()
    rows[-1] = replace(rows[-1], label_available_at="2024-03-01")
    assert evaluate(rows)["cohorts"][0]["rank_ic"]["status"] == "pending"
    rows[-1] = replace(rows[-1], label_available_at="2024-01-02", net_forward_return=None)
    metric = evaluate(rows)["cohorts"][0]["rank_ic"]
    assert metric["status"] == "insufficient"
    assert metric["reason"] == "cohort_has_missing_mature_labels"


def test_ties_stay_in_same_quantile_without_alphabetical_top_bottom_fabrication():
    result = evaluate(cohort(scores=(1., 1., 1., 1.)))
    buckets = result["cohorts"][0]["quantile_returns"]
    assert buckets["1"]["reason"] == "empty_quantile_due_to_ties"
    assert buckets["2"]["sample_size"] == 4
    assert result["cohorts"][0]["top_minus_bottom"]["value"] is None


def test_missing_scores_reduce_coverage_without_imputation():
    rows = cohort()
    rows[-1] = replace(rows[-1], score=None, target_weight=0.)
    result = evaluate(rows)
    assert result["cohorts"][0]["coverage"]["value"] == .75
    assert result["cohorts"][0]["rank_ic"]["sample_size"] == 3


def test_small_cross_section_and_empty_input_produce_explicit_insufficiency():
    result = evaluate(cohort()[:2])
    assert result["cohorts"][0]["rank_ic"]["reason"] == "cross_section_too_small"
    assert evaluate([])["decay"] == []
    assert evaluate([])["target_weight_turnover"]["status"] == "insufficient"
    assert evaluate()["executed_turnover"]["reason"] == "no_equity_observations_in_window"


def test_decay_reports_all_predeclared_horizons_without_selecting_best():
    rows = cohort(horizon=24.) + cohort(horizon=48., returns=(.4, .3, .2, .1))
    result = evaluate(rows)
    assert [row["horizon_hours"] for row in result["decay"]] == [24., 48.]
    assert [row["mean_rank_ic"]["value"] for row in result["decay"]] == pytest.approx([1., -1.])
    assert result["target_weight_turnover"]["value"] == 1.  # Not twice for two horizons.
    assert "selected_horizon" not in result


def test_realized_turnover_uses_gross_fills_and_observed_equity_not_target_changes():
    fills = [ResearchFill("f1", "S0/USDT", "2024-01-01", "buy", 2., 100., .2),
             ResearchFill("f2", "S0/USDT", "2024-01-02", "sell", 1., 110., .11),
             ResearchFill("outside", "S0/USDT", "2024-02-02", "sell", 20., 200., 4.)]
    equities = [ResearchEquity("2024-01-01", 1000.), ResearchEquity("2024-01-02", 1100.)]
    result = evaluate(fills=fills, equity_observations=equities)
    assert result["executed_notional"] == 310.
    assert result["executed_turnover"]["value"] == pytest.approx(310. / 1050.)
    assert result["executed_turnover"]["sample_size"] == 2
    assert result["actual_fees"] == pytest.approx(.31)


def test_target_turnover_counts_departures_and_stays_separate_from_fills():
    first = cohort()
    second = [replace(r, target_weight=.5 if i < 2 else 0.) for i, r in enumerate(cohort("2024-01-02"))]
    result = evaluate(first + second)
    assert result["target_weight_turnover"]["value"] == 2.  # Initial 1 + .25 * 4 changes.
    assert result["executed_turnover"]["value"] is None


def test_final_diagnostics_are_refused_even_for_already_mature_supplied_labels():
    with pytest.raises(ValueError, match="one-time adjudication"):
        evaluate(split="final")
    result = evaluate(split="validation")
    assert result["model_selection_allowed"] is False
    assert result["research_effectiveness"] == "not_adjudicated"
    assert result["formal_routing_enabled"] is False


@pytest.mark.parametrize("changed,message", [
    ({"score_available_at": "2024-01-02"}, "score was not available"),
    ({"mature_at": "2024-01-01T12:00:00Z"}, "maturity"),
    ({"label_available_at": "2024-01-01T12:00:00Z"}, "maturity"),
    ({"net_forward_return": -1.1}, "less than -1"),
    ({"net_forward_return": float("inf")}, "finite"),
    ({"target_weight": 1.1}, "long-only"),
    ({"eligible": False}, "cannot carry target"),
])
def test_invalid_time_return_or_weight_contracts_are_refused(changed, message):
    rows = cohort()
    rows[0] = replace(rows[0], **changed)
    with pytest.raises(ValueError, match=message):
        evaluate(rows)


def test_duplicate_aliases_and_inconsistent_horizon_copies_are_refused():
    rows = cohort()
    with pytest.raises(ValueError, match="duplicate normalized"):
        evaluate(rows + [replace(rows[0], symbol="S0-USDT")])
    with pytest.raises(ValueError, match="copies disagree"):
        evaluate(rows + [replace(cohort(horizon=48.)[0], target_weight=.1)])
    with pytest.raises(ValueError, match="same contemporaneous universe"):
        evaluate(rows + cohort(horizon=48.)[:3])


def test_duplicate_fills_and_equity_points_cannot_inflate_turnover():
    fill = ResearchFill("f1", "S0", "2024-01-01", "buy", 1., 100., .1)
    with pytest.raises(ValueError, match="fill identities"):
        evaluate(fills=[fill, fill])
    with pytest.raises(ValueError, match="duplicate equity"):
        evaluate(equity_observations=[ResearchEquity("2024-01-01", 1000.),
                                      ResearchEquity("2024-01-01T08:00:00+08:00", 1000.)])


@pytest.mark.parametrize("kwargs,message", [
    ({"split": "full_sample"}, "split"),
    ({"window_end": "2024-03-01"}, "reporting window"),
    ({"min_cross_section": 2}, "min_cross_section"),
    ({"quantiles": 1}, "quantiles"),
    ({"quote_currency": ""}, "quote_currency"),
])
def test_invalid_evaluation_contract_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        evaluate(**kwargs)
