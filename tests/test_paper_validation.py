"""Counterexamples and hand-calculated contracts for paper diagnostics."""
import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from analysis.paper_validation import (
    assemble_cpcv_paths,
    block_bootstrap_indices,
    cpcv_splits,
    cscv_pbo,
    deflated_sharpe_evidence,
    white_reality_check,
)


def panel(values, columns=None):
    array = np.asarray(values, dtype=float)
    return pd.DataFrame(array, index=pd.date_range("2020-01-01", periods=len(array), tz="UTC"),
                        columns=columns or [f"candidate_{i}" for i in range(array.shape[1])])


def zero_benchmark(frame):
    return pd.Series(0.0, index=frame.index, name="fixed_benchmark")


def test_cscv_complete_small_matrix_has_hand_calculated_selection_and_ranks():
    # Each candidate alternates superiority between the two time blocks.
    frame = panel([[3, -3], [1, -1], [-3, 3], [-1, 1]], ["A", "B"])
    result = cscv_pbo(frame, n_groups=2, family_complete=True)
    assert result["status"] == "ok"
    assert result["probability"] == 1.0
    assert result["computed_splits"] == 2
    first, second = result["outcomes"]
    assert [first["selected_candidate"], second["selected_candidate"]] == ["A", "B"]
    assert first["train_score"] == pytest.approx(math.sqrt(2))
    assert first["test_score"] == pytest.approx(-math.sqrt(2))
    assert first["oos_rank"] == 1
    assert first["relative_oos_rank"] == pytest.approx(1 / 3)
    assert first["logit"] == pytest.approx(math.log(.5))
    assert result["admission_eligible"] is False
    json.dumps(result, allow_nan=False)


def test_cscv_enumerates_all_complements_and_retains_local_diagnostic():
    frame = panel([[3, -3], [1, -1], [3, -3], [1, -1],
                   [-3, 3], [-1, 1], [-3, 3], [-1, 1]])
    result = cscv_pbo(frame, n_groups=4, score="mean")
    assert result["expected_splits"] == result["computed_splits"] == 6
    assert result["status"] == "diagnostic"
    assert result["probability"] is None
    assert result["diagnostic_probability"] == 1
    for row in result["outcomes"]:
        assert set(row["train_groups"]).isdisjoint(row["test_groups"])
        assert set(row["train_groups"]) | set(row["test_groups"]) == set(range(4))


def test_cscv_ties_use_first_input_candidate_and_average_oos_rank_conservatively():
    frame = panel([[1, 1, 1], [-1, -1, -1]] * 4, ["Z", "A", "B"])
    result = cscv_pbo(frame, n_groups=4, family_complete=True)
    assert result["probability"] == 1
    assert all(row["selected_candidate"] == "Z" for row in result["outcomes"])
    assert all(row["relative_oos_rank"] == .5 for row in result["outcomes"])
    assert all(row["is_maximum_ties"] == 3 for row in result["outcomes"])


@pytest.mark.parametrize("change", ["missing", "duplicate_dates", "reverse", "duplicate_columns"])
def test_candidate_panel_rejects_axis_or_missing_data_without_silent_intersection(change):
    frame = panel([[1, -1], [-1, 1]] * 20)
    if change == "missing":
        frame.iloc[2, 0] = np.nan
    elif change == "duplicate_dates":
        frame.index = frame.index[:-1].append(frame.index[-2:-1])
    elif change == "reverse":
        frame = frame.iloc[::-1]
    else:
        frame.columns = ["same", "same"]
    assert cscv_pbo(frame, n_groups=4)["status"] == "invalid"
    assert white_reality_check(frame, zero_benchmark(frame))["status"] == "invalid"
    assert deflated_sharpe_evidence(frame, frame.columns[0])["status"] == "invalid"


def test_cscv_never_drops_undefined_trial_or_unbalanced_tail():
    result = cscv_pbo(panel([[0, 1], [0, -1]] * 4), n_groups=4, family_complete=True)
    assert result["status"] == "insufficient"
    assert result["probability"] is None
    assert len(result["invalid_splits"]) == 6
    result = cscv_pbo(panel([[1, -1], [-1, 1]] * 5), n_groups=4)
    assert result["status"] == "insufficient"
    assert "divisible" in result["reason"]
    assert result["outcomes"] == []


def event_plan(*, duration=0, embargo=0):
    starts = pd.date_range("2020-01-01", periods=24, tz="UTC")
    return cpcv_splits(starts, starts + pd.Timedelta(days=duration), n_groups=4,
                       n_test_groups=2, embargo_fraction=embargo)


