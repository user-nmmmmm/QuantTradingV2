import hashlib
import json
from datetime import datetime, timezone

import pytest

from analysis.strategy_review import freeze_prospective, open_prospective
from scripts.register_strategy_successor import register_successor


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("scripts.register_strategy_successor._utc_now",
                        lambda: datetime(2026, 9, 20, 12, tzinfo=timezone.utc))


def setup_candidate(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config/params.yaml").write_text("strategy_governance: paused_revalidation\n")
    (root / "main.py").write_text("# repaired candidate\n")
    historical = tmp_path / "historical"
    historical.mkdir()
    (historical / "review_protocol.json").write_text('{"selection":"train_only"}')
    hash_file = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    freeze_prospective(historical / "prospective_protocol.json", code_hash="old-code",
        config_hash=hash_file(root / "config/params.yaml"),
        registry_hash=hash_file(historical / "review_protocol.json"), frozen_at="2026-09-19T04:00:00Z")
    return dict(source_root=root, historical_batch=historical, output=tmp_path / "successor")


def test_successor_has_new_window_snapshot_and_preserves_parent(tmp_path):
    args = setup_candidate(tmp_path)
    parent = args["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    receipt = register_successor(**args)
    assert parent.read_bytes() == before
    assert receipt["test_start"] == "2026-10-21T00:00:00+00:00"
    assert receipt["mature_after"] == "2027-05-09T00:00:00+00:00"
    assert receipt["admission"] == "paused_revalidation"
    assert (args["output"] / "source/main.py").read_bytes() == (args["source_root"] / "main.py").read_bytes()
    with pytest.raises(FileExistsError):
        register_successor(**args)


@pytest.mark.parametrize("mutation", ["maturity", "registry", "configuration", "backdate", "opening_receipt"])
def test_successor_rejects_changed_or_consumed_protocol(tmp_path, mutation, monkeypatch):
    args = setup_candidate(tmp_path)
    parent = args["historical_batch"] / "prospective_protocol.json"
    if mutation == "maturity":
        record = json.loads(parent.read_text())
        record["mature_after"] = "2026-09-20T00:00:00Z"
        parent.write_text(json.dumps(record))
    elif mutation == "registry":
        (args["historical_batch"] / "review_protocol.json").write_text("{}")
    elif mutation == "configuration":
        (args["source_root"] / "config/params.yaml").write_text("changed: true")
    elif mutation == "opening_receipt":
        parent.with_suffix(".json.opened").write_text("crash after exclusive claim")
    else:
        monkeypatch.setattr("scripts.register_strategy_successor._utc_now",
                            lambda: datetime(2026, 9, 18, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        register_successor(**args)
    assert not args["output"].exists()


def test_tampered_maturity_cannot_open_final_sample(tmp_path):
    args = setup_candidate(tmp_path)
    parent = args["historical_batch"] / "prospective_protocol.json"
    record = json.loads(parent.read_text())
    record["mature_after"] = "2026-09-20T00:00:00Z"
    parent.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="hash"):
        open_prospective(parent, code_hash=record["code_hash"], config_hash=record["config_hash"],
            registry_hash=record["experiment_registry_hash"], data_hash="data", labels_complete=True,
            now="2026-09-21T00:00:00Z")
    assert not parent.with_suffix(".json.opened").exists()


def test_string_false_is_not_complete_label_evidence(tmp_path):
    args = setup_candidate(tmp_path)
    parent = args["historical_batch"] / "prospective_protocol.json"
    record = json.loads(parent.read_text())
    with pytest.raises(PermissionError, match="Complete"):
        open_prospective(parent, code_hash=record["code_hash"], config_hash=record["config_hash"],
            registry_hash=record["experiment_registry_hash"], data_hash="data", labels_complete="false",
            now="2028-01-01T00:00:00Z")
    assert not parent.with_suffix(".json.opened").exists()


def test_pending_protocol_with_access_log_cannot_be_registered(tmp_path):
    args = setup_candidate(tmp_path)
    parent = args["historical_batch"] / "prospective_protocol.json"
    record = json.loads(parent.read_text())
    record["data_access_log"] = [{"purpose": "prior_access"}]
    parent.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="prior sample"):
        register_successor(**args)
    assert not args["output"].exists()
