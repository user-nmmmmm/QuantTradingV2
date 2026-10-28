"""The developer task runner mirrors CI and the scripts index stays complete (infra 0.6)."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from scripts import dev

ROOT = Path(__file__).resolve().parents[1]


def commands(task_name: str, extra=()):
    return dev.TASKS[task_name].build(extra)


def test_list_shows_every_task_and_check(capsys):
    assert dev.main(["list"]) == 0
    out = capsys.readouterr().out
    for name in dev.TASKS:
        assert re.search(rf"^{name}\s", out, re.M), name
    assert "check" in out


def test_check_runs_the_ci_sequence_and_can_skip_tests():
    names = [task.name for task, _ in dev.plan(["check"])]
    assert names == dev.CHECK_ORDER and {"frontend", "test", "sandbox"} <= set(names)
    short = [task.name for task, _ in dev.plan(["check"], no_tests=True)]
    assert short == [name for name in dev.CHECK_ORDER if name not in {"test", "sandbox"}]


def test_extra_arguments_reach_pytest_and_unknown_tasks_fail():
    (_, steps), = dev.plan(["test"], ["-k", "ledger"])
    assert steps[0][-2:] == ["-k", "ledger"] and "pytest" in steps[0]
    assert "not slow" in " ".join(commands("fast")[0])
    with pytest.raises(SystemExit):
        dev.plan(["no-such-task"])


def test_a_failing_task_does_not_stop_the_rest_and_sets_the_exit_code(capsys):
    boom = dev.Task("boom", "fails", lambda extra: [[sys.executable, "-c", "raise SystemExit(3)"]])
    fine = dev.Task("fine", "passes", lambda extra: [[sys.executable, "-c", "pass"]])
    missing = dev.Task("missing", "no such tool", lambda extra: [["definitely-not-installed-xyz"]])
    results = dev.run([(boom, boom.build(())), (fine, fine.build(())), (missing, missing.build(()))])
    assert results == {"boom": 3, "fine": 0, "missing": 127}
    assert "FAIL boom" in capsys.readouterr().out


def test_every_check_task_is_present_in_the_ci_quality_job():
    """Every CI gate must use the shared task, in both directions and in the same order."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    tasks = []
    for step in workflow["jobs"]["quality"]["steps"]:
        if "run" not in step:
            continue
        if step["name"] in {"Install dependencies (runtime versions come from the lock)",
                            "Install the project (editable)", "Report frozen-scope identity impact (informational)"}:
            continue
        if step["name"] == "Preserve exact checkout identity":
            assert step.get("if") == "always()"
            assert step["run"].splitlines() == [
                "mkdir -p outputs/current-ci",
                "git rev-parse HEAD > outputs/current-ci/checkout-sha.txt",
                "git rev-parse 'HEAD^{tree}' > outputs/current-ci/checkout-tree.txt",
            ]
            continue
        command = shlex.split(step["run"])
        assert "if" not in step, step["name"]
        assert command[:2] == ["python", "scripts/dev.py"], step["name"]
        expected_reports = [
            "--", "--junitxml=outputs/current-ci/full-tests.xml",
            "--cov-report=term", "--cov-report=json:outputs/current-ci/runtime-coverage.json",
        ] if command[2] == "test" else []
        assert command[3:] == expected_reports, step["name"]
        tasks.append(command[2])
    assert tasks == dev.CHECK_ORDER


@pytest.mark.parametrize("has_xdist", [False, True])
def test_full_tests_always_enforce_the_same_coverage_gate(monkeypatch, has_xdist):
    monkeypatch.setattr(dev.importlib.util, "find_spec", lambda name: object() if has_xdist else None)
    command, = commands("test")
    assert {"--cov=core", "--cov=ops", "--cov=signal_meta", "--cov=backtest",
            "--cov=live_trading", "--cov-fail-under=90", "--durations=20"} <= set(command)
    assert ("-n" in command) == has_xdist  # execution mode may change, the coverage requirement may not
    assert not any(arg.startswith("--cov") for arg in commands("fast")[0])


def test_frontend_expands_every_matching_file_without_relying_on_the_shell(tmp_path, monkeypatch):
    folder = tmp_path / "tests"
    folder.mkdir()
    for name in ("dashboard_b.test.mjs", "dashboard_a.test.mjs", "other.test.mjs"):
        (folder / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    command, = commands("frontend")
    assert command[:2] == ["node", "--test"]
    assert [Path(name).name for name in command[2:]] == ["dashboard_a.test.mjs", "dashboard_b.test.mjs"]
    for name in command[2:]:
        assert Path(name).is_file()


def test_missing_frontend_tests_fail_instead_of_running_zero_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="dashboard_.*test.mjs"):
        commands("frontend")


def test_low_coverage_returns_failure_through_the_shared_runner(tmp_path, monkeypatch):
    (tmp_path / "sample.py").write_text("def uncovered():\n    return 42\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text("def test_passes():\n    assert True\n", encoding="utf-8")
    # Use the real pytest-cov exit status with an isolated, intentionally uncovered module.
    monkeypatch.setattr(dev.importlib.util, "find_spec", lambda name: None)
    command = ["--cov=sample" if arg == "--cov=core" else arg for arg in commands("test")[0]
               if not arg.startswith("--cov=") or arg == "--cov=core"]
    task = dev.Task("test", "isolated coverage gate", lambda extra: [command])
    assert dev.run([(task, task.build(()))], cwd=tmp_path) == {"test": 1}


def test_a_frontend_assertion_failure_is_reported_by_the_runner(tmp_path, monkeypatch):
    folder = tmp_path / "tests"
    folder.mkdir()
    (folder / "dashboard_failure.test.mjs").write_text(
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "test('regression sentinel', () => assert.equal(1, 2));\n", encoding="utf-8")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    task = dev.TASKS["frontend"]
    assert dev.run([(task, task.build(()))], cwd=tmp_path) == {"frontend": 1}


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_pyproject_changes_trigger_container_ci(event):
    workflow = yaml.load((ROOT / ".github/workflows/devcontainer.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
    assert "pyproject.toml" in workflow["on"][event]["paths"]
    assert "python scripts/dev.py check --no-tests" in workflow["jobs"]["build-and-check"]["steps"][-1]["with"]["runCmd"]


def test_scripts_index_lists_every_script_and_only_existing_ones():
    index = (ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    scripts = sorted(path.name for path in (ROOT / "scripts").glob("*.py"))
    missing = [name for name in scripts if name not in index]
    assert not missing, "add these scripts to scripts/README.md: " + ", ".join(missing)
    for name in set(re.findall(r"\[`([\w.]+\.py)`\]\(\1\)", index)):  # same-directory links only
        assert (ROOT / "scripts" / name).is_file(), f"scripts/README.md lists a missing script: {name}"