def test_cpcv_generates_genuine_combinations_and_complete_unique_test_cells():
    plan = event_plan()
    assert plan["status"] == "ok"
    assert plan["split_count"] == 6
    assert plan["path_count"] == 3
    assert [row["test_groups"] for row in plan["splits"]] == [
        [0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]]
    seen = set()
    for path in plan["paths"]:
        assert [segment["group_id"] for segment in path["segments"]] == list(range(4))
        assert [i for segment in path["segments"] for i in segment["positions"]] == list(range(24))
        for segment in path["segments"]:
            cell = (segment["split_id"], segment["group_id"])
            assert cell not in seen
            seen.add(cell)
    assert len(seen) == 6 * 2
    # Non-contiguous test groups do not purge the entire gap between them.
    assert plan["splits"][1]["train"] == list(range(6, 12)) + list(range(18, 24))
    json.dumps(plan, allow_nan=False)


def test_cpcv_purges_label_overlap_and_applies_each_test_group_embargo():
    plan = event_plan(duration=2, embargo=.04)
    assert plan["status"] == "ok"
    starts, ends = pd.to_datetime(plan["event_starts"]), pd.to_datetime(plan["event_ends"])
    for split in plan["splits"]:
        assert set(split["train"]).isdisjoint(split["test"])
        for i in split["train"]:
            for j in split["test"]:
                assert not (starts[i] <= ends[j] and ends[i] >= starts[j])
        assert set(split["train"]).isdisjoint(split["embargoed"])
    split = plan["splits"][1]  # groups 0 and 2; first envelopes end at 7 and 19.
    assert split["embargoed"] == [8, 20]
    assert 6 in split["purged"] and 7 in split["purged"]
    assert 18 in split["purged"] and 19 in split["purged"]


def test_cpcv_insufficient_after_purge_is_not_usable_and_series_events_work():
    starts = pd.Series(pd.date_range("2020-01-01", periods=24, tz="UTC"))
    good = cpcv_splits(starts, starts, n_groups=4, embargo_fraction=0)
    assert good["status"] == "ok"
    plan = cpcv_splits(starts, starts + pd.Timedelta(days=100), n_groups=4)
    assert plan["status"] == "insufficient"
    assert plan["empty_train_splits"]
    assert assemble_cpcv_paths(plan, {})["status"] == "insufficient"


def test_cpcv_path_assembly_uses_each_split_prediction_on_the_exact_event_axis():
    plan = event_plan()
    predictions = {row["split_id"]: pd.Series([row["split_id"] + i / 100 for i in row["test"]],
                                               index=row["test"]) for row in plan["splits"]}
    result = assemble_cpcv_paths(plan, predictions)
    assert result["status"] == "ok"
    assert result["value_kind"] == "event_predictions_not_strategy_returns"
    assert result["path_count"] == 3
    for path in result["paths"]:
        assert len(path["observations"]) == 24
        for row in path["observations"]:
            assert row["prediction"] == row["split_id"] + row["position"] / 100
    missing = dict(predictions)
    missing[0] = predictions[0].iloc[1:]
    assert assemble_cpcv_paths(plan, missing)["status"] == "invalid"
    extra = dict(predictions)
    extra[0] = pd.concat([predictions[0], pd.Series([1.0], index=[100])])
    assert assemble_cpcv_paths(plan, extra)["status"] == "invalid"
    json.dumps(result, allow_nan=False)


def test_reality_check_hand_calculated_constant_advantage_and_no_edge():
    benchmark = np.tile([.01, -.01], 20)
    frame = panel(np.column_stack([benchmark + .02, benchmark - .01]), ["better", "worse"])
    actual_benchmark = pd.Series(benchmark, index=frame.index)
    result = white_reality_check(frame, actual_benchmark, family_complete=True, iterations=199)
    assert result["observed_statistic"] == pytest.approx(math.sqrt(40) * .02)
    assert result["p_value"] == 1 / 200
    assert result["best_candidate"] == "better"
    assert result["admission_eligible"] is False
    same = frame.copy()
    same["better"] = benchmark
    same["worse"] = benchmark
    none = white_reality_check(same, actual_benchmark, family_complete=True, iterations=199)
    assert none["observed_statistic"] == 0
    assert none["p_value"] == 1
    json.dumps(result, allow_nan=False)


def test_reality_check_duplicates_keep_joint_dependence_and_seed_is_reproducible():
    values = np.random.default_rng(50).normal(0, .01, 100)
    one = panel(values[:, None], ["A"])
    duplicate = one.assign(B=one["A"])
    first = white_reality_check(one, zero_benchmark(one), iterations=199, seed=53)
    second = white_reality_check(duplicate, zero_benchmark(duplicate), iterations=199, seed=53)
    assert first["bootstrap_max_quantiles"] == pytest.approx(second["bootstrap_max_quantiles"])
    assert first["diagnostic_p_value"] == second["diagnostic_p_value"]
    assert second == white_reality_check(duplicate, zero_benchmark(duplicate), iterations=199, seed=53)
    assert second["status"] == "diagnostic"
    assert second["p_value"] is None


