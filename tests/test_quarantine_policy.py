"""C4: flaky-test quarantine is bounded, and platform skips cannot hide failures on CI."""
from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest as hooks
from tests.capabilities import can_symlink

ROOT = Path(__file__).resolve().parents[1]
MAX_QUARANTINED = 10


def _quarantine_marks():
    found = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "quarantine"
                    and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "mark"):
                found.append((path.name, node.lineno, {k.arg: k.value for k in node.keywords}))
    return found


def test_quarantined_tests_have_a_reason_and_an_unexpired_deadline():
    marks = _quarantine_marks()
    assert len(marks) <= MAX_QUARANTINED, f"more than {MAX_QUARANTINED} tests are quarantined: fix some first"
    for name, line, keywords in marks:
        where = f"{name}:{line}"
        assert {"reason", "until"} <= set(keywords), f"{where}: quarantine needs reason= and until="
        reason, until = (ast.literal_eval(keywords[k]) for k in ("reason", "until"))
        assert isinstance(reason, str) and len(reason.strip()) >= 10, f"{where}: say why and where it was located"
        deadline = date.fromisoformat(until)
        assert deadline >= date.today(), f"{where}: quarantine expired on {until}; fix the test or remove the mark"


def test_the_quarantine_hook_turns_a_marked_test_into_a_non_strict_xfail():
    added = []
    mark = SimpleNamespace(kwargs={"reason": "races on slow disks", "until": "2099-01-01"})
    marked = SimpleNamespace(get_closest_marker=lambda name: mark if name == "quarantine" else None,
                             add_marker=added.append)
    plain = SimpleNamespace(get_closest_marker=lambda name: None, add_marker=added.append)
    hooks.pytest_collection_modifyitems([plain, marked])
    assert len(added) == 1
    assert added[0].name == "xfail" and added[0].kwargs["strict"] is False
    assert "2099-01-01" in added[0].kwargs["reason"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink privilege is only a Windows concern")
def test_symlinks_work_where_ci_runs_so_guarded_tests_are_never_skipped_there():
    assert can_symlink()
