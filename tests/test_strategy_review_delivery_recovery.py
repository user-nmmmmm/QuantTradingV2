"""Append-only cache recovery must preserve facts and refuse invented values."""
import pickle
import json

import pytest

from scripts.recover_strategy_review_delivery import ProjectionError, project_primitive_pickle, sha256, verify_cache
from scripts.finalize_strategy_review_delivery import account_summary, combine_status
from scripts.replay_strategy_review_controls import decode_prediction


def cache(tmp_path, payload):
    path = tmp_path / "stage.pickle"
    path.write_bytes(pickle.dumps(payload, protocol=5))
    path.with_suffix(".sha256").write_text(sha256(path), encoding="ascii")
    return path


def test_projection_preserves_metadata_and_counts_large_tables(tmp_path):
    payload = {"digest": {"trades": "frozen"}, "p0": {"candidates": [{"id": str(i)} for i in range(2001)]},
        "p2": {"status": "complete", "policy": {"horizons": [1, 3, 20]},
               "predictions": [{"value": i} for i in range(2001)], "validation": {"causal": True}},
        "p3": {"rows": [{"value": i} for i in range(1001)],
               "accounts": [{"arm": "baseline", "return_fraction": -0.4}], "errors": []}}
    result = project_primitive_pickle(cache(tmp_path, payload), drop_roots={"p0"})
    assert result["payload"]["digest"] == payload["digest"]
    assert result["payload"]["p2"]["validation"] == {"causal": True}
    assert result["payload"]["p2"]["policy"] == payload["p2"]["policy"]
    assert result["payload"]["p2"]["predictions"]["count"] == 2001
    assert result["payload"]["p3"]["accounts"] == payload["p3"]["accounts"]
    assert result["omitted_tables"]["p3/rows"] == 1001
    assert result["retained_memo_slots"] < result["memo_slots"]


def test_projection_resolves_memoized_atoms_from_discarded_rows(tmp_path):
    atom = "a long shared immutable identity used by both layers"
    payload = {"p0": {"candidates": [{"id": atom}]}, "p3": {"input_identity": atom}}
    assert project_primitive_pickle(cache(tmp_path, payload), drop_roots={"p0"})["payload"]["p3"] == {"input_identity": atom}


def test_projection_refuses_missing_required_container_alias(tmp_path):
    rows = [{"value": 1}]
    path = cache(tmp_path, {"predictions": rows, "required_identity": rows})
    with pytest.raises(ProjectionError, match="omitted container"):
        project_primitive_pickle(path)


class InertGlobal:
    def __reduce__(self):
        return eval, ("1 / 0",)


def test_projection_never_executes_pickle_reducers(tmp_path):
    path = cache(tmp_path, {"p0": {"candidates": [InertGlobal()]}, "status": "complete"})
    assert project_primitive_pickle(path, drop_roots={"p0"})["payload"]["status"] == "complete"
    required = cache(tmp_path, {"required": InertGlobal()})
    with pytest.raises(ProjectionError, match="omitted container"):
        project_primitive_pickle(required)


def test_cache_hash_and_trailing_content_fail_closed(tmp_path):
    path = cache(tmp_path, {"status": "complete"})
    assert verify_cache(path)["sha256"] == sha256(path)
    path.write_bytes(path.read_bytes() + b"unexpected")
    with pytest.raises(ProjectionError, match="checksum"):
        verify_cache(path)
    with pytest.raises(ProjectionError, match="trailing"):
        project_primitive_pickle(path)


def test_known_failure_is_not_downgraded_to_insufficient():
    assert combine_status(["pass", "insufficient"]) == "insufficient"
    assert combine_status(["pass", "fail", "insufficient"]) == "fail"
    assert combine_status(["pass", "pass"]) == "pass"


def test_halted_or_nonfinite_account_never_gets_valid_zero_return():
    payload = {"status": "complete", "validation": {"independent_accounts": True}, "errors": [],
        "accounts": [{"arm": "baseline", "status": "unresolved_execution_error", "activity": "active",
                      "return_fraction": 0.0, "max_drawdown_fraction": 0.0, "net_pnl": 0.0},
                     {"arm": "gate", "status": "completed", "activity": "inactive",
                      "return_fraction": 0.0, "max_drawdown_fraction": 0.0, "net_pnl": 0.0},
                     {"arm": "sizing", "status": "completed", "activity": "active",
                      "return_fraction": float("nan"), "max_drawdown_fraction": 0.1, "net_pnl": 1.0}]}
    result = account_summary(payload)
    assert result["status"] == "incomplete"
    assert result["accounts"][0]["return_fraction"] is None
    assert result["accounts"][1]["metrics_valid"] is True
    assert result["accounts"][1]["comparison_eligible"] is False
    assert result["accounts"][2]["return_fraction"] is None


def test_control_projection_uses_frozen_predecision_fields_only():
    row = {"candidate_id": "c", "horizon_bars": "5", "estimate_bps": "0", "lower_bound_bps": "",
           "status": "abstain", "realized_net_bps": "10000", "final_rank": "1"}
    decoded = decode_prediction(row)
    assert decoded["horizon_bars"] == 5
    assert decoded["estimate_bps"] == 0.0
    assert decoded["lower_bound_bps"] is None
    assert "realized_net_bps" not in decoded and "final_rank" not in decoded


def test_stratification_overlay_records_new_summary_identity_without_writing_source(tmp_path, monkeypatch):
    from scripts import summarize_strategy_review_meta as report
    source, output = tmp_path / "sealed", tmp_path / "delivery"
    source.mkdir()
    recovered = tmp_path / "recovered_p2.json"
    recovered.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    monkeypatch.setattr(report, "summarize_meta", lambda path: ([], []))
    result = report.write_summary(source, output, p2_summary=recovered)
    assert result["source_statuses"]["P2"] == "complete"
    assert result["source_sha256"]["p2_summary.json"] == sha256(recovered)
    assert result["source_paths"]["p2_summary.json"] == str(recovered.resolve())
    assert "p2_summary.json" not in result["missing_sources"]
    assert result["status"] == "incomplete_evidence"
    assert not (source / "p2_summary.json").exists()
