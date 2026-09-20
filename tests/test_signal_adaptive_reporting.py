"""Reports preserve paired denominators, unknown evidence and account semantics."""
from copy import deepcopy
import csv
import json
import math

import pytest

from backtest.reporting.signal_adaptive import (
    research_digest, write_signal_adaptive_report, write_signal_meta_replay_report,
)


def p2_payload():
    return {"schema": "signal_adaptive_ev/v1", "status": "complete", "policy": {"min_regime_samples": 40},
        "predictions": [], "evaluations": [], "folds": [], "cell_snapshots": [], "regime_models": [],
        "calibration_predictions": [], "attribution": [], "model_version": "p2-implementation",
        "input_identity": {"p0_snapshot_version": "snapshot-v1"}, "protocol": {},
        "validation": {"causal": True}, "errors": []}


def add(payload, cid, actual, *, prior=0., uniform=1., adaptive=2., status="allow",
        label="matured", flags=None, **extra):
    row = {"candidate_id": cid, "horizon_bars": 5, "fold_id": "f1", "available_at": "2020-01-01T00:00:00Z",
        "strategy": "Trend", "direction": "long", "symbol": "BTC/USDT", "status": status,
        "reason": "supported", "prior": {"mean_bps": prior}, "uniform_estimate_bps": uniform,
        "estimate_bps": adaptive, "regime_model_id": "m1", **extra}
    payload["predictions"].append(row)
    payload["evaluations"].append({**deepcopy(row), "label_status": label,
        "realized_net_bps": actual, "execution_flags": [] if flags is None else flags})
    return row


def comparisons(summary):
    return {row["comparison"]: row for row in summary["forecast_comparisons"]}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def p3_payload():
    return {"schema": "signal_meta_replay/v1", "status": "complete", "implementation_version": "p3-v1",
        "policy": {"horizon_bars": 5, "initial_capital": 10000., "reference_notional": 1000.,
                   "min_size_multiplier": .25, "max_size_multiplier": 1.},
        "rows": [], "accounts": [], "equity": [], "fills": [], "financing": [], "execution_audit": [],
        "input_identity": {}, "protocol": {}, "validation": {"causal": True}, "errors": []}


def account(arm, *, active=True, **extra):
    return {"arm": arm, "status": "completed", "activity": "active" if active else "inactive",
        "halt_time": None, "initial_capital": 10000., "ending_marked_equity": 10100. if active else 10000.,
        "net_pnl": 100. if active else 0., "return_fraction": .01 if active else 0.,
        "max_drawdown_fraction": .002 if active else 0., "fills": 3 if active else 0,
        "open_positions": 1 if active else 0, "commission": 3., "slippage_cost": 4.,
        "financing_cost": 2., **extra}


def test_p2_empty_report_writes_schema_and_unknown_not_zero_mse(tmp_path):
    summary = write_signal_adaptive_report(p2_payload(), tmp_path)
    assert summary["status"] == "complete"
    assert summary["evidence"] == "insufficient"
    assert summary["status_counts"] == {"abstain": 0, "allow": 0, "veto": 0}
    assert len(summary["artifacts"]) == 10
    for name in summary["artifacts"]:
        assert (tmp_path / name).is_file()
        if name.endswith(".csv"):
            assert read_csv(tmp_path / name)[0]
    for row in summary["forecast_comparisons"]:
        assert row["paired_count"] == 0
        assert all(value is None for value in row["mse_bps2"].values())
    assert json.loads((tmp_path / "p2_summary.json").read_text(encoding="utf-8")) == summary


def test_p2_comparisons_use_common_samples_and_source_predictions_not_label_side_edits(tmp_path):
    payload = p2_payload()
    add(payload, "a", 2., prior=0., uniform=1., adaptive=2.)
    add(payload, "b", 4., prior=0., uniform=2., adaptive=3.)
    add(payload, "c", 100., prior=None, uniform=None, adaptive=99.)
    payload["evaluations"][0].update(estimate_bps=9999, uniform_estimate_bps=9999,
                                      prior={"mean_bps": 9999}, status="veto")
    summary = write_signal_adaptive_report(payload, tmp_path)
    result = comparisons(summary)
    assert result["prior_vs_adaptive"]["paired_count"] == 2
    assert result["prior_vs_adaptive"]["mse_bps2"] == {"prior": 10., "adaptive": .5}
    assert result["uniform_vs_adaptive"]["mse_bps2"] == {"uniform": 2.5, "adaptive": .5}
    assert result["prior_vs_uniform_vs_adaptive"]["paired_count"] == 2
    assert summary["forecast_count"] == 3
    assert summary["status_counts"]["allow"] == 3


