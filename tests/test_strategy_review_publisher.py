"""Report status and comparison semantics must not manufacture completed evidence."""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from scripts.publish_strategy_review import publish, test_evidence as engineering_test_evidence
from scripts.strategy_review_report_helpers import (
    combined_status, cross_evidence, distribution, economic_comparison,
    meta_evidence, public_evidence, verified_runs,
)


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("values,expected", [
    ([], "pending"), (["pass", None], "pending"), (["fail", "pending"], "pending"),
    (["pass", "insufficient"], "insufficient"), (["fail", "insufficient"], "insufficient"),
    (["pass", "fail"], "fail"), (["pass", "pass"], "pass"),
])
def test_statuses_preserve_missing_support_without_vacuous_success(values, expected):
    assert combined_status(values) == expected


def _protocol():
    return {"arms": [{"arm": "baseline", "families": ["baseline"]}],
        "rolling_windows": [{"name": "rolling_00"}], "baseline_specs": [{"name": "main"}],
        "validation_specs": [{"name": name, "end": "2024-01-01"} for name in
            ("main_1", "final20", "reversed_symbols", "prefix_2023", "prefix_2024")],
        "missing_bar_seeds": list(range(42, 62)),
        "gates": {"positive_windows_at_least": 1, "cost_rerun_multipliers_required": [1.5, 2],
                  "max_drawdown_pct": 20, "bootstrap": {"block_groups": 5, "seed": 42, "iterations": 2000}}}


def test_empty_registered_batch_publishes_pending_costs_and_only_allowed_issue_labels(tmp_path):
    _save(tmp_path / "review_protocol.json", _protocol())
    _save(tmp_path / "reference_protocol.json", {"segments": {"final20": ["2024-01-01", "2024-06-30"]}})
    result = publish(tmp_path)
    assert result["evidence_pipeline_complete"] is False
    assert result["research"]["cost_reruns"] == {"1.5": "pending", "2": "pending"}
    assert result["research"]["retrospective_assessment"] == "pending"
    assert result["engineering_status"] == "pending"
    issues = json.loads((tmp_path / "issue_closure.json").read_text(encoding="utf-8"))
    assert {row["status"] for row in issues} == {"证据不足"}
    assert (tmp_path / "legacy_criteria.json").exists()


def test_all_skipped_tests_are_not_engineering_pass(tmp_path):
    (tmp_path / "engineering_tests.xml").write_text('<testsuites><testsuite tests="2" skipped="2" errors="0" failures="0"/></testsuites>')
    assert engineering_test_evidence(tmp_path)["status"] == "insufficient"


def test_twenty_seed_distribution_does_not_report_only_best_sample():
    result = distribution(range(-10, 10))
    assert result["count"] == 20
    assert result["minimum"] == -10 and result["median"] == -.5 and result["maximum"] == 9
    assert distribution([None, float("nan")])["count"] == 0


def _economic_files(folder, prices):
    folder.mkdir()
    dates = pd.date_range("2024-01-01", periods=len(prices), tz="UTC")
    pd.DataFrame({"timestamp": dates, "equity": prices}).to_csv(folder / "equity_requested_period.csv", index=False)
    pd.DataFrame({"fill_time": [dates[0]], "symbol": ["BTC/USDT"], "side": ["buy"],
        "qty": [1], "fill_price": [100], "commission": [.1], "strategy_id": ["TrendBreakout"],
        "exit_reason": ["signal"]}).to_csv(folder / "trades.csv", index=False)


def test_prefix_comparison_uses_only_prefix_and_detects_changed_economic_value(tmp_path):
    main, prefix = tmp_path / "main", tmp_path / "prefix"
    _economic_files(main, [100, 101, 999])
    _economic_files(prefix, [100, 101])
    assert economic_comparison(main, prefix, pd.Timestamp("2024-01-02", tz="UTC"))["status"] == "pass"
    frame = pd.read_csv(prefix / "equity_requested_period.csv")
    frame.loc[1, "equity"] = 102
    frame.to_csv(prefix / "equity_requested_period.csv", index=False)
    assert economic_comparison(main, prefix, pd.Timestamp("2024-01-02", tz="UTC"))["status"] == "fail"


def test_public_report_reads_validated_manifest_and_preserves_gap_scope(tmp_path):
    root = tmp_path / "public_data_validated"
    root.mkdir()
    data = b"timestamp,open,high,low,close,volume\n"
    (root / "x.csv").write_bytes(data)
    stream = {"csv_path": "x.csv", "csv_sha256": hashlib.sha256(data).hexdigest(),
        "venue": "binance", "symbol": "BTC/USDT", "timeframe": "4h", "status": "evidence_incomplete",
        "rows": 0, "missing_bars": 1, "gaps": [{"start": "2021-09-29T04:00:00Z"}], "failures": [],
        "segments": {"historical": {"expected": 1, "observed": 1}, "recent_drift": {"expected": 1, "observed": 1}}}
    _save(root / "manifest.json", {"matrix_streams": 1, "complete_streams": 0, "rows": 0, "missing_bars": 1, "streams": [stream]})
    _save(tmp_path / "public_data_zzz/manifest.json", {"matrix_streams": 100, "complete_streams": 100})
    result = public_evidence(tmp_path)
    assert result["matrix_streams"] == 1 and result["missing_bars"] == 1
    assert result["historical_complete_streams"] == result["recent_complete_streams"] == 1
    assert result["pipeline_status"] == "pending"


