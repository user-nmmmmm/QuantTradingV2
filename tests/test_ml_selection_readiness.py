"""Evidence audits use synthetic files and never train/load a model runtime."""
from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest
import yaml

from core.reproducibility import canonical_json, sha256_file, sha256_frame
from research.ml_selection.readiness import audit_readiness, describe_serving_drift


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path):
    frame = pd.DataFrame({"open": [1., 2.], "high": [2., 3.], "low": [1., 2.],
                          "close": [2., 3.], "volume": [100., 100.]},
                         index=pd.date_range("2024-01-01", periods=2, name="timestamp"))
    data_path = tmp_path / "input/engine/A_USDT.csv"
    data_path.parent.mkdir(parents=True)
    frame.to_csv(data_path)
    registration = {"symbols": ["A/USDT"], "input_files": {"input/engine/A_USDT.csv": sha256_file(data_path)},
                    "engine_frame_hashes": {"A/USDT": sha256_frame(frame)}}
    write_json(tmp_path / "data.json", registration)
    write_json(tmp_path / "baseline.json", {"arms": {"smart": {"parameters": {"account": {"mode": "spot"}}}}})
    settings = {"account_mode": "spot", "data_registration": "data.json", "baseline_registration": "baseline.json",
                "next_research": {"final_sample": {"opened": False, "start": "2026-10-06", "end": "2027-01-01"}}}
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(settings))
    model = {"artifact_version": 1, "kind": "lightgbm", "features": ["x"], "numeric_identity_probe": 1e-6,
             "metadata": {"train_latest_label_available_at": "2022-12-30", "train_max_as_of": "2022-12-10"}}
    model["model_id"] = hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    write_json(tmp_path / "model.json", model)
    protocol = {"settings": settings, "parameters": {"account": {"mode": "spot"}},
                "final_holdout_opened": False,
                "data_evidence": {"data_registration_sha256": sha256_file(tmp_path / "data.json"),
                    "baseline_registration_sha256": sha256_file(tmp_path / "baseline.json"),
                    "historical_universe_verified": True}}
    protocol["protocol_id"] = hashlib.sha256(canonical_json(protocol).encode()).hexdigest()
    write_json(tmp_path / "protocol.json", protocol)
    bundle = {"schema": "frozen-coin-selector/v1", "candidate": {"selected_candidate": "supervised", "model_id": model["model_id"]},
              "files": {"model": {"path": "model.json", "sha256": sha256_file(tmp_path / "model.json"), "model_id": model["model_id"]}},
              "training_end_exclusive": "2023-01-01", "validation_end_exclusive": "2024-07-01",
              "source_protocol_id": protocol["protocol_id"], "source_protocol_sha256": sha256_file(tmp_path / "protocol.json")}
    write_json(tmp_path / "bundle.json", bundle)
    return tmp_path


def audit(folder, **kwargs):
    return audit_readiness(folder / "settings.yaml", folder / "bundle.json", root=folder,
                           protocol_path=folder / "protocol.json", as_of="2026-10-04", **kwargs)


def test_verified_bytes_still_pending_pit_and_account_reproduction(evidence):
    report = audit(evidence)
    assert report["status"] == "pending_external_evidence"
    assert not report["training_performed"] and not report["backtest_performed"]
    assert not report["formal_admission"]
    assert report["watermarks"]["market_common_end"] == "2024-01-02T00:00:00+00:00"
    assert "historical_pit_sources_and_coverage_not_reverified" in report["pending_reasons"]
    checks = {row["name"]: row for row in report["checks"]}
    assert checks["model_identity"]["status"] == "verified"
    assert checks["account_mode"]["status"] == "verified"
    assert checks["historical_pit"]["declared_verified"] is True


def test_missing_external_sources_are_pending_and_no_credentials_requested(evidence):
    (evidence / "data.json").unlink()
    report = audit_readiness(evidence / "settings.yaml", evidence / "bundle.json", root=evidence)
    assert report["status"] == "pending_external_evidence"
    assert "missing_external_artifact" in report["pending_reasons"]
    assert "original_frozen_protocol_not_available" in report["pending_reasons"]
    assert report["watermarks"]["market_common_end"] is None


def test_model_tamper_and_account_mismatch_block(evidence):
    payload = json.loads((evidence / "model.json").read_text())
    payload["features"] = ["wrong_feature"]
    write_json(evidence / "model.json", payload)
    report = audit(evidence, serving_account_mode="spot_margin")
    assert report["status"] == "blocked"
    assert "artifact_identity_mismatch" in report["failed_reasons"]
    assert "training_serving_account_mismatch" in report["failed_reasons"]


def test_input_tamper_reports_frame_identity_and_file_hash_failures(evidence):
    source = evidence / "input/engine/A_USDT.csv"
    source.write_text(source.read_text().replace("2.0,3.0", "2.0,4.0"))
    report = audit(evidence)
    assert report["status"] == "blocked"
    assert any(row["name"] == "input_file" and row["status"] == "failed" for row in report["checks"])
    assert any(row["name"] == "engine_frame" and row["status"] == "failed" for row in report["checks"])


def test_bundle_traversal_fails_without_loading_external_model(evidence):
    bundle = json.loads((evidence / "bundle.json").read_text())
    bundle["files"]["model"]["path"] = "../outside.json"
    write_json(evidence / "bundle.json", bundle)
    assert "artifact_path_escapes_bundle" in audit(evidence)["failed_reasons"]


