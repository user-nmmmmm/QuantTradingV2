"""D1 inventories are reviewable local facts, never implicit cleanup approval."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import repository_governance as governance


def run(repo, *args, data=None):
    result = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                            env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0"), check=True)
    return result.stdout.decode("utf-8").removesuffix("\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repository with spaces"
    root.mkdir()
    run(root, "init", "-b", "main")
    run(root, "config", "user.name", "Fixture")
    run(root, "config", "user.email", "fixture@example.invalid")
    run(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("outputs/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run(root, "add", ".gitignore", "tracked.txt")
    run(root, "commit", "-m", "initial")
    return root


def commit(repo, message="next"):
    (repo / "tracked.txt").write_text(message + "\n", encoding="utf-8")
    run(repo, "add", "tracked.txt")
    run(repo, "commit", "-m", message)
    return run(repo, "rev-parse", "HEAD")


def test_exact_refs_divergence_and_symbolic_aliases(repo):
    old = run(repo, "rev-parse", "HEAD")
    run(repo, "branch", "already-reachable", old)
    run(repo, "update-ref", "refs/remotes/old-origin/archive", old)
    current = commit(repo)
    tree = run(repo, "rev-parse", old + "^{tree}")
    unique = run(repo, "commit-tree", tree, "-p", old, "-m", "unique topic")
    run(repo, "update-ref", "refs/heads/needs-review", unique)
    run(repo, "update-ref", "refs/remotes/origin/main", current)
    run(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    result = governance.snapshot(repo, "refs/remotes/origin/main")
    branches = {item["ref"]: item for item in result["branches"]}
    assert result["base_sha"] == current
    assert result["remote_state"] == "local_tracking_refs_only"
    assert "refs/remotes/origin/HEAD" not in branches
    assert branches["refs/heads/main"]["review"] == "keep_base"
    reachable = branches["refs/heads/already-reachable"]
    assert (reachable["sha"], reachable["ahead"], reachable["behind"]) == (old, 0, 1)
    assert reachable["reachable_from_base"] and reachable["review"] == "review_reachable"
    divergent = branches["refs/heads/needs-review"]
    assert (divergent["sha"], divergent["ahead"], divergent["behind"]) == (unique, 1, 1)
    assert not divergent["reachable_from_base"] and divergent["review"] == "review_unique_commits"
    assert branches["refs/remotes/old-origin/archive"]["sha"] == old
    assert result["owner_approval"] == "required_per_ref_sha_and_worktree"


def test_dirty_and_clean_worktrees_are_distinguished_without_listing_private_files(repo):
    assert governance.snapshot(repo, "main")["worktrees"][0]["status"] == "clean"
    (repo / "private-untracked.txt").write_text("private payload", encoding="utf-8")
    result = governance.snapshot(repo, "main")
    assert result["worktrees"][0]["status"] == "dirty"
    assert "private-untracked" not in json.dumps(result)
    assert "private payload" not in json.dumps(result)


@pytest.mark.parametrize("value", [" /tmp/repository ", "/tmp/repository\t", "/tmp/repository\n",
                                 "/tmp/repository\r\n", "/tmp/repository\u00a0", "main", ""])
def test_text_removes_only_the_git_protocol_newline(monkeypatch, value):
    monkeypatch.setattr(governance, "git", lambda *args: (value + "\n").encode("utf-8"))
    assert governance.text(Path("."), "rev-parse", "--show-toplevel") == value


@pytest.mark.parametrize("suffix", [
    "\u00a0",
    pytest.param(" ", marks=pytest.mark.skipif(os.name == "nt", reason="Windows trims trailing spaces")),
    pytest.param("\n", marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline paths")),
])
def test_repository_whitespace_survives_inventory_and_receipt(repo, suffix):
    root = repo.with_name(repo.name + suffix)
    repo.rename(root)
    result = governance.snapshot(root, "main")
    assert Path(result["repository"]) == root.resolve()
    assert Path(result["worktrees"][0]["path"]).resolve() == root.resolve()
    assert result["worktrees"][0]["status"] == "clean"
    output = governance.write_snapshot(root, Path("outputs/review.json"), result)
    assert output == root.resolve() / "outputs/review.json"
    assert json.loads(output.read_text(encoding="utf-8"))["repository"] == str(root.resolve())


@pytest.mark.parametrize("suffix", [
    "\u00a0",
    pytest.param(" ", marks=pytest.mark.skipif(os.name == "nt", reason="Windows trims trailing spaces")),
    pytest.param("\n", marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline paths")),
])
def test_linked_worktree_whitespace_survives_status_validation(repo, tmp_path, suffix):
    attached = tmp_path / ("attached worktree" + suffix)
    run(repo, "worktree", "add", "-b", "whitespace-topic", str(attached))
    commit(repo)
    result = governance.snapshot(repo, "main")
    item = next(item for item in result["worktrees"] if item["branch"] == "refs/heads/whitespace-topic")
    assert Path(item["path"]).resolve() == attached.resolve()
    assert item["status"] == "clean"
    branch = next(item for item in result["branches"] if item["ref"] == "refs/heads/whitespace-topic")
    assert [Path(path).resolve() for path in branch["worktrees"]] == [attached.resolve()]
    assert branch["review"] == "review_worktree"


def test_checked_out_locked_branch_and_detached_worktree_require_individual_review(repo, tmp_path):
    old = run(repo, "rev-parse", "HEAD")
    run(repo, "branch", "checked-out", old)
    current = commit(repo)
    attached = tmp_path / "attached worktree"
    detached = tmp_path / "detached worktree"
    run(repo, "worktree", "add", str(attached), "checked-out")
    run(repo, "worktree", "lock", "--reason", "retain evidence", str(attached))
    run(repo, "worktree", "add", "--detach", str(detached), old)
    result = governance.snapshot(repo, current)
    worktrees = {Path(item["path"]).resolve(): item for item in result["worktrees"]}
    assert worktrees[attached.resolve()]["locked"]
    assert worktrees[attached.resolve()]["lock_reason"] == "retain evidence"
    assert worktrees[attached.resolve()]["status"] == "clean"
    assert worktrees[detached.resolve()]["detached"] and worktrees[detached.resolve()]["branch"] is None
    branch = next(item for item in result["branches"] if item["ref"] == "refs/heads/checked-out")
    assert branch["reachable_from_base"] and branch["review"] == "review_worktree"
    assert [Path(path).resolve() for path in branch["worktrees"]] == [attached.resolve()]


def test_prunable_registration_is_not_a_clean_worktree(repo, tmp_path):
    # Let Git create its complete registration, then simulate the checkout going missing.
    absent = tmp_path / "absent worktree"
    displaced = tmp_path / "displaced worktree"
    assert absent.resolve().parent == displaced.resolve().parent == tmp_path.resolve()
    run(repo, "worktree", "add", "-b", "missing", str(absent))
    absent.rename(displaced)
    assert not absent.exists() and (displaced / ".git").is_file()
    assert "prunable" in run(repo, "worktree", "list", "--porcelain")
    result = governance.snapshot(repo, "main")
    missing = next(item for item in result["worktrees"] if Path(item["path"]).resolve() == absent.resolve())
    assert missing["prunable"] and missing["prune_reason"]
    assert missing["status"] == "unavailable"
    assert any(item["ref"] == "refs/heads/missing" and item["worktrees"] for item in result["branches"])


def test_nul_parser_preserves_newlines_quotes_and_empty_lock_reasons():
    raw = ('worktree /tmp/a "quoted"\npath\0HEAD ' + "a" * 40
           + '\0detached\0locked\0\0worktree /tmp/b\0HEAD ' + "b" * 40
           + '\0branch refs/heads/topic\0prunable missing\0\0').encode()
    first, second = governance.parse_worktrees(raw)
    assert first["path"] == '/tmp/a "quoted"\npath'
    assert first["locked"] and first["lock_reason"] == "" and first["detached"]
    assert second["branch"] == "refs/heads/topic" and second["prune_reason"] == "missing"


@pytest.mark.parametrize("raw", [b"HEAD abc\0\0", b"worktree /tmp/a\0\0"])
def test_malformed_worktree_records_are_rejected(raw):
    with pytest.raises(governance.InventoryError, match="invalid worktree"):
        governance.parse_worktrees(raw)


@pytest.mark.parametrize("base", ["missing-ref", "--help", "refs/heads/main~99"])
def test_missing_or_option_like_base_is_rejected(repo, base):
    with pytest.raises(governance.InventoryError):
        governance.snapshot(repo, base)


def test_shallow_history_is_rejected_before_classifying_refs(repo):
    (repo / ".git/shallow").write_text(run(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    with pytest.raises(governance.InventoryError, match="complete history required"):
        governance.snapshot(repo, "main")


@pytest.mark.parametrize("change", ["ref", "base", "worktree"])
def test_drift_rejects_the_snapshot(repo, monkeypatch, change):
    original = governance.git
    reads = 0

    def changing(root, *args):
        nonlocal reads
        key = {"ref": "for-each-ref", "base": "rev-parse", "worktree": "worktree"}[change]
        matches = args[0] == key and (change != "base" or "--verify" in args)
        if matches:
            reads += 1
        value = original(root, *args)
        if matches and reads == 2:
            if change == "ref":
                return value + b"refs/heads/new\t" + b"a" * 40 + b"\t\n"
            if change == "base":
                return b"b" * 40 + b"\n"
            return value.replace(b"\0\0", b"\0locked changed\0\0", 1)
        return value

    monkeypatch.setattr(governance, "git", changing)
    with pytest.raises(governance.InventoryError, match="changed during inventory"):
        governance.snapshot(repo, "main")


def test_unreadable_status_is_unavailable_and_git_stderr_is_not_published(repo, monkeypatch):
    original = governance.git

    def unreadable(root, *args):
        if args[0] == "status":
            raise governance.InventoryError("status unavailable")
        return original(root, *args)

    monkeypatch.setattr(governance, "git", unreadable)
    assert governance.snapshot(repo, "main")["worktrees"][0]["status"] == "unavailable"


def test_foreign_repository_at_a_worktree_path_is_unavailable(repo, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    run(foreign, "init", "-b", "main")
    item = {"path": str(foreign), "prunable": False}
    assert governance.worktree_status(repo, item, (repo / ".git").resolve()) == "unavailable"


def test_cli_writes_a_new_ignored_local_receipt_without_mutating_git(repo, capsys):
    before_refs = run(repo, "show-ref")
    before_index = (repo / ".git/index").read_bytes()
    before_worktrees = run(repo, "worktree", "list", "--porcelain")
    assert governance.main(["--repo", str(repo), "--base", "main", "--output", "outputs/d1.json"]) == 0
    output = repo / "outputs/d1.json"
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema"] == "repository-governance/v1"
    assert result["base_sha"] == run(repo, "rev-parse", "main")
    assert run(repo, "show-ref") == before_refs
    assert (repo / ".git/index").read_bytes() == before_index
    assert run(repo, "worktree", "list", "--porcelain") == before_worktrees
    assert "owner review required, no cleanup performed" in capsys.readouterr().out
    before = output.read_bytes()
    assert governance.main(["--repo", str(repo), "--base", "main", "--output", "outputs/d1.json"]) == 1
    assert output.read_bytes() == before


@pytest.mark.parametrize("name", [
    "review\u00a0.json", 'review "quoted".json' if os.name != "nt" else "review quoted.json",
    pytest.param("review\npath.json", marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline paths")),
])
def test_ignored_receipt_paths_do_not_depend_on_git_quoting_or_lines(repo, name):
    output = governance.write_snapshot(repo, Path("outputs") / name, {"preserved": True})
    assert output.name == name
    assert json.loads(output.read_text(encoding="utf-8")) == {"preserved": True}


@pytest.mark.parametrize("output", ["docs/d1.json", "outputs/../../outside.json", "outputs/../public.json"])
def test_receipt_cannot_be_written_outside_ignored_outputs(repo, output):
    with pytest.raises(governance.InventoryError, match="ignored outputs"):
        governance.write_snapshot(repo, Path(output), {})
    assert not (repo / "docs").exists()


def test_tracked_output_is_never_overwritten_even_if_it_matches_an_ignore_rule(repo):
    output = repo / "outputs/retained.json"
    output.parent.mkdir()
    output.write_text("retained evidence\n", encoding="utf-8")
    run(repo, "add", "-f", "outputs/retained.json")
    with pytest.raises(governance.InventoryError, match="tracked file"):
        governance.write_snapshot(repo, output, {})
    assert output.read_text(encoding="utf-8") == "retained evidence\n"


def test_outputs_must_actually_be_ignored(repo):
    (repo / ".gitignore").write_text("", encoding="utf-8")
    with pytest.raises(governance.InventoryError):
        governance.write_snapshot(repo, Path("outputs/d1.json"), {})
    assert not (repo / "outputs").exists()


def test_failed_snapshot_does_not_create_a_partial_receipt(repo, capsys):
    assert governance.main(["--repo", str(repo), "--base", "missing", "--output", "outputs/d1.json"]) == 1
    assert not (repo / "outputs").exists()
    assert "ERROR:" in capsys.readouterr().err