def _cross_payload(status="insufficient"):
    pairs = [{"period": period, "timeframe": timeframe, "status": status}
             for period in ("historical", "recent_drift") for timeframe in ("1d", "4h")]
    return {"runs": {f"{row['period']}_{venue}_{row['timeframe']}":
                     {"status": status, "accounting_ok": True}
                     for row in pairs for venue in ("binance", "okx")},
            "paired_comparisons": pairs}


def test_cross_report_reads_runner_comparison_and_keeps_insufficient_distinct(tmp_path):
    _save(tmp_path / "cross_market/comparison.json", _cross_payload())
    result = cross_evidence(tmp_path)
    assert result["pipeline_status"] == "complete" and result["research_status"] == "insufficient"
    assert result["retrospective_status"] == "insufficient"


def test_cross_retrospective_pass_does_not_satisfy_missing_admission_evidence(tmp_path):
    _save(tmp_path / "cross_market/comparison.json", _cross_payload("pass"))
    result = cross_evidence(tmp_path)
    assert result["pipeline_status"] == "complete"
    assert result["retrospective_status"] == "pass"
    assert result["research_status"] == "insufficient"
    assert result["not_admission_test"] is True
    assert result["prospective_status"] == "pending_unseen_evidence"
    assert len(result["missing_required_research_evidence"]) == 3


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "empty", "wrong_run_identity"])
def test_cross_requires_every_fixed_pair_and_run_without_vacuous_success(tmp_path, mutation):
    payload = _cross_payload("pass")
    pairs = payload["paired_comparisons"]
    if mutation == "missing":
        pairs.pop()
    elif mutation == "duplicate":
        pairs[-1] = dict(pairs[0])
    elif mutation == "empty":
        pairs.clear()
    else:
        payload["runs"]["unregistered"] = payload["runs"].pop("historical_binance_1d")
    _save(tmp_path / "cross_market/comparison.json", payload)
    result = cross_evidence(tmp_path)
    assert result["pipeline_status"] == result["retrospective_status"] == result["research_status"] == "pending"


@pytest.mark.parametrize("other_status", ["insufficient", "pass"])
def test_cross_known_failure_remains_visible_without_unseen_evidence(tmp_path, other_status):
    payload = _cross_payload(other_status)
    payload["paired_comparisons"][0]["status"] = "fail"
    _save(tmp_path / "cross_market/comparison.json", payload)
    result = cross_evidence(tmp_path)
    assert result["research_status"] == "fail"
    assert result["observed_failed_pairs"] == [{"period": "historical", "timeframe": "1d"}]


def test_meta_report_uses_actual_incomplete_primary_and_equal_quarter_facts(tmp_path):
    accounts = [{"arm": arm, "metrics_valid": True, "comparison_eligible": arm != "gate",
        "return_fraction": .01, "max_drawdown_fraction": .02, "finite_closed_candidate_count": 1}
        for arm in ("baseline", "gate", "sizing", "fixed_quarter")]
    _save(tmp_path / "meta_review/acceptance.json", {"status": "incomplete", "primary_accounts": accounts,
        "components": {"p3_primary": {"status": "incomplete", "errors": ["missing"]}}})
    result = meta_evidence(tmp_path)
    assert result["pipeline_status"] == "incomplete"
    assert "p3_primary_component_missing_or_incomplete" in result["observed_limitations"]
    assert "sizing_economics_equal_fixed_quarter_control" in result["observed_limitations"]
    assert result["descriptive_comparisons"]["sizing"]["return_percentage_point_delta"] == 0


def test_new_run_receipt_cannot_omit_output_hashes(tmp_path):
    protocol = _protocol()
    _save(tmp_path / "review_protocol.json", protocol)
    folder = tmp_path / "matrix_results/runs/baseline__rolling_00"
    _save(folder / "summary.json", {"name": "baseline__rolling_00"})
    _save(folder / "review_identity.json", {"sha256": "example", "identity": {
        "protocol_sha256": hashlib.sha256((tmp_path / "review_protocol.json").read_bytes()).hexdigest()}, "artifacts": {}})
    with pytest.raises(ValueError, match="omits summary"):
        verified_runs(tmp_path, protocol)


@pytest.mark.parametrize("kind", ["uniform", "nonuniform", "invalid", "incomplete"])
def test_meta_actual_weights_are_checked_even_when_fitting_succeeds(tmp_path, kind):
    root = tmp_path / "meta_review"
    _save(root / "p2_summary.json", {"attribution_count": 3 if kind == "incomplete" else 2,
                                  "attribution_fallback_count": 1})
    weights = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
    if kind == "nonuniform":
        weights = {"a": .5, "b": .3, "c": .2}
    if kind == "invalid":
        weights = {"a": .3, "b": .3, "c": None}
    pd.DataFrame([{"status": "uniform_fallback", "weights": json.dumps({"a": 1/3, "b": 1/3, "c": 1/3})},
                  {"status": "fitted", "weights": json.dumps(weights)}]).to_csv(root / "p2_attribution.csv", index=False)
    result = meta_evidence(tmp_path)
    fact = result["attribution_weight_evidence"]
    assert fact["all_actual_weights_uniform"] is (kind == "uniform")
    assert ("all_actual_attribution_weights_uniform_including_fitted_books" in result["observed_limitations"]) is (kind == "uniform")
    assert fact["nonuniform_count"] == int(kind == "nonuniform")
    assert fact["invalid_count"] == int(kind == "invalid")
    assert fact["fitted_uniform_count"] == int(kind in {"uniform", "incomplete"})
