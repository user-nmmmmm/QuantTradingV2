"""P1 report arithmetic and honest treatment of missing research evidence."""
from copy import deepcopy
import csv
import json

import pytest

from backtest.reporting.signal_meta_layer import (
    signal_meta_diagnostics, signal_meta_layer_digest, write_signal_meta_layer_report,
)


def payload():
    return {"schema": "signal_ev/v1", "status": "complete", "policy": {},
        "model_version": "fixed-v1", "context_definition": {}, "input_identity": {},
        "folds": [], "predictions": [], "evaluations": [], "cell_snapshots": [],
        "ingestion_audit": {}, "validation": {"causal": True}, "errors": []}


def add(source, cid, realized=None, *, estimate=None, baseline=0.0, status="allow",
        label="matured", flags=None, **overrides):
    prediction = {"candidate_id": cid, "fold_id": "f1", "strategy": "Test", "direction": "long",
        "horizon_bars": 5, "estimate_bps": estimate, "status": status,
        "would_allow": {"allow": True, "veto": False, "abstain": None}[status],
        "prior": {"mean_bps": baseline}, **overrides}
    source["predictions"].append(prediction)
    source["evaluations"].append({**prediction, "label_status": label,
        "realized_net_bps": realized, "execution_flags": flags or [], "baseline_ev_bps": baseline})


def test_empty_export_has_headers_and_unknown_evidence(tmp_path):
    summary = write_signal_meta_layer_report(payload(), tmp_path)
    assert summary["status"] == "complete"
    assert summary["evidence"] == "insufficient"
    assert summary["status_counts"] == {"abstain": 0, "allow": 0, "veto": 0}
    assert len(summary["artifacts"]) == 7
    for name in summary["artifacts"]:
        assert (tmp_path/name).exists()
        if name.endswith(".csv"):
            with (tmp_path/name).open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                assert reader.fieldnames
                assert list(reader) == []
    assert "不构造零收益" in (tmp_path/"ev_report.md").read_text(encoding="utf-8")
    saved = json.loads((tmp_path/"ev_summary.json").read_text(encoding="utf-8"))
    assert saved == summary


def test_censored_non_executable_and_nonfinite_labels_are_not_zero():
    source = payload()
    add(source, "ok", 10.0, estimate=8.0)
    add(source, "tail", None, estimate=100, label="censored_end_of_data")
    add(source, "flagged", -999, estimate=-200, flags=["participation_limit"])
    add(source, "nan", float("nan"), estimate=0)
    add(source, "unresolved", None, label="missing_evaluation")
    row, = signal_meta_diagnostics(source)
    assert row["prediction_count"] == 5
    assert row["matured_count"] == 3
    assert row["eligible_count"] == 1
    assert row["censored_count"] == row["non_executable_count"] == 1
    assert row["invalid_net_label_count"] == row["unresolved_count"] == 1
    assert row["mean_realized_net_bps"] == 10
    assert row["mean_forecast_bps"] == 8
    assert row["conditional_mse_bps2"] == 4
    assert row["baseline_mse_bps2"] == 100


def test_abstentions_and_vetoes_remain_separate(tmp_path):
    source = payload()
    add(source, "cold", 20, status="abstain")
    add(source, "bad", -10, estimate=-8, status="veto")
    rows = {row["predicted_status"]: row for row in signal_meta_diagnostics(source)}
    assert set(rows) == {"abstain", "veto"}
    assert rows["abstain"]["mean_realized_net_bps"] == 20
    assert rows["abstain"]["mean_forecast_bps"] is None
    assert rows["abstain"]["conditional_mse_bps2"] is None
    summary = write_signal_meta_layer_report(source, tmp_path)
    assert summary["status_counts"] == {"abstain": 1, "allow": 0, "veto": 1}
    assert summary["evidence"] == "diagnostic_only"