def test_p1_reference_comparison_is_joined_on_candidate_horizon_and_same_identity(tmp_path):
    payload = p2_payload()
    add(payload, "a", 2., prior=0., uniform=1., adaptive=2.)
    add(payload, "b", 4., prior=0., uniform=2., adaptive=3.)
    add(payload, "c", 100., prior=None, uniform=None, adaptive=99.)
    reference = {"status": "complete", "input_identity": {"p0_snapshot_version": "snapshot-v1"},
        "predictions": [{**row, "fold_id": "different-p1-fold", "estimate_bps": value}
                        for row, value in zip(payload["predictions"], [1., 5., 100.])]}
    summary = write_signal_adaptive_report(payload, tmp_path, p1_payload=reference)
    result = comparisons(summary)
    assert result["adaptive_vs_p1"]["paired_count"] == 3
    assert result["adaptive_vs_p1"]["mse_bps2"] == pytest.approx({"adaptive": 2/3, "p1": 2/3})
    assert result["prior_vs_uniform_vs_adaptive_vs_p1"]["paired_count"] == 2
    assert result["prior_vs_uniform_vs_adaptive_vs_p1"]["mse_bps2"]["p1"] == 1
    assert summary["p1_reference_sha256"] == research_digest(reference)
    reference["input_identity"]["p0_snapshot_version"] = "other"
    mismatch = write_signal_adaptive_report(payload, tmp_path, p1_payload=reference)
    assert mismatch["p1_reference_compatible"] is False
    assert comparisons(mismatch)["adaptive_vs_p1"]["paired_count"] == 0


def test_p2_censored_flagged_invalid_and_duplicate_labels_do_not_enter_mse(tmp_path):
    payload = p2_payload()
    add(payload, "ok", 10., adaptive=8.)
    add(payload, "tail", None, adaptive=100., label="censored_end_of_data")
    add(payload, "flagged", -1000., flags=["non_executable"])
    add(payload, "invalid", math.nan)
    add(payload, "duplicate", 50.)
    payload["evaluations"].append(deepcopy(payload["evaluations"][-1]))
    summary = write_signal_adaptive_report(payload, tmp_path)
    result = comparisons(summary)["uniform_vs_adaptive"]
    assert result["paired_count"] == 1
    assert result["adaptive_mse_bps2"] == 4
    assert summary["censored_count"] == summary["non_executable_count"] == summary["unresolved_count"] == 1
    assert summary["eligible_count"] == 1
    _, rows = read_csv(tmp_path / "p2_evaluations.csv")
    assert next(row for row in rows if row["candidate_id"] == "invalid")["realized_net_bps"] == ""


def test_duplicate_prediction_or_duplicate_reference_does_not_inflate_paired_count(tmp_path):
    payload = p2_payload()
    row = add(payload, "dup", 10.)
    payload["predictions"].append(deepcopy(row))
    summary = write_signal_adaptive_report(payload, tmp_path)
    assert comparisons(summary)["prior_vs_adaptive"]["paired_count"] == 0
    payload["predictions"].pop()
    reference = {"status": "complete", "input_identity": payload["input_identity"], "predictions": [row, row]}
    summary = write_signal_adaptive_report(payload, tmp_path, p1_payload=reference)
    assert comparisons(summary)["adaptive_vs_p1"]["paired_count"] == 0


def test_missing_snapshot_identity_does_not_certify_a_p1_comparison(tmp_path):
    payload = p2_payload()
    add(payload, "one", 10.)
    payload["input_identity"] = {}
    reference = {"status": "complete", "input_identity": {}, "predictions": payload["predictions"]}
    summary = write_signal_adaptive_report(payload, tmp_path, p1_payload=reference)
    assert summary["p1_reference_compatible"] is False
    assert comparisons(summary)["adaptive_vs_p1"]["paired_count"] == 0


def test_entropy_excludes_unknown_and_models_preserve_full_fit_evidence(tmp_path):
    payload = p2_payload()
    add(payload, "soft", 10., memberships={"trend": {"state_0": .5, "state_1": .5}},
        posterior_entropy={"trend": math.log(2)})
    add(payload, "unknown", 10., status="abstain", adaptive=None,
        memberships={"trend": {"unknown": 1.}}, posterior_entropy={"trend": 0.},
        regime_model_id=None, reason="unknown_context")
    manifest = {"model_id": "m1", "fit_spec": {"min_samples": 40, "neighbors": 20},
        "axes": {"trend": {"status": "available", "reason": "fitted", "centroids": [[1., 2.], [9., 10.]],
            "temperature_bps2": 5., "separation_bps2": 64., "inertia_bps2": 3.}}}
    payload["regime_models"] = [manifest]
    payload["attribution"] = [{"status": "uniform_fallback", "reason": "insufficient_samples"}]
    summary = write_signal_adaptive_report(payload, tmp_path)
    axis = summary["axis_diagnostics"]["trend"]
    assert axis["known_prediction_count"] == axis["unknown_prediction_count"] == 1
    assert axis["mean_entropy_nats"] == math.log(2)
    assert axis["mean_normalized_entropy"] == 1
    assert axis["mean_fit_separation_bps2"] == 64
    assert summary["attribution_fallback_count"] == 1
    artifact = json.loads((tmp_path / "p2_regime_models.json").read_text(encoding="utf-8"))
    assert artifact["models"] == [manifest]
    assert artifact["missing_model_predictions"][0]["model_reason"] == "no_fitted_model_for_book"
    assert "三轴独立" in (tmp_path / "p2_report.md").read_text(encoding="utf-8")


