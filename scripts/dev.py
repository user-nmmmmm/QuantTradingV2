"""One entry point for the checks contributors and CI run.

  python scripts/dev.py list              # show tasks
  python scripts/dev.py check             # everything CI's `quality` job does, in order (keeps going, prints a summary)
  python scripts/dev.py check --no-tests  # same without the long test run
  python scripts/dev.py lint types imports ratchet
  python scripts/dev.py test -- -k ledger # extra arguments after `--` go to pytest
  python scripts/dev.py fast              # tests without the `slow` marker
  python scripts/dev.py setup             # install dev dependencies and the project (editable)

Also installed as the console script ``quant-dev``. Works the same on Windows, macOS and Linux
(no ``make`` needed): every task is a plain command run with the current interpreter.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TYPED_FILES = ["core/foundation/domain.py", "core/foundation/runtime.py", "live_trading/execution_adapter.py"]


def _tool(name: str) -> List[str]:
    """Locate a console script installed next to the interpreter (works without activating the venv)."""
    for folder in (Path(PY).parent, Path(PY).parent / "Scripts", Path(PY).parent / "bin"):
        for candidate in (folder / name, folder / f"{name}.exe"):
            if candidate.is_file():
                return [str(candidate)]
    found = shutil.which(name)
    return [found] if found else [name]


def _pytest(extra: Sequence[str] = (), *, parallel: bool = True, fast: bool = False) -> List[str]:
    cmd = [PY, "-m", "pytest", "-q"]
    if parallel and importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", "4"]
    if fast:
        cmd += ["-m", "not slow"]
    return cmd + list(extra)


@dataclass(frozen=True)
class Task:
    name: str
    description: str
    build: Callable[[Sequence[str]], List[List[str]]]   # extra args -> commands to run in order
    in_check: bool = True
    heavy: bool = False                                  # skipped by `check --no-tests`


def _simple(*command: str) -> Callable[[Sequence[str]], List[List[str]]]:
    return lambda extra: [list(command)]


TASKS: Dict[str, Task] = {t.name: t for t in [
    Task("env", "dependency/environment self-check and runtime lock integrity",
         lambda extra: [[PY, "scripts/check_environment.py"], [PY, "scripts/verify_lock.py"]]),
    Task("lint", "ruff hard gate (syntax / undefined-name level)", _simple(PY, "-m", "ruff", "check", ".")),
    Task("ratchet", "no new ruff/mypy findings above the baseline", _simple(PY, "scripts/quality_ratchet.py", "check")),
    Task("imports", "import direction (layered architecture)", lambda extra: [_tool("lint-imports")]),
    Task("types", "mypy on the typed contract files", _simple(PY, "-m", "mypy", *TYPED_FILES)),
    Task("hygiene", "tracked files and frozen artifacts", _simple(PY, "scripts/check_repository_hygiene.py")),
    Task("archive", "protected historical baseline archive", _simple(PY, "scripts/verify_baseline_archive.py")),
    Task("links", "Markdown relative links", _simple(PY, "scripts/check_markdown_links.py")),
    Task("roadmap", "roadmap completion structure", _simple(PY, "scripts/verify_roadmap_completion.py", "--structure-only")),
    Task("test", "full test suite (parallel when pytest-xdist is installed); extra args go to pytest",
         lambda extra: [_pytest(extra)], heavy=True),
    Task("fast", "tests without the `slow` marker", lambda extra: [_pytest(extra, fast=True)], in_check=False),
    Task("identity", "frozen-scope identity impact versus origin/main", lambda extra:
         [[PY, "scripts/identity_report.py", "--base", "origin/main", "--head", "WORKTREE"]], in_check=False),
    Task("setup", "install dev dependencies and the project (editable)", lambda extra: [
        [PY, "-m", "pip", "install", "-r", "requirements-dev.txt"],
        [PY, "-m", "pip", "install", "-e", ".", "--no-deps"]], in_check=False),
]}

CHECK_ORDER = ["env", "lint", "ratchet", "imports", "types", "hygiene", "archive", "links", "roadmap", "test"]


def plan(names: Sequence[str], extra: Sequence[str] = (), *, no_tests: bool = False) -> List[tuple]:
    """Resolve task names into (task, commands) without running anything."""
    if list(names) == ["check"]:
        names = [n for n in CHECK_ORDER if not (no_tests and TASKS[n].heavy)]
    unknown = [n for n in names if n not in TASKS]
    if unknown:
        raise SystemExit(f"unknown task(s): {', '.join(unknown)}; try `list`")
    return [(TASKS[n], TASKS[n].build(extra)) for n in names]


def run(steps: Sequence[tuple], cwd: Path = ROOT) -> Dict[str, Optional[int]]:
    results: Dict[str, Optional[int]] = {}
    timings: Dict[str, float] = {}
    for task, commands in steps:
        print(f"\n=== {task.name}: {task.description}", flush=True)
        started, code = time.monotonic(), 0
        for command in commands:
            print("$ " + " ".join(command), flush=True)
            try:
                code = subprocess.run(command, cwd=cwd).returncode
            except FileNotFoundError:
                print(f"command not found: {command[0]} (run `python scripts/dev.py setup`)")
                code = 127
            if code:
                break
        results[task.name], timings[task.name] = code, time.monotonic() - started
    print("\n--- summary")
    for name, code in results.items():
        print(f"{'ok  ' if code == 0 else 'FAIL'} {name:<9} {timings[name]:6.1f}s")
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    extra: List[str] = []
    if "--" in args:
        split = args.index("--")
        args, extra = args[:split], args[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tasks", nargs="*", help="task names, `check` for CI parity, or `list`")
    parser.add_argument("--no-tests", action="store_true", help="with `check`: skip the long test run")
    options = parser.parse_args(args)
    if not options.tasks or options.tasks == ["list"]:
        width = max(map(len, TASKS))
        for task in TASKS.values():
            print(f"{task.name:<{width}}  {task.description}")
        print(f"{'check':<{width}}  all of: {', '.join(CHECK_ORDER)}")
        return 0
    results = run(plan(options.tasks, extra, no_tests=options.no_tests))
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
