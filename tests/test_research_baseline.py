from __future__ import annotations

import json
import subprocess

import pytest

from analysis.research_baseline import ci_python_versions, current_state, diff_states, environment_state, git_state
from scripts.capture_research_baseline import main


def run_git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    run_git(tmp_path, "init", "-q")
    (tmp_path / "core").mkdir()
    (tmp_path / "core/a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/tests.yml").write_text('python-version: "3.11"\n  python-version: ["3.11", "3.13"]\n', encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_git_state_separates_tracked_changes_from_untracked_sources(repo):
    (repo / "core/a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "core/untracked.py").write_text("y = 1\n", encoding="utf-8")
    state = git_state(repo)
    assert state["tracked_changes"] == ["core/a.py"] and state["untracked_files"] == ["core/untracked.py"]
    assert len(state["head"]) == 40


def test_source_identity_in_state_covers_untracked_files(repo):
    before = current_state(repo)
    (repo / "core/untracked.py").write_text("y = 1\n", encoding="utf-8")
    after = current_state(repo)
    assert "core/untracked.py" in after["source_identity"]["files"]
    assert before["source_identity"]["sha256"] != after["source_identity"]["sha256"]


def test_diff_reports_added_removed_and_changed_sources(repo):
    before = current_state(repo)
    (repo / "core/a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "core/b.py").write_text("z = 3\n", encoding="utf-8")
    diff = diff_states(before, current_state(repo))
    assert diff["source_changed"] == ["core/a.py"] and diff["source_added"] == ["core/b.py"]
    assert diff["source_removed"] == [] and diff["untracked_added"] == ["core/b.py"] and diff["head_changed"] is False


def test_missing_previous_baseline_is_not_reported_as_unchanged(repo):
    diff = diff_states(None, current_state(repo))
    assert diff["status"] == "no_previous_baseline" and "source_changed" not in diff


def test_environment_state_flags_ci_interpreter_mismatch(repo):
    assert ci_python_versions(repo) == ["3.11", "3.13"]
    env = environment_state(repo)
    assert env["python_matches_ci"] is (".".join(env["python"].split(".")[:2]) in {"3.11", "3.13"})
    (repo / ".github/workflows/tests.yml").unlink()
    assert environment_state(repo)["python_matches_ci"] is None


def test_known_failures_are_recorded_verbatim_and_separately(repo):
    assert current_state(repo, known_failures=["tests/x.py::t failed before this work"])["known_failures"] == [
        "tests/x.py::t failed before this work"]
    assert current_state(repo)["known_failures"] == []


def test_cli_writes_three_files_and_refuses_existing_directory(repo, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "baseline"
    assert main(["--root", str(repo), "--output", str(out), "--known-failure", "pre-existing"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["baseline_diff.json", "current_state.json", "environment.json"]
    assert json.loads((out / "baseline_diff.json").read_text(encoding="utf-8"))["status"] == "no_previous_baseline"
    with pytest.raises(FileExistsError):
        main(["--root", str(repo), "--output", str(out)])
    out2 = out.with_name("baseline2")
    (repo / "core/a.py").write_text("x = 5\n", encoding="utf-8")
    assert main(["--root", str(repo), "--output", str(out2), "--previous", str(out / "current_state.json")]) == 0
    assert json.loads((out2 / "baseline_diff.json").read_text(encoding="utf-8"))["source_changed"] == ["core/a.py"]
