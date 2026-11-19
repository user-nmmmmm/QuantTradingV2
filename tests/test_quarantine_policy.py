"""C4: flaky-test quarantine is bounded, and platform skips cannot hide failures on CI."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest as hooks
from tests.capabilities import can_symlink

@pytest.fixture()
def run_collection(tmp_path):
    """Exercise the real hook in an isolated pytest process, including parameter expansion."""
    (tmp_path / "conftest.py").write_text(Path(hooks.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --strict-markers\nmarkers =\n    quarantine: bounded flaky-test marker\n", encoding="utf-8")

    def run(source, *args, relative="test_probe.py", collect_only=True):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        command = [sys.executable, "-m", "pytest", "-q", "-c", str(tmp_path / "pytest.ini"),
                   "--confcutdir", str(tmp_path)]
        if collect_only:
            command.append("--collect-only")
        return subprocess.run(command + list(args), cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)

    return run


def _cases(form, count=11, days=7):
    header = ("import pytest\nfrom datetime import datetime, timedelta, timezone\n"
              f"until = (datetime.now(timezone.utc).date() + timedelta(days={days})).isoformat()\n"
              "mark = pytest.mark.quarantine(reason='races on slow disks', until=until)\n")
    if form == "class":
        return header + (f"@mark\nclass TestGroup:\n    @pytest.mark.parametrize('value', range({count}))\n"
                         "    def test_case(self, value):\n        assert value >= 0\n")
    if form == "module":
        return header + (f"pytestmark = mark\n@pytest.mark.parametrize('value', range({count}))\n"
                         "def test_case(value):\n    assert value >= 0\n")
    if form == "parameter":
        return header + (f"@pytest.mark.parametrize('value', [pytest.param(n, marks=mark) for n in range({count})])\n"
                         "def test_case(value):\n    assert value >= 0\n")
    return header + (f"@mark\n@pytest.mark.parametrize('value', range({count}))\n"
                     "def test_case(value):\n    assert value >= 0\n")


@pytest.mark.parametrize("form", ["class", "module", "parameter", "function"])
@pytest.mark.parametrize("count", [10, 11])
def test_quarantine_limit_counts_real_cases_including_subdirectories(run_collection, form, count):
    result = run_collection(_cases(form, count), relative="nested/test_probe.py")
    if count == 10:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode == 4, result.stdout + result.stderr
        assert "11 quarantined test cases" in result.stderr


@pytest.mark.parametrize("selection", [["-k", "case[0]"], ["-m", "not quarantine"],
                                       ["--deselect", "nested/test_probe.py"]])
def test_deselection_cannot_hide_an_over_limit_collection(run_collection, selection):
    result = run_collection(_cases("class"), *selection, relative="nested/test_probe.py")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "11 quarantined test cases" in result.stderr


@pytest.mark.parametrize("days, valid", [(-1, False), (0, True), (30, True), (31, False)])
def test_deadline_window_is_inclusive_and_at_most_thirty_utc_days(run_collection, days, valid):
    result = run_collection(_cases("function", count=1, days=days))
    assert result.returncode == (0 if valid else 4), result.stdout + result.stderr


@pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="pytest-xdist is not installed locally")
@pytest.mark.parametrize("count, days, message", [(10, 7, None), (11, 7, "11 quarantined test cases"),
                                                 (1, -1, "expired"), (1, 31, "at most 30 days")])
def test_parallel_collection_enforces_the_whole_suite_limit(run_collection, count, days, message):
    result = run_collection(_cases("class", count=count, days=days), "-p", "xdist.plugin", "-n", "2", collect_only=False)
    output = result.stdout + result.stderr
    assert "INTERNALERROR" not in output, output
    if message is None:
        assert result.returncode == 0, output
        assert "10 xpassed" in output, output
    else:
        assert result.returncode in {1, 2, 4}, output
        assert message in output, output


@pytest.mark.parametrize("kwargs, message", [
    ({"reason": "too short"}, "reason"),
    ({"reason": None}, "reason"),
    ({"until": "2099-01-01"}, "at most 30 days"),
    ({"until": "2026-02-30"}, "YYYY-MM-DD"),
    ({"until": 20261015}, "YYYY-MM-DD"),
    ({"until": None}, "YYYY-MM-DD"),
])
def test_invalid_markers_fail_collection_before_xfail_can_hide_them(kwargs, message):
    values = {"reason": "races on slow disks", "until": datetime.now(timezone.utc).date().isoformat()} | kwargs
    added = []
    marker = SimpleNamespace(kwargs=values)
    item = SimpleNamespace(nodeid="test_probe.py::test_case", iter_markers=lambda name: [marker],
                           get_closest_marker=lambda name: marker, add_marker=added.append)
    with pytest.raises(pytest.UsageError, match=message):
        session = SimpleNamespace(shouldfail=False)
        hooks.pytest_collection_modifyitems([item], session)
    assert message in session.shouldfail
    assert added == []


def test_invalid_inherited_marker_cannot_be_hidden_by_a_valid_function_marker(run_collection):
    source = _cases("function", count=1) + "pytestmark = pytest.mark.quarantine(reason='races on slow disks', until='2099-01-01')\n"
    result = run_collection(source)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "at most 30 days" in result.stderr


def test_multiple_valid_markers_count_each_case_once(run_collection):
    result = run_collection(_cases("function", count=10) + "pytestmark = mark\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_quarantine_hook_turns_a_marked_test_into_a_non_strict_xfail():
    added = []
    until = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
    mark = SimpleNamespace(kwargs={"reason": "races on slow disks", "until": until})
    marked = SimpleNamespace(get_closest_marker=lambda name: mark if name == "quarantine" else None,
                             nodeid="test_probe.py::test_marked", iter_markers=lambda name: [mark],
                             add_marker=added.append)
    plain = SimpleNamespace(get_closest_marker=lambda name: None, iter_markers=lambda name: [], add_marker=added.append)
    hooks.pytest_collection_modifyitems([plain, marked], SimpleNamespace(shouldfail=False))
    assert len(added) == 1
    assert added[0].name == "xfail" and added[0].kwargs["strict"] is False
    assert until in added[0].kwargs["reason"]


def test_valid_quarantine_keeps_running_failures_and_passes(run_collection):
    source = _cases("function", count=1) + ("@mark\ndef test_fails():\n    assert False\n"
                                           "def test_plain():\n    assert True\n")
    result = run_collection(source, collect_only=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 xfailed, 1 xpassed" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="symlink privilege is only a Windows concern")
def test_symlinks_work_where_ci_runs_so_guarded_tests_are_never_skipped_there():
    assert can_symlink()
