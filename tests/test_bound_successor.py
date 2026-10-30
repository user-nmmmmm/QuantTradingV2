"""A5 (ADR 0032): a new registration is bound to an annotated release tag and a machine-checked configuration change."""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from analysis import strategy_review
from analysis.strategy_review import freeze_prospective, open_prospective
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
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
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
    assert receipt["identity_scheme"] == binding["receipt"]["scheme"] == "code-token/v2"
    assert any("code-token/v2" in limitation for limitation in receipt["limitations"])
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
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
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


def test_registration_requires_the_main_reference(world):
    git(world["repo_root"], "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(ValueError, match="required main reference"):
        register_bound_successor(**world)
    assert not world["output"].exists()


def assert_complete_registration(output):
    assert {path.name for path in output.iterdir()} == {
        "review_protocol.json", "parent_protocol.json", "config_change.json",
        "identity_binding.json", "prospective_protocol.json", "acceptance.json",
    }
    receipt = json.loads((output / "acceptance.json").read_text())
    protocol = strategy_review.validate_prospective(json.loads((output / "prospective_protocol.json").read_text()))
    assert receipt["protocol_hash"] == protocol["protocol_hash"]
    assert receipt["identity_binding_sha256"] == sha((output / "identity_binding.json").read_bytes())
    assert receipt["parent_protocol_sha256"] == sha((output / "parent_protocol.json").read_bytes())


def opening_args(world):
    path = world["historical_batch"] / "prospective_protocol.json"
    record = json.loads(path.read_text())
    return dict(path=path, code_hash=record["code_hash"], config_hash=record["config_hash"],
                registry_hash=record["experiment_registry_hash"], data_hash="test-data", labels_complete=True,
                now="2099-01-01T12:00:00Z")


@pytest.mark.parametrize("mutation", ["receipt", "protocol", "registry"])
def test_parent_changes_before_publication_are_rejected(world, monkeypatch, mutation):
    original = module._write
    parent = world["historical_batch"] / "prospective_protocol.json"

    def write_and_change(path, value):
        original(path, value)
        if path.name == "acceptance.json":
            if mutation == "receipt":
                parent.with_suffix(".json.opened").write_text("claimed before JSON rewrite", encoding="utf-8")
            elif mutation == "protocol":
                parent.write_bytes(parent.read_bytes() + b"\n")
            else:
                (world["historical_batch"] / "review_protocol.json").write_text("changed", encoding="utf-8")

    monkeypatch.setattr(module, "_write", write_and_change)
    with pytest.raises(ValueError, match="exclusive opening receipt|changed during registration"):
        register_bound_successor(**world)
    assert not world["output"].exists()
    assert not list(world["output"].parent.glob(".out.registration-*"))


@pytest.mark.parametrize("phase", ["copy", "binding", "protocol", "acceptance", "publish"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_interrupted_build_is_unpublished_and_retryable(world, monkeypatch, phase, failure):
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    with monkeypatch.context() as patch:
        if phase == "copy":
            def fail_copy(source, target):
                Path(target).write_bytes(b"partial copy")
                raise failure("interrupted copy")
            patch.setattr(module.shutil, "copyfile", fail_copy)
        elif phase in {"binding", "acceptance"}:
            original = module._write
            name = "identity_binding.json" if phase == "binding" else "acceptance.json"

            def fail_write(path, value):
                original(path, value)
                if path.name == name:
                    raise failure("interrupted receipt")
            patch.setattr(module, "_write", fail_write)
        elif phase == "protocol":
            original = module.freeze_prospective

            def fail_freeze(*args, **kwargs):
                original(*args, **kwargs)
                raise failure("interrupted freeze")
            patch.setattr(module, "freeze_prospective", fail_freeze)
        else:
            def fail_publish(path, target):
                raise failure("interrupted publication")
            patch.setattr(module, "_publish_registration", fail_publish)
        with pytest.raises(failure, match="interrupted"):
            register_bound_successor(**world)
    assert parent.read_bytes() == before
    assert not parent.with_suffix(".json.opened").exists()
    assert not world["output"].exists()
    assert not list(world["output"].parent.glob(".out.registration-*"))
    register_bound_successor(**world)
    assert_complete_registration(world["output"])


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_a_destination_created_during_build_is_never_overwritten(world, monkeypatch, kind):
    original = module._write
    output = world["output"]

    def write_and_create_destination(path, value):
        original(path, value)
        if path.name == "acceptance.json":
            if kind == "directory":
                output.mkdir()
            else:
                output.write_bytes(b"other owner")

    monkeypatch.setattr(module, "_write", write_and_create_destination)
    with pytest.raises(FileExistsError):
        register_bound_successor(**world)
    assert (list(output.iterdir()) == []) if kind == "directory" else (output.read_bytes() == b"other owner")
    assert not list(output.parent.glob(".out.registration-*"))


def test_a_busy_destination_does_not_publish_and_can_be_retried(world):
    with strategy_review.exclusive_path_lock(world["output"]):
        with pytest.raises(BlockingIOError, match="busy"):
            register_bound_successor(**world)
        assert not world["output"].exists()
    register_bound_successor(**world)
    assert_complete_registration(world["output"])


def registration_process(world, ready, resume, result):
    """Pause after the first staged copy, while the protocol lock is held."""
    original = module.shutil.copyfile

    def paused_copy(source, target):
        value = original(source, target)
        ready.set()
        if not resume.wait(30):
            raise TimeoutError("registration test was not resumed")
        return value

    module._utc_now = lambda: datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
    module.shutil.copyfile = paused_copy
    try:
        register_bound_successor(**world)
        result.put("registered")
    except BaseException as exc:
        result.put(type(exc).__name__)


def publication_process(world, ready, resume, result):
    """Pause after the final existence observation, before the native publication."""
    original = os.path.lexists
    observations = 0

    def paused_lexists(path):
        nonlocal observations
        exists = original(path)
        if Path(path) == world["output"]:
            observations += 1
            if observations == 2:
                assert not exists
                ready.set()
                if not resume.wait(30):
                    raise TimeoutError("publication test was not resumed")
        return exists

    module._utc_now = lambda: datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
    module.os.path.lexists = paused_lexists
    try:
        register_bound_successor(**world)
        result.put("registered")
    except BaseException as exc:
        result.put(type(exc).__name__)


def opening_process(world, ready, resume, result):
    """Pause after the durable opening claim, before replacing the protocol JSON."""
    original = Path.replace

    def paused_replace(path, target):
        ready.set()
        if not resume.wait(30):
            raise TimeoutError("opening test was not resumed")
        return original(path, target)

    Path.replace = paused_replace
    try:
        open_prospective(**opening_args(world))
        result.put("opened")
    except BaseException as exc:
        result.put(type(exc).__name__)


@pytest.fixture()
def paused_process():
    context = multiprocessing.get_context("spawn")
    running = []

    def start(target, world):
        ready, resume, result = context.Event(), context.Event(), context.Queue()
        process = context.Process(target=target, args=(world, ready, resume, result))
        process.start()
        running.append((process, resume, result))
        assert ready.wait(30), "child did not reach the requested interleaving"
        return process, resume, result

    yield start
    for process, resume, result in running:
        # A killed child can leave Event's internal semaphore locked. Never signal it after death.
        if process.is_alive():
            resume.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)
        result.close()


def test_registration_serializes_opening_and_another_registration(world, paused_process):
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    process, resume, result = paused_process(registration_process, world)
    assert not world["output"].exists()
    with pytest.raises(BlockingIOError, match="busy"):
        open_prospective(**opening_args(world))
    with pytest.raises(BlockingIOError, match="busy"):
        register_bound_successor(**world)
    assert parent.read_bytes() == before and not parent.with_suffix(".json.opened").exists()
    resume.set()
    assert result.get(timeout=30) == "registered"
    process.join(10)
    assert process.exitcode == 0
    assert_complete_registration(world["output"])
    # Releasing the mutex must not consume the historical opening claim.
    assert open_prospective(**opening_args(world))["status"] == "opened_for_final_evaluation"


def test_opening_serializes_registration_and_retains_the_claim_after_a_crash(world, paused_process):
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    process, _, _ = paused_process(opening_process, world)
    assert parent.read_bytes() == before and parent.with_suffix(".json.opened").exists()
    with pytest.raises(BlockingIOError, match="busy"):
        register_bound_successor(**world)
    process.kill()
    process.join(10)
    with pytest.raises(ValueError, match="exclusive opening receipt"):
        register_bound_successor(**world)
    assert not world["output"].exists()
    assert parent.read_bytes() == before and parent.with_suffix(".json.opened").exists()


def test_killed_registration_leaves_no_final_directory_and_retry_succeeds(world, paused_process):
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    process, _, _ = paused_process(registration_process, world)
    process.kill()
    process.join(10)
    assert not process.is_alive()
    assert not world["output"].exists()
    assert parent.read_bytes() == before and not parent.with_suffix(".json.opened").exists()
    register_bound_successor(**world)
    assert_complete_registration(world["output"])


def test_concurrent_different_parents_cannot_replace_an_existing_registration(world, paused_process, tmp_path):
    other = dict(world, historical_batch=tmp_path / "other-parent")
    shutil.copytree(world["historical_batch"], other["historical_batch"])
    process, resume, result = paused_process(registration_process, world)
    register_bound_successor(**other)
    output = world["output"]
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    resume.set()
    assert result.get(timeout=30) == "FileExistsError"
    process.join(10)
    assert process.exitcode == 0
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
    assert_complete_registration(output)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_noncooperating_writer_after_final_check_is_never_overwritten(world, paused_process, kind):
    output = world["output"]
    target = output.parent / "other-owner"
    if kind in {"symlink", "dangling_symlink"}:
        if kind == "symlink":
            target.mkdir()
            (target / "marker").write_bytes(b"other owner")
        try:
            output.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        output.unlink()
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    process, resume, result = paused_process(publication_process, world)
    assert not os.path.lexists(output)
    if kind == "directory":
        output.mkdir()
    elif kind == "file":
        output.write_bytes(b"other owner")
    else:
        output.symlink_to(target, target_is_directory=True)
    identity = output.lstat()
    link_target = os.readlink(output) if kind in {"symlink", "dangling_symlink"} else None
    resume.set()
    assert result.get(timeout=30) == "FileExistsError"
    process.join(10)
    assert process.exitcode == 0
    assert output.lstat() == identity
    if kind == "directory":
        assert list(output.iterdir()) == []
    elif kind == "file":
        assert output.read_bytes() == b"other owner"
    else:
        assert output.is_symlink() and os.readlink(output) == link_target
        if kind == "symlink":
            assert (target / "marker").read_bytes() == b"other owner"
    assert parent.read_bytes() == before and not parent.with_suffix(".json.opened").exists()
    assert not list(output.parent.glob(".out.registration-*"))


@pytest.mark.parametrize("unsupported", ["platform", "libc", "filesystem"])
def test_publication_without_native_no_replace_support_fails_closed(world, monkeypatch, unsupported):
    # Substitute the platform probe, not sys.platform itself: pathlib must keep its host flavor.
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="freebsd" if unsupported == "platform" else "linux"))
    if unsupported == "libc":
        monkeypatch.setattr(module.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    elif unsupported == "filesystem":
        def unsupported_rename(*args):
            module.ctypes.set_errno(errno.EOPNOTSUPP)
            return -1
        monkeypatch.setattr(module.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(renameat2=unsupported_rename))
    parent = world["historical_batch"] / "prospective_protocol.json"
    before = parent.read_bytes()
    with pytest.raises(OSError) as error:
        register_bound_successor(**world)
    assert error.value.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
    assert parent.read_bytes() == before and not parent.with_suffix(".json.opened").exists()
    assert not os.path.lexists(world["output"])
    assert not list(world["output"].parent.glob(".out.registration-*"))


def test_native_linux_publication_preserves_unrelated_os_errors(world, monkeypatch):
    def denied_rename(*args):
        module.ctypes.set_errno(errno.EACCES)
        return -1

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(renameat2=denied_rename))
    with pytest.raises(PermissionError) as error:
        register_bound_successor(**world)
    assert error.value.errno == errno.EACCES
    assert error.value.filename2 == str(world["output"])
    assert not os.path.lexists(world["output"])
    assert not list(world["output"].parent.glob(".out.registration-*"))
