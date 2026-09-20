"""Completed engines alone do not establish complete research delivery."""
import json

from scripts.publish_strategy_review import required_deliverables


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_required_reports_keep_delivery_incomplete(tmp_path):
    result = required_deliverables(tmp_path, {"arms": [{"arm": "baseline"}],
                                             "rolling_windows": [{"name": "w0"}]})
    assert result["complete"] is False
    assert not any(result["checks"].values())


def test_documented_gaps_and_pending_future_do_not_require_fabricated_evidence(tmp_path):
    protocol = {"arms": [{"arm": "baseline"}], "rolling_windows": [{"name": "w0"}]}
    folder = tmp_path / "matrix_results/runs/baseline__w0"
    _save(folder / "review_diagnostics.json", {"status": "complete"})
    for filename in ("review_state_transitions.csv", "review_setup_timing.csv", "review_exit_quality.csv",
                     "review_reentry_costs.csv", "review_score_buckets.csv"):
        (folder / filename).write_text("column\n", encoding="utf-8")
    _save(tmp_path / "meta_review/stratified_coverage/summary.json", {"status": "incomplete_evidence"})
    (tmp_path / "meta_review/stratified_coverage/coverage.csv").write_text("column\n", encoding="utf-8")
    _save(tmp_path / "family_assessment.json", {"pipeline_status": "complete", "families": [{}] * 5})
    _save(tmp_path / "public_data_archive_audit/manifest.json", {
        "status": "complete_audit", "attempted_archives": 936, "registered_archives": 936,
        "valid_closed_bar_coverage_complete": False})
    _save(tmp_path / "prospective_protocol.json", {
        "status": "pending_unseen_evidence", "admission": "paused_revalidation", "data_access_log": [],
        **dict.fromkeys(("code_hash", "config_hash", "protocol_hash", "test_start", "test_end_exclusive", "mature_after"), "frozen")})
    assert required_deliverables(tmp_path, protocol)["complete"] is True
    (folder / "review_exit_quality.csv").unlink()
    assert required_deliverables(tmp_path, protocol)["complete"] is False
