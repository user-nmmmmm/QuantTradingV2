"""The developer task runner mirrors CI and the scripts index stays complete (infra 0.6)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

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
    assert names == dev.CHECK_ORDER and names[-1] == "test"
    short = [task.name for task, _ in dev.plan(["check"], no_tests=True)]
    assert short == dev.CHECK_ORDER[:-1]


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
    """If CI gains or changes a check, the local runner has to follow (and vice versa)."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    for name in dev.CHECK_ORDER:
        for command in commands(name):
            if Path(command[0]).stem.startswith("lint-imports"):
                needle = "lint-imports"
            elif name == "test":
                needle = "-m pytest"
            else:
                needle = " ".join(command[1:]).removeprefix("-m ")
            assert needle in workflow, f"task {name!r}: {needle!r} is not in CI"


def test_scripts_index_lists_every_script_and_only_existing_ones():
    index = (ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    scripts = sorted(path.name for path in (ROOT / "scripts").glob("*.py"))
    missing = [name for name in scripts if name not in index]
    assert not missing, "add these scripts to scripts/README.md: " + ", ".join(missing)
    for name in set(re.findall(r"\[`([\w.]+\.py)`\]\(\1\)", index)):  # same-directory links only
        assert (ROOT / "scripts" / name).is_file(), f"scripts/README.md lists a missing script: {name}"
