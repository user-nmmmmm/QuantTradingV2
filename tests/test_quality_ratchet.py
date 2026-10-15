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
    state = {"ruff": Counter({"a.py::F401": 2}), "mypy": Counter({"b.py::attr-defined": 4}),
             "function_length": Counter({"c.py::long": 150})}
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ratchet, "GATE_LIST", tmp_path / "gate.txt")
    monkeypatch.setattr(ratchet, "settings", lambda: {name: {} for name in state})
    monkeypatch.setattr(ratchet, "RUNNERS", {"ruff": lambda config: Counter(state["ruff"]),
                                             "mypy": lambda config: Counter(state["mypy"]),
                                             "function_length": lambda config: Counter(state["function_length"])})
    ratchet.BASELINE.write_text(json.dumps({"schema": "quality-baseline/v1", "tools": state}), encoding="utf-8")
    return state


def test_check_update_cycle(fake_tools, capsys):
    assert ratchet.main(["update"]) == 0
    saved = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))["tools"]
    assert saved == fake_tools
    assert ratchet.main(["check"]) == 0

    fake_tools["ruff"]["a.py::F401"] = 1                      # improvement: still passes, suggests locking in
    assert ratchet.main(["check"]) == 0
    assert "improved" in capsys.readouterr().out

    fake_tools["mypy"]["new.py::union-attr"] = 1              # a brand-new finding fails
    assert ratchet.main(["check"]) == 1
    assert "new.py::union-attr: 0 -> 1" in capsys.readouterr().out
    assert ratchet.main(["check", "--tool", "ruff"]) == 0     # other tools are unaffected when limited


@pytest.mark.parametrize("command", ["check", "update"])
def test_missing_baseline_means_everything_is_new(fake_tools, command):
    ratchet.BASELINE.unlink()
    assert ratchet.main([command]) == 1
    assert not ratchet.BASELINE.exists()


@pytest.mark.parametrize("tool", ["ruff", "mypy", "function_length"])
@pytest.mark.parametrize("limited", [False, True])
@pytest.mark.parametrize("new_pair", [False, True])
def test_update_rejects_per_entry_regressions_without_writing_any_tool(fake_tools, tool, limited, new_pair, capsys):
    before = ratchet.BASELINE.read_bytes()
    # Improvements elsewhere cannot pay for a new pair or growth in an existing pair.
    fake_tools["ruff"]["a.py::F401"] = 0
    fake_tools["mypy"]["b.py::attr-defined"] = 0
    fake_tools["function_length"]["c.py::long"] = 0
    old_key = {"ruff": "a.py::F401", "mypy": "b.py::attr-defined", "function_length": "c.py::long"}[tool]
    if new_pair:
        fake_tools[tool]["new.py::new"] = 1  # even a lower aggregate must fail
    else:
        fake_tools[tool][old_key] = {"ruff": 3, "mypy": 5, "function_length": 151}[tool]
    args = ["update"] + (["--tool", tool] if limited else [])
    assert ratchet.main(args) == 1
    assert ratchet.BASELINE.read_bytes() == before
    assert f"ERROR [{tool}]" in capsys.readouterr().out


def test_update_locks_decreases_and_removals_and_preserves_unselected_tools(fake_tools):
    before = ratchet.load_baseline()
    fake_tools["ruff"].clear()
    fake_tools["mypy"]["b.py::attr-defined"] = 3
    assert ratchet.main(["update", "--tool", "ruff"]) == 0
    saved = ratchet.load_baseline()
    assert saved["ruff"] == {}
    assert saved["mypy"] == before["mypy"] and saved["function_length"] == before["function_length"]
    assert ratchet.main(["update"]) == 0
    assert ratchet.load_baseline()["mypy"] == {"b.py::attr-defined": 3}


def test_tool_failure_cannot_partially_update_the_baseline(fake_tools, monkeypatch):
    before = ratchet.BASELINE.read_bytes()
    fake_tools["ruff"].clear()

    def broken(config):
        raise SystemExit("tool failed")

    monkeypatch.setitem(ratchet.RUNNERS, "mypy", broken)
    with pytest.raises(SystemExit, match="tool failed"):
        ratchet.main(["update"])
    assert ratchet.BASELINE.read_bytes() == before


def test_repository_configuration_and_baseline_are_consistent():
    config = ratchet.settings()
    assert {"F", "B"} <= set(config["ruff"]["select"]) and "B905" in config["ruff"]["ignore"]
    assert "core" in config["mypy"]["paths"]
    baseline = json.loads((ROOT / "scripts" / "quality_baseline.json").read_text(encoding="utf-8"))
    assert baseline["schema"] == "quality-baseline/v1" and set(baseline["tools"]) == {"ruff", "mypy", "function_length"}
    for tool in baseline["tools"].values():
        assert all(key.count("::") == 1 and count > 0 for key, count in tool.items())


# ---- C3: the mypy hard-gate list ------------------------------------------------------------------------------

