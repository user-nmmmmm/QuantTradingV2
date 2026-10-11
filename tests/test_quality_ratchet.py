"""The quality ratchet only lets findings go down (infra 0.3)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import quality_ratchet as ratchet

ROOT = Path(__file__).resolve().parents[1]


def test_compare_flags_increases_and_new_pairs_but_not_improvements():
    worse, better = ratchet.compare(Counter({"a.py::F401": 3, "b.py::B023": 1, "c.py::E402": 2}),
                                    {"a.py::F401": 2, "c.py::E402": 5, "d.py::F811": 1})
    assert worse == ["a.py::F401: 2 -> 3", "b.py::B023: 0 -> 1"]
    assert better == ["c.py::E402: 5 -> 2", "d.py::F811: 1 -> 0"]


@pytest.mark.parametrize("line, expected", [
    ("core/x.py:12: error: Item has no attribute  [attr-defined]", ("core/x.py", "attr-defined")),
    ("core\\x.py:12:5: error: bad  [arg-type]", ("core\\x.py", "arg-type")),
])
def test_mypy_lines_are_parsed(line, expected):
    match = ratchet.MYPY_LINE.match(line)
    assert (match["path"], match["code"]) == expected


def test_notes_and_summaries_are_not_counted():
    for line in ("core/x.py:1: note: See docs", "Success: no issues found in 3 source files", "core/x.py:3: error: no code"):
        assert ratchet.MYPY_LINE.match(line) is None


@pytest.fixture()
def fake_tools(tmp_path: Path, monkeypatch):
    state = {"ruff": Counter({"a.py::F401": 2}), "mypy": Counter({"b.py::attr-defined": 4})}
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ratchet, "settings", lambda: {"ruff": {}, "mypy": {}})
    monkeypatch.setattr(ratchet, "RUNNERS", {"ruff": lambda config: Counter(state["ruff"]),
                                             "mypy": lambda config: Counter(state["mypy"])})
    return state


def test_check_update_cycle(fake_tools, capsys):
    assert ratchet.main(["update"]) == 0
    saved = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))["tools"]
    assert saved == {"ruff": {"a.py::F401": 2}, "mypy": {"b.py::attr-defined": 4}}
    assert ratchet.main(["check"]) == 0

    fake_tools["ruff"]["a.py::F401"] = 1                      # improvement: still passes, suggests locking in
    assert ratchet.main(["check"]) == 0
    assert "improved" in capsys.readouterr().out

    fake_tools["mypy"]["new.py::union-attr"] = 1              # a brand-new finding fails
    assert ratchet.main(["check"]) == 1
    assert "new.py::union-attr: 0 -> 1" in capsys.readouterr().out
    assert ratchet.main(["check", "--tool", "ruff"]) == 0     # other tools are unaffected when limited


def test_missing_baseline_means_everything_is_new(fake_tools):
    assert ratchet.main(["check"]) == 1


def test_repository_configuration_and_baseline_are_consistent():
    config = ratchet.settings()
    assert {"F", "B"} <= set(config["ruff"]["select"]) and "B905" in config["ruff"]["ignore"]
    assert "core" in config["mypy"]["paths"]
    baseline = json.loads((ROOT / "scripts" / "quality_baseline.json").read_text(encoding="utf-8"))
    assert baseline["schema"] == "quality-baseline/v1" and set(baseline["tools"]) == {"ruff", "mypy"}
    for tool in baseline["tools"].values():
        assert all(key.count("::") == 1 and count > 0 for key, count in tool.items())
