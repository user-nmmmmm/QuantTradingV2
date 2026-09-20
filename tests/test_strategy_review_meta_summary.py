"""Stratified support and paired MSE must preserve their actual denominators."""
import csv
import json

import pytest

from scripts.summarize_strategy_review_meta import (
    membership_status, summarize_meta, weighted_paired_mse, write_summary,
)


def _csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value) if isinstance(value, (dict, list)) else value
                             for key, value in row.items()})


def _book(**extra):
    return {"strategy": "TrendBreakout", "direction": "long", "horizon_bars": 5,
            "fold_id": "fold_1", **extra}


def _diagnostic(status, count, paired, mse):
    return _book(predicted_status=status, prediction_count=count, matured_count=count,
                 eligible_count=count, forecast_count=count if status != "abstain" else 0,
                 paired_forecast_count=paired, conditional_mse_bps2=mse,
                 baseline_mse_bps2=mse * 2 if mse is not None else None,
                 censored_count=0, unresolved_count=0, non_executable_count=0, invalid_net_label_count=0)


def test_status_strata_use_paired_denominators_and_support_is_not_summed(tmp_path):
    predictions = [_book(candidate_id=f"c{i}", status="allow" if i < 10 else "veto",
                         effective_samples=i + 1, effective_blocks=(i + 1) / 2)
                   for i in range(13)]
    predictions += [_book(candidate_id=f"missing{i}", status="abstain", reason="insufficient_blocks")
                    for i in range(2)]
    _csv(tmp_path / "ev_predictions.csv", predictions)
    _csv(tmp_path / "ev_diagnostics.csv", [
        _diagnostic("allow", 10, 1, 4), _diagnostic("veto", 3, 3, 16),
        _diagnostic("abstain", 2, 0, None),
    ])
    rows, issues = summarize_meta(tmp_path)
    assert issues == [] and len(rows) == 1
    row = rows[0]
    assert row["prediction_count"] == 15 and row["forecast_count"] == 13
    assert row["paired_forecast_count"] == 4 and row["weighted_conditional_mse_bps2"] == 13
    assert row["weighted_baseline_mse_bps2"] == 26
    assert row["effective_samples_known_count"] == 13
    assert row["effective_samples_min"] == 1 and row["effective_samples_median"] == 7
    assert row["effective_samples_max"] == 13
    assert "effective_samples_sum" not in row
    assert row["abstention_reasons"] == {"insufficient_blocks": 2}


def test_error_unknown_when_pairs_missing_or_positive_subgroup_error_absent():
    assert weighted_paired_mse([], "mse")["paired_count"] is None
    zero = weighted_paired_mse([{"paired_forecast_count": 0, "mse": None}], "mse")
    assert zero["value"] is None and zero["status"] == "no_paired_forecasts"
    incomplete = weighted_paired_mse([
        {"paired_forecast_count": 1, "mse": 4}, {"paired_forecast_count": 3, "mse": None},
    ], "mse")
    assert incomplete["value"] is None
    assert incomplete["paired_count"] == 4 and incomplete["known_pair_count"] == 1


@pytest.mark.parametrize("states,status", [
    ({"unknown": 1}, "explicit_unknown"), (None, "missing_or_invalid_membership"),
    ({"state_0": 0.2, "state_1": 0.2}, "invalid_probability_mass"),
    ({"state_0": -1, "state_1": 2}, "invalid_probability"),
    ({"state_0": 0.3, "state_1": 0.7}, "known"),
])
def test_unknown_state_is_not_claimed_available(states, status):
    assert membership_status(states) == status


def test_p2_models_memberships_and_attribution_remain_separate_per_book(tmp_path):
    known = {axis: {"state_0": 0.4, "state_1": 0.6} for axis in ("trend", "efficiency", "volatility")}
    unknown = {**known, "volatility": {"unknown": 1}}
    rows = [
        _book(candidate_id="c1", status="allow", memberships=known, regime_model_id="model-a"),
        _book(candidate_id="c2", status="abstain", memberships=unknown, regime_model_id="orphan-model"),
        _book(candidate_id="c3", status="abstain", reason="training_window", memberships={}),
        _book(candidate_id="initial", fold_id=None, status="abstain", reason="training_window"),
    ]
    _csv(tmp_path / "p2_predictions.csv", rows)
    model = {"model_id": "model-a", "fold_id": "fold_1", "book": _book(), "axes": {
        "trend": {"status": "available"}, "efficiency": {"status": "available"},
        "volatility": {"status": "unavailable", "reason": "insufficient_feature_samples"},
    }}
    (tmp_path / "p2_regime_models.json").write_text(json.dumps({"models": [model]}), encoding="utf-8")
    _csv(tmp_path / "p2_attribution.csv", [_book(status="uniform_fallback", reason="insufficient_samples",
                                                sample_count=3, effective_blocks=2)])
    summary, _ = summarize_meta(tmp_path)
    row = next(row for row in summary if row["fold_id"] == "fold_1")
    assert row["regime_model_count"] == 1 and row["regime_all_axes_available_model_count"] == 0
    assert row["regime_all_axes_known_prediction_count"] == 1
    assert row["regime_missing_model_id_prediction_count"] == 1
    assert row["regime_unlinked_model_id_prediction_count"] == 1
    assert row["regime_axis_membership_counts"]["volatility"] == {
        "known": 1, "explicit_unknown": 1, "missing_or_invalid_membership": 1,
    }
    assert row["attribution_uniform_fallback_count"] == 1
    assert row["attribution_uniform_fallback_reasons"] == {"insufficient_samples": 1}
    assert row["attribution_sample_count_median"] == 3
    assert row["matured_count"] is None and row["weighted_conditional_mse_bps2"] is None
    initial = next(row for row in summary if row["fold_id"] is None)
    assert initial["prediction_count"] == 1 and initial["regime_model_count"] == 0


def test_duplicate_status_diagnostics_do_not_double_count_errors(tmp_path):
    _csv(tmp_path / "ev_predictions.csv", [_book(status="allow")])
    diagnostic = _diagnostic("allow", 1, 1, 4)
    _csv(tmp_path / "ev_diagnostics.csv", [diagnostic, diagnostic])
    rows, issues = summarize_meta(tmp_path)
    assert issues[0]["reason"] == "duplicate_diagnostic_status_strata"
    assert rows[0]["weighted_conditional_mse_bps2"] is None
    assert rows[0]["matured_count"] is None


def test_missing_prediction_source_is_unknown_and_no_source_artifacts_changed(tmp_path):
    _csv(tmp_path / "ev_diagnostics.csv", [_diagnostic("allow", 1, 1, 0)])
    source = (tmp_path / "ev_diagnostics.csv").read_bytes()
    summary = write_summary(tmp_path)
    assert summary["status"] == "incomplete_evidence"
    assert "ev_predictions.csv" in summary["missing_sources"]
    assert (tmp_path / "ev_diagnostics.csv").read_bytes() == source
    rows, _ = summarize_meta(tmp_path)
    assert rows[0]["prediction_count"] is None and rows[0]["effective_samples_known_count"] is None
    assert rows[0]["weighted_conditional_mse_bps2"] is None
    assert (tmp_path / "stratified_coverage/README.md").exists()