def test_all_unknown_regimes_can_be_engineering_complete_with_insufficient_evidence(tmp_path):
    payload = p2_payload()
    add(payload, "unknown", 10., status="abstain", adaptive=None, uniform=None,
        memberships={"trend": {"unknown": 1.}}, regime_model_id=None, reason="training_window", fold_id=None)
    summary = write_signal_adaptive_report(payload, tmp_path)
    assert summary["status"] == "complete"
    assert summary["evidence"] == "insufficient"
    assert summary["axis_diagnostics"]["trend"]["mean_entropy_nats"] is None
    assert summary["missing_model_prediction_count"] == 1
    text = (tmp_path / "p2_report.md").read_text(encoding="utf-8")
    assert "不是论文完整复现" in text
    assert "不是经校准" in text


def test_p2_digest_stable_inputs_not_mutated_and_failed_validation_visible(tmp_path):
    payload = p2_payload()
    add(payload, "one", 10.)
    payload["validation"]["causal"] = False
    before = deepcopy(payload)
    summary = write_signal_adaptive_report(payload, tmp_path)
    assert summary["status"] == "incomplete"
    assert summary["failed_validation"] == ["causal"]
    assert payload == before
    assert research_digest(dict(reversed(list(payload.items())))) == summary["research_payload_sha256"]


def test_p3_empty_exports_are_present_and_do_not_invent_account_results(tmp_path):
    summary = write_signal_meta_replay_report(p3_payload(), tmp_path)
    assert len(summary["artifacts"]) == 8
    assert summary["accounts"] == []
    assert summary["comparison_eligible_arms"] == []
    for name in summary["artifacts"]:
        assert (tmp_path / name).is_file()
        if name.endswith(".csv"):
            fields, rows = read_csv(tmp_path / name)
            assert fields and not rows


def test_p3_marked_equity_closed_pnl_and_open_tail_remain_distinct_without_reducing_costs_twice(tmp_path):
    payload = p3_payload()
    payload["accounts"] = [account("baseline"), account("gate", active=False), account("sizing")]
    payload["rows"] = [{"arm": "baseline", "candidate_id": "closed", "horizon_bars": 5,
        "status": "closed", "net_pnl": 80.}, {"arm": "baseline", "candidate_id": "tail", "horizon_bars": 5,
        "status": "censored_end_of_data", "net_pnl": None},
        {"arm": "gate", "candidate_id": "closed", "horizon_bars": 5, "status": "meta_blocked", "net_pnl": None}]
    before = deepcopy(payload)
    summary = write_signal_meta_replay_report(payload, tmp_path)
    accounts = {row["arm"]: row for row in summary["accounts"]}
    assert accounts["baseline"]["net_pnl"] == 100
    assert accounts["baseline"]["closed_candidate_net_pnl"] == 80
    assert accounts["baseline"]["censored_candidate_count"] == 1
    assert accounts["baseline"]["open_positions"] == 1
    assert accounts["gate"]["closed_candidate_net_pnl"] is None
    assert accounts["gate"]["return_fraction"] == 0
    assert accounts["gate"]["comparison_eligible"] is False
    assert accounts["gate"]["evidence"] == "inactive"
    assert summary["inactive_arms"] == ["gate"]
    assert summary["comparison_eligible_arms"] == ["baseline", "sizing"]
    assert summary["candidate_count"] == 2
    assert payload == before
    saved = json.loads((tmp_path / "p3_summary.json").read_text(encoding="utf-8"))
    assert saved == summary
    text = (tmp_path / "p3_report.md").read_text(encoding="utf-8")
    assert "不自动给账户排名" in text
    assert "收益率分母为初始资本" in text
    assert "最大回撤分母为此前标记权益峰值" in text


@pytest.mark.parametrize("problem", ["halt", "error", "incomplete", "validation", "missing_metric"])
def test_p3_unresolved_results_have_no_return_or_drawdown_comparison(problem, tmp_path):
    payload = p3_payload()
    payload["accounts"] = [account("baseline")]
    if problem == "halt":
        payload["accounts"][0].update(status="unresolved_execution_error", halt_time="2020-01-01")
    elif problem == "error":
        payload["errors"] = [{"arm": "baseline", "reason": "missing_carry"}]
    elif problem == "incomplete":
        payload["status"] = "incomplete"
    elif problem == "validation":
        payload["validation"]["causal"] = False
    else:
        payload["accounts"][0]["return_fraction"] = None
    summary = write_signal_meta_replay_report(payload, tmp_path)
    result = summary["accounts"][0]
    assert result["return_fraction"] is None
    assert result["net_pnl"] is None
    assert result["max_drawdown_fraction"] is None
    assert result["ending_marked_equity"] == 10100
    assert result["comparison_eligible"] is False
    assert summary["unresolved_arms"] == ["baseline"]
    assert summary["status"] == "incomplete"
