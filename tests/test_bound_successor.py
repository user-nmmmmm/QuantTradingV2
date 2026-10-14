"""A5 (ADR 0032): a new registration is bound to an annotated release tag and a machine-checked configuration change."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from analysis.strategy_review import freeze_prospective
from scripts import register_bound_successor as module
from scripts.register_bound_successor import (
    STATEMENT_SCHEMA, check_config_change, register_bound_successor, structural_diff,
)

PARENT_CONFIG = b"strategy_governance: paused_revalidation\nallocation:\n  order: score_strategy_symbol\n"
NEW_CONFIG = PARENT_CONFIG + b"  capital:\n    enabled: false\n    max_positions: 8\n"
SCOPE = {"schema": "frozen-scope/v1", "directories": [{"prefix": "core/", "suffixes": [".py"]}], "files": []}
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [3.1.0] - 2026-10-20\n- release\n"
PYPROJECT = '[project]\nname = "x"\nversion = "3.1.0"\n'


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def statement(**overrides):
    value = {"schema": STATEMENT_SCHEMA, "reason": "disabled capital block added after the parent was frozen",
             "parent_config_sha256": sha(PARENT_CONFIG), "new_config_sha256": sha(NEW_CONFIG),
             "added": ["allocation.capital"]}
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(module, "_utc_now", lambda: datetime(2099, 1, 1, 12, tzinfo=timezone.utc))


@pytest.fixture()
def world(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "core").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "config/frozen_scope.json").write_text(json.dumps(SCOPE), encoding="utf-8")
    (repo / "config/params.yaml").write_bytes(NEW_CONFIG)
    (repo / "core/a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "release")
    git(repo, "tag", "-a", "v3.1.0", "-m", "Release 3.1.0")
    historical = tmp_path / "historical"
    (historical / "source/config").mkdir(parents=True)
    (historical / "source/config/params.yaml").write_bytes(PARENT_CONFIG)
    (historical / "review_protocol.json").write_text('{"selection":"train_only"}')
    freeze_prospective(historical / "prospective_protocol.json", code_hash="old-code", config_hash=sha(PARENT_CONFIG),
                       registry_hash=sha((historical / "review_protocol.json").read_bytes()),
                       frozen_at="2026-09-20T09:00:00Z")
    change = tmp_path / "config_change.json"
    change.write_text(json.dumps(statement()), encoding="utf-8")
    return dict(repo_root=repo, tag="v3.1.0", historical_batch=historical, config_change=change, output=tmp_path / "out")


def test_registration_binds_the_tag_and_starts_a_fresh_clock(world):
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    receipt = register_bound_successor(**world)
    out = world["output"]
    binding = json.loads((out / "identity_binding.json").read_text())
    assert parent.read_bytes() == before and (out / "parent_protocol.json").read_bytes() == before
    assert binding["tag"] == "v3.1.0" and receipt["tag_commit"] == binding["tag_commit"]
    assert receipt["code_hash"] == binding["receipt"]["identity_sha256"]
    assert receipt["config_hash"] == sha(NEW_CONFIG)
    assert receipt["config_change_sha256"] == sha(world["config_change"].read_bytes())
    assert receipt["test_start"] == "2099-02-01T00:00:00+00:00" and receipt["opened_at"] is None
    assert receipt["new_observation_inherits_elapsed_days"] is False and receipt["admission"] == "paused_revalidation"
    protocol = json.loads((out / "prospective_protocol.json").read_text())
    assert protocol["status"] == "pending_unseen_evidence" and protocol["code_hash"] == receipt["code_hash"]
    with pytest.raises(FileExistsError):
        register_bound_successor(**world)


def test_the_config_hash_does_not_depend_on_line_endings(world):
    repo = world["repo_root"]
    git(repo, "config", "core.autocrlf", "false")
    (repo / "config/params.yaml").write_bytes(NEW_CONFIG.replace(bytes([10]), bytes([13, 10])))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "crlf")
    assert module.config_hash_at(repo, "HEAD")[0] == sha(NEW_CONFIG)


REFUSALS = {
    "lightweight_tag": "release tag is not usable", "opened": "prior sample access", "opening_receipt": "exclusive opening receipt",
    "registry": "registry identity mismatch", "parent_snapshot": "does not match its frozen configuration",
    "backdated": "must follow the original registration", "tag_in_future": "dated after the registration",
    "statement_hash": "does not name the tagged configuration", "extra_change": "differs from the statement",
    "enabled_true": "disabled by default",
}


@pytest.mark.parametrize("mutation", ["lightweight_tag", "opened", "opening_receipt", "registry", "parent_snapshot",
                                      "backdated", "tag_in_future", "statement_hash", "extra_change", "enabled_true"])
def test_registration_refuses_unsafe_inputs_and_writes_nothing(world, mutation, monkeypatch):
    historical, repo = world["historical_batch"], world["repo_root"]
    parent = historical / "prospective_protocol.json"
    if mutation == "lightweight_tag":
        git(repo, "tag", "v3.1.0-rc.1")
        world["tag"] = "v3.1.0-rc.1"
    elif mutation == "opened":
        record = json.loads(parent.read_text())
        record["data_access_log"] = [{"purpose": "prior_access"}]
        parent.write_text(json.dumps(record))
    elif mutation == "opening_receipt":
        parent.with_suffix(".json.opened").write_text("claimed")
    elif mutation == "registry":
        (historical / "review_protocol.json").write_text("{}")
    elif mutation == "parent_snapshot":
        (historical / "source/config/params.yaml").write_bytes(PARENT_CONFIG + b"x: 1\n")
    elif mutation == "backdated":
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
    elif mutation == "tag_in_future":
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    elif mutation == "statement_hash":
        world["config_change"].write_text(json.dumps(statement(new_config_sha256="0" * 64)))
    elif mutation == "extra_change":
        world["config_change"].write_text(json.dumps(statement(added=["allocation.capital", "allocation.order"])))
    elif mutation == "enabled_true":
        world["config_change"].write_text(json.dumps(statement()))
        enabled = NEW_CONFIG.replace(b"enabled: false", b"enabled: true")
        (repo / "config/params.yaml").write_bytes(enabled)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "enable")
        git(repo, "tag", "-fa", "v3.1.0", "-m", "moved")
        world["config_change"].write_text(json.dumps(statement(new_config_sha256=sha(enabled))))
    with pytest.raises(ValueError, match=REFUSALS[mutation]):
        register_bound_successor(**world)
    assert not world["output"].exists()


def test_a_changed_configuration_outside_the_statement_is_visible():
    assert structural_diff({"a": {"b": 1}}, {"a": {"b": 2}, "c": 3}) == [("changed", "a.b"), ("added", "c")]
    with pytest.raises(ValueError, match="differs from the statement"):
        check_config_change(statement(), parent_config=PARENT_CONFIG, new_config=NEW_CONFIG.replace(b"order: score", b"order: other"),
                            parent_config_hash=sha(PARENT_CONFIG), new_config_hash=sha(NEW_CONFIG))