def test_block_resampling_preserves_autocorrelation_and_changes_null_uncertainty():
    indices = block_bootstrap_indices(20, iterations=3, block_length=4, seed=42, method="circular")
    for draw in indices:
        for start in range(0, 20, 4):
            assert np.array_equal(draw[start:start + 4], (draw[start] + np.arange(4)) % 20)
    stationary = block_bootstrap_indices(100, iterations=100, block_length=10, seed=9)
    continuity = (stationary[:, 1:] == (stationary[:, :-1] + 1) % 100).mean()
    assert .87 < continuity < .93
    # A persistent sign process has much greater mean uncertainty under blocks.
    values = np.repeat([-1 / 128, 1 / 128] * 5, 40)
    frame = panel(values[:, None])
    iid = white_reality_check(frame, zero_benchmark(frame), iterations=999, block_length=1, seed=9)
    dependent = white_reality_check(frame, zero_benchmark(frame), iterations=999, block_length=10, seed=9)
    assert dependent["bootstrap_max_quantiles"]["0.95"] > 1.5 * iid["bootstrap_max_quantiles"]["0.95"]
    assert iid["diagnostic_p_value"] == dependent["diagnostic_p_value"] == 1


def test_reality_check_rejects_mismatched_benchmark_and_insufficient_nominal_blocks():
    frame = panel([[.01, -.01], [-.01, .01]] * 20)
    assert white_reality_check(frame, zero_benchmark(frame).iloc[1:])["status"] == "invalid"
    assert white_reality_check(frame, zero_benchmark(frame), block_length=30)["status"] == "insufficient"


def test_dsr_matches_hand_calculated_moments_and_period_unit_formula():
    frame = panel(np.tile([[.03, .01], [-.01, -.01], [.02, .01], [0, -.01]], (10, 1)), ["A", "B"])
    result = deflated_sharpe_evidence(frame, "A", historical_trials_complete=True,
                                      registered_before_results=True, declared_total_trials=2)
    chosen = frame["A"].to_numpy()
    sharpe = chosen.mean() / chosen.std(ddof=1)
    centered = chosen - chosen.mean()
    skew = (centered ** 3).mean() / chosen.var() ** 1.5
    kurtosis = (centered ** 4).mean() / chosen.var() ** 2
    sharpes = frame.mean().to_numpy() / frame.std(ddof=1).to_numpy()
    dispersion = sharpes.std(ddof=1)
    gamma = .5772156649015329
    expected = dispersion * ((1 - gamma) * NormalDist().inv_cdf(.5)
                             + gamma * NormalDist().inv_cdf(1 - 1 / (2 * math.e)))
    variance = 1 - skew * sharpe + (kurtosis - 1) * sharpe ** 2 / 4
    probability = NormalDist().cdf((sharpe - expected) * math.sqrt(39) / math.sqrt(variance))
    assert result["status"] == "ok"
    assert result["skewness"] == pytest.approx(skew)
    assert result["kurtosis"] == pytest.approx(kurtosis)
    assert result["expected_max_period_sharpe"] == pytest.approx(expected)
    assert result["probability"] == pytest.approx(probability)
    assert result["observed_annualized_sharpe"] == pytest.approx(sharpe * math.sqrt(365))
    assert result["admission_eligible"] is False
    alternate_display = deflated_sharpe_evidence(frame, "A", historical_trials_complete=True,
        registered_before_results=True, declared_total_trials=2, periods_per_year=252)
    assert alternate_display["probability"] == result["probability"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("metadata", [{}, {"historical_trials_complete": True},
    {"historical_trials_complete": True, "registered_before_results": True, "declared_total_trials": 99}])
def test_dsr_incomplete_search_never_claims_certified_probability(metadata):
    frame = panel(np.random.default_rng(1).normal(.001, .01, (100, 3)))
    result = deflated_sharpe_evidence(frame, "candidate_0", **metadata)
    assert result["status"] == "diagnostic"
    assert result["probability"] is None
    assert result["diagnostic_probability"] is not None
    assert result["search_accounting_complete"] is False


def test_dsr_duplicate_trials_reduce_only_estimated_count_and_autocorrelation_is_sensitivity():
    values = np.repeat([-.01, .015] * 10, 10)
    frame = panel(np.column_stack([values, values, values]), ["A", "B", "C"])
    result = deflated_sharpe_evidence(frame, "A")
    assert result["observed_trial_count"] == 3
    assert result["effective_trial_count_estimate"] == pytest.approx(1)
    assert result["temporal_effective_observations_estimate"] < result["observations"] / 2
    assert result["sensitivity"]["raw_trial_count_iid"]["trials"] == 3
    assert "heuristic" in result["effective_trial_count_method"]
    # With identical trial Sharpes the measured dispersion is zero, transparently.
    assert result["trial_sharpe_sample_std"] == pytest.approx(0, abs=1e-15)
    assert result["sensitivity"]["raw_trial_count_estimated_time_ess"]["probability"] < result["diagnostic_probability"]


def test_dsr_zero_variance_and_too_few_rows_are_explicitly_insufficient():
    assert deflated_sharpe_evidence(panel([[0, 1], [0, -1]] * 20), "candidate_0")["status"] == "insufficient"
    assert deflated_sharpe_evidence(panel([[1, -1], [-1, 1]]), "candidate_0")["status"] == "insufficient"