@pytest.fixture()
def gate_list(tmp_path: Path, monkeypatch):
    path = tmp_path / "gate.txt"
    monkeypatch.setattr(ratchet, "GATE_LIST", path)
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)            # listed files are looked up relative to the root
    for name in ("a.py", "b.py", "z.py", "other.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    return path


def test_a_finding_in_a_gated_module_fails_check_even_with_a_baseline_that_allows_it(fake_tools, gate_list, capsys):
    gate_list.write_text("# header\nb.py\n", encoding="utf-8")
    before = ratchet.BASELINE.read_bytes()                    # simulate a pre-existing permissive baseline
    assert ratchet.main(["update"]) == 1
    assert ratchet.BASELINE.read_bytes() == before
    assert ratchet.main(["check"]) == 1                       # ... but b.py is gated, so it cannot be tolerated
    assert "ERROR [mypy gate] b.py::attr-defined: 4 finding(s)" in capsys.readouterr().out
    gate_list.write_text("other.py\n", encoding="utf-8")
    assert ratchet.main(["check"]) == 0


def test_gate_flags_a_listed_file_that_no_longer_exists(gate_list):
    problems = ratchet.gate_problems(["b.py", "core/no_such_module.py"], Counter())
    assert problems == [f"core/no_such_module.py: listed in {gate_list.name} but the file does not exist"]


def test_promote_adds_only_clean_modules_and_keeps_the_list_sorted(fake_tools, gate_list, monkeypatch, capsys):
    monkeypatch.setattr(ratchet, "clean_modules", lambda config: ["z.py", "a.py", "b.py"])
    gate_list.write_text("b.py\n", encoding="utf-8")
    assert ratchet.main(["promote"]) == 0 and ratchet.read_gate_list() == ["b.py"]      # dry run changes nothing
    assert ratchet.main(["promote", "--write"]) == 0
    assert ratchet.read_gate_list() == ["a.py", "b.py", "z.py"]
    assert "2 clean module(s)" in capsys.readouterr().out


def test_committed_gate_list_is_sorted_existing_and_disjoint_from_the_baseline():
    files = ratchet.read_gate_list()
    assert files and files == sorted(set(files))
    assert not [name for name in files if not (ROOT / name).is_file()]
    baseline = json.loads((ROOT / "scripts" / "quality_baseline.json").read_text(encoding="utf-8"))["tools"]["mypy"]
    assert not [key for key in baseline if key.split("::")[0] in set(files)]
    assert {"core/foundation/domain.py", "core/foundation/runtime.py", "live_trading/execution_adapter.py"} <= set(files)


@pytest.mark.slow
def test_no_clean_module_is_missing_from_the_gate_list():
    """A module that became clean must be gated so it cannot regress: `quality_ratchet.py promote --write`."""
    fresh = [name for name in ratchet.clean_modules(ratchet.settings()["mypy"]) if name not in set(ratchet.read_gate_list())]
    assert not fresh, "run `python scripts/quality_ratchet.py promote --write` and commit: " + ", ".join(fresh)


# ---- B8: function length ---------------------------------------------------------------------------------------

def _write(root: Path, name: str, body_lines: int) -> None:
    body = "".join(f"    x{n} = {n}\n" for n in range(body_lines))
    (root / name).write_text(f"def long():\n{body}", encoding="utf-8")


def test_function_length_counts_only_functions_at_or_above_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    (tmp_path / "pkg").mkdir()
    _write(tmp_path / "pkg", "a.py", 99)                      # def line + 99 statements = 100 lines: at the limit
    _write(tmp_path / "pkg", "b.py", 98)                      # 99 lines: below it
    (tmp_path / "pkg" / "c.py").write_text(
        "class K:\n    def method(self):\n" + "".join(f"        y{n} = {n}\n" for n in range(120)), encoding="utf-8")
    counts = ratchet.run_function_length({"limit": 100, "paths": ["pkg"]})
    assert counts == Counter({"pkg/a.py::long": 100, "pkg/c.py::K.method": 121})


def test_a_long_function_may_shrink_but_not_grow_and_a_new_one_fails():
    baseline = {"pkg/a.py::long": 150}
    assert ratchet.compare(Counter({"pkg/a.py::long": 150}), baseline) == ([], [])
    worse, better = ratchet.compare(Counter({"pkg/a.py::long": 160, "pkg/new.py::fresh": 101}), baseline)
    assert worse == ["pkg/a.py::long: 150 -> 160", "pkg/new.py::fresh: 0 -> 101"] and better == []
    assert ratchet.compare(Counter({"pkg/a.py::long": 120}), baseline)[1] == ["pkg/a.py::long: 150 -> 120"]
    assert ratchet.compare(Counter(), baseline)[1] == ["pkg/a.py::long: 150 -> 0"]    # shrunk below the limit


def test_the_engine_and_the_command_line_have_no_function_over_the_limit_in_the_baseline():
    baseline = json.loads((ROOT / "scripts" / "quality_baseline.json").read_text(encoding="utf-8"))["tools"]["function_length"]
    assert not [key for key in baseline if key.split("::")[0] in {"backtest/engine.py", "cli/run.py", "cli/data.py", "cli/replay.py"}]