def set_account_contract(folder, contract, *, bundle_contract=None):
    model = json.loads((folder / "model.json").read_text())
    del model["model_id"]
    model["metadata"]["account_contract"] = contract
    model["model_id"] = hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    write_json(folder / "model.json", model)
    bundle = json.loads((folder / "bundle.json").read_text())
    bundle["candidate"]["model_id"] = model["model_id"]
    bundle["files"]["model"].update(model_id=model["model_id"], sha256=sha256_file(folder / "model.json"))
    if bundle_contract is not None:
        bundle["account_contract"] = bundle_contract
    write_json(folder / "bundle.json", bundle)


def test_explicit_frozen_account_contract_is_enforced_without_model_loading(evidence):
    account = {"training_account_mode": "spot", "deployment_account_modes": ["spot"],
               "compatibility_verified": True, "cross_account_transfer_verified": False}
    set_account_contract(evidence, account)
    report = audit(evidence)
    assert report["account_contract"]["status"] == "verified_supported_account"
    assert report["status"] == "pending_external_evidence"
    rejected = audit(evidence, serving_account_mode="spot_margin")
    assert "frozen_account_contract_invalid" in rejected["failed_reasons"]
    set_account_contract(evidence, account, bundle_contract={**account, "compatibility_verified": False})
    assert "frozen_account_contract_invalid" in audit(evidence)["failed_reasons"]


def test_frozen_contract_cannot_contradict_original_training_account(evidence):
    set_account_contract(evidence, {"training_account_mode": "spot_margin", "deployment_account_modes": ["spot"],
        "compatibility_verified": True, "cross_account_transfer_verified": True})
    assert "frozen_contract_training_account_disagrees_with_protocol" in audit(evidence)["failed_reasons"]


def test_new_final_contract_cannot_hide_opened_holdout(evidence):
    settings = yaml.safe_load((evidence / "settings.yaml").read_text())
    settings["evaluation_protocol"] = {"final_sample": {"opened": True}}
    (evidence / "settings.yaml").write_text(yaml.safe_dump(settings))
    report = audit(evidence)
    assert "final_already_opened" in report["failed_reasons"]
    assert report["independent_holdout"] is False


def test_readiness_cli_writes_immutable_report_and_no_training(evidence, capsys):
    from scripts.audit_ml_readiness import main
    output = evidence / "audit.json"
    args = ["--root", str(evidence), "--settings", str(evidence / "settings.yaml"),
            "--bundle", str(evidence / "bundle.json"), "--protocol", str(evidence / "protocol.json"),
            "--output", str(output)]
    assert main(args) == 0
    assert not json.loads(output.read_text())["training_performed"]
    with pytest.raises(SystemExit):
        main(args)
    capsys.readouterr()


def test_drift_keeps_missingness_and_unknown_activity_separate_from_rejection():
    report = describe_serving_drift([{"x": 0}, {"x": 2}], [
        {"features": {"x": 4}, "as_of": "2026-10-01", "information_cutoff": "2026-10-01",
         "eligible": True, "selected": False, "selection_probability": .495, "policy_threshold": .51},
        {"as_of": "2026-10-02", "information_cutoff": "2026-10-04", "eligible": False,
         "selected": False, "selection_probability": .496, "policy_threshold": .51}], features=["x"],
        training_domain={"account_mode": "spot"}, serving_domain={"account_mode": "spot_margin"})
    assert report["feature_missingness"]["x"] == .5
    assert report["feature_mean_shift"]["x"]["standardized_mean_shift"] == 3
    assert report["staleness"]["stale_rows"] == 1
    assert report["selection"]["maximum_consecutive_all_rejected_dates"] == 2
    assert report["actual_activity"]["maximum_consecutive_no_fill_dates"] is None
    assert report["action_probability"]["at_or_above_frozen_threshold"] == 0
    assert report["action_probability"]["meaning"].startswith("Probability of the policy accept action")
    assert report["domain_comparison"]["matched"] is False
    assert report["drift_pass"] is None


def test_unknown_dates_break_no_fill_streak_and_future_information_is_reported():
    activity = [{"date": "2026-10-01", "fills": 0, "status": "completed"},
                {"date": "2026-10-02", "fills": None, "status": "failed"},
                {"date": "2026-10-03", "fills": 0, "status": "completed"},
                {"date": "2026-10-05", "fills": 0, "status": "completed"}]
    report = describe_serving_drift([], [{"x": 1, "as_of": "2026-10-05", "information_cutoff": "2026-10-04"}],
                                   features=["x"], activity=activity)
    assert report["staleness"]["future_information_rows"] == 1
    assert report["actual_activity"]["maximum_consecutive_no_fill_dates"] == 1
    assert report["actual_activity"]["unknown_rows"] == 1
    with pytest.raises(ValueError, match="unique"):
        describe_serving_drift([], [], features=["x"], activity=[activity[0], deepcopy(activity[0])])


def test_empty_drift_is_pending_not_a_statistical_pass():
    report = describe_serving_drift([], [], features=["x"])
    assert report["status"] == "pending_observations"
    assert report["feature_missingness"]["x"] is None
    assert report["eligibility"]["coverage"] is None