def test_mean_mse_and_rank_correlation_use_paired_finite_rows():
    source = payload()
    add(source, "a", 2, estimate=1, baseline=0)
    add(source, "b", 4, estimate=3, baseline=2)
    add(source, "c", 8, estimate=5, baseline=4)
    add(source, "d", 10, estimate=9, baseline=None)
    add(source, "e", 100, estimate=None, baseline=0)
    row, = signal_meta_diagnostics(source)
    assert row["eligible_count"] == 5
    assert row["forecast_count"] == 4
    assert row["paired_forecast_count"] == 3
    assert row["mean_realized_net_bps"] == pytest.approx(124/5)
    assert row["mean_realized_scored_bps"] == 6
    assert row["mean_forecast_bps"] == 4.5
    assert row["conditional_mse_bps2"] == pytest.approx(11/3)
    assert row["baseline_mse_bps2"] == 8
    assert row["conditional_rank_correlation"] == pytest.approx(1)
    assert row["baseline_rank_correlation"] == pytest.approx(1)


@pytest.mark.parametrize("values", [[1], [1, 2], [2, 2, 2]])
def test_rank_correlation_unknown_when_insufficient_or_constant(values):
    source = payload()
    for index, estimate in enumerate(values):
        add(source, str(index), index, estimate=estimate)
    row, = signal_meta_diagnostics(source)
    assert row["conditional_rank_correlation"] is None
    assert row["baseline_rank_correlation"] is None


def test_average_tied_ranks():
    source = payload()
    for index, (estimate, realized) in enumerate([(1, 1), (1, 2), (3, 3)]):
        add(source, str(index), realized, estimate=estimate)
    row, = signal_meta_diagnostics(source)
    assert row["conditional_rank_correlation"] == pytest.approx(0.8660254037844387)


def test_missing_and_duplicate_evaluations_stay_visible():
    source = payload()
    add(source, "missing", 5, estimate=5)
    add(source, "duplicate", 5, estimate=5)
    source["evaluations"] = [source["evaluations"][1]]*2
    row, = signal_meta_diagnostics(source)
    assert row["prediction_count"] == row["unresolved_count"] == 2
    assert row["eligible_count"] == 0
    assert row["mean_realized_net_bps"] is None
    assert row["label_statuses"] == {"duplicate_evaluation": 1, "missing_evaluation": 1}


def test_frozen_prediction_values_win_over_label_side_values():
    source = payload()
    add(source, "frozen", 10, estimate=2, baseline=3)
    source["evaluations"][0].update(estimate_bps=10, baseline_ev_bps=10, status="veto")
    row, = signal_meta_diagnostics(source)
    assert row["predicted_status"] == "allow"
    assert row["conditional_mse_bps2"] == 64
    assert row["baseline_mse_bps2"] == 49


def test_digest_deterministic_and_exports_do_not_mutate_input(tmp_path):
    source = payload()
    add(source, "one", 10, estimate=4)
    before = deepcopy(source)
    reordered = dict(reversed(list(source.items())))
    assert signal_meta_layer_digest(source) == signal_meta_layer_digest(reordered)
    summary = write_signal_meta_layer_report(source, tmp_path)
    assert source == before
    assert summary["research_payload_sha256"] == signal_meta_layer_digest(source)
    source["predictions"][0]["estimate_bps"] = 5
    assert signal_meta_layer_digest(source) != summary["research_payload_sha256"]


def test_validation_and_engineering_status_not_profit(tmp_path):
    source = payload()
    add(source, "loss", -50, estimate=-10, status="veto")
    source["validation"]["causal"] = False
    source["status"] = "incomplete"
    source["errors"] = [{"reason": "invalid_input"}]
    summary = write_signal_meta_layer_report(source, tmp_path)
    assert summary["status"] == "incomplete"
    assert summary["evidence"] == "diagnostic_only"
    assert summary["failed_validation"] == ["causal"]
    text = (tmp_path/"ev_report.md").read_text(encoding="utf-8")
    assert "不表示盈利" in text
    assert "数据／验证异常" in text
