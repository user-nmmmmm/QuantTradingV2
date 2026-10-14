"""Quality ratchet: existing lint/type findings are tolerated, new ones are not.

For each tool the findings are counted per (file, rule) and compared with the
committed baseline ``scripts/quality_baseline.json``. A count may go down but not
up, and a (file, rule) pair that is not in the baseline must stay at zero. Line
numbers are deliberately ignored so unrelated edits do not disturb the check.

  python scripts/quality_ratchet.py check            # CI / pre-commit
  python scripts/quality_ratchet.py check --tool ruff
  python scripts/quality_ratchet.py update           # after fixing findings: lock the improvement in
  python scripts/quality_ratchet.py gate             # only the mypy hard-gate modules (``check`` includes it)
  python scripts/quality_ratchet.py promote          # list modules that became clean and are not gated yet
  python scripts/quality_ratchet.py promote --write  # ... and add them to the gate list

Configuration lives in ``pyproject.toml`` under ``[tool.quality_ratchet]``.
The hard gate (`ruff check .` with the minimal rule set, mypy on the typed
contract files) is unchanged; this adds a wider net on top of it.

The mypy hard-gate list (``scripts/mypy_hard_gate.txt``, infrastructure roadmap C3) holds the modules whose
mypy findings reached zero. Unlike the baseline it cannot be loosened by ``update``: any finding in a listed
module fails ``check``/``gate``, and a module only leaves the list in a PR that says why. Findings come from the
same whole-package mypy run as the ratchet: with ``follow_imports = "skip"`` a module's result depends on which
other modules are on the command line, so a subset run would not agree with the baseline.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "quality_baseline.json"
GATE_LIST = ROOT / "scripts" / "mypy_hard_gate.txt"
MYPY_LINE = re.compile(r"^(?P<path>[^:\n]+):\d+(?::\d+)?: error: .*\[(?P<code>[a-z0-9-]+)\]\s*$")


def settings() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["quality_ratchet"]


def _rel(path: str) -> str:
    candidate = Path(path)
    try:
        candidate = candidate.resolve().relative_to(ROOT)
    except (ValueError, OSError):
        pass
    return candidate.as_posix()


def run_ruff(config: dict) -> Counter:
    cmd = [sys.executable, "-m", "ruff", "check", ".", "--output-format", "json", "--no-cache",
           "--select", ",".join(config["select"])]
    if config.get("ignore"):
        cmd += ["--ignore", ",".join(config["ignore"])]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        raise SystemExit(f"ruff failed:\n{result.stderr}")
    counts: Counter = Counter()
    for item in json.loads(result.stdout or "[]"):
        counts[f"{_rel(item['filename'])}::{item['code']}"] += 1
    return counts


def _mypy_findings(targets) -> Counter:
    """Findings per ``file::code`` for ``targets`` (paths relative to the repository root)."""
    cmd = [sys.executable, "-m", "mypy", *targets, "--no-error-summary", "--show-error-codes",
           "--no-pretty", "--cache-dir", str(ROOT / ".mypy_cache"),
           # Pin the analysis target so Windows/macOS dev machines and Linux CI agree on the counts.
           "--platform", "linux", "--python-version", "3.11"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        raise SystemExit(f"mypy failed:\n{result.stdout}\n{result.stderr}")
    counts: Counter = Counter()
    for line in result.stdout.splitlines():
        match = MYPY_LINE.match(line.strip())
        if match:
            counts[f"{_rel(match['path'])}::{match['code']}"] += 1
    return counts


def run_mypy(config: dict) -> Counter:
    return _mypy_findings(config["paths"])


def read_gate_list(path: Path | None = None) -> list[str]:
    path = path or GATE_LIST
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def write_gate_list(files, path: Path | None = None) -> None:
    path = path or GATE_LIST
    header = ("# Modules whose mypy findings are zero and must stay zero: `python scripts/quality_ratchet.py gate`.\n"
              "# Add with `python scripts/quality_ratchet.py promote --write`; remove only in a PR that says why.\n")
    path.write_text(header + "".join(f"{name}\n" for name in sorted(set(files))), encoding="utf-8", newline="\n")


def gate_problems(files: list[str], findings: Counter) -> list[str]:
    """Findings in the hard-gate modules (empty when the gate holds), plus listed files that no longer exist."""
    gated = set(files)
    problems = [f"{name}: listed in {GATE_LIST.name} but the file does not exist" for name in files
                if not (ROOT / name).is_file()]
    problems += [f"{key}: {count} finding(s)" for key, count in sorted(findings.items()) if key.split("::")[0] in gated]
    return problems


def clean_modules(config: dict, findings: Counter | None = None) -> list[str]:
    """Python files under the mypy paths without any finding."""
    findings = run_mypy(config) if findings is None else findings
    dirty = {key.split("::")[0] for key in findings}
    found = [p.relative_to(ROOT).as_posix() for folder in config["paths"] for p in sorted((ROOT / folder).rglob("*.py"))]
    return [name for name in found if name not in dirty]


def run_function_length(config: dict) -> Counter:
    """Line count of every function at or above ``limit`` lines, keyed ``file::Class.function`` (nested: ``a.b``)."""
    limit = int(config.get("limit", 100))
    counts: Counter = Counter()

    def visit(node, prefix: str, rel: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                length = child.end_lineno - child.lineno + 1
                if length >= limit:
                    counts[f"{rel}::{name}"] += length
                visit(child, name + ".", rel)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.", rel)
            else:
                visit(child, prefix, rel)

    for folder in config["paths"]:
        for path in sorted((ROOT / folder).rglob("*.py")):
            visit(ast.parse(path.read_text(encoding="utf-8")), "", path.relative_to(ROOT).as_posix())
    return counts


RUNNERS = {"ruff": run_ruff, "mypy": run_mypy, "function_length": run_function_length}


def load_baseline() -> Dict[str, Dict[str, int]]:
    if not BASELINE.is_file():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))["tools"]


def compare(current: Counter, baseline: Dict[str, int]) -> tuple[list[str], list[str]]:
    worse, better = [], []
    for key in sorted(set(current) | set(baseline)):
        now, before = current.get(key, 0), baseline.get(key, 0)
        if now > before:
            worse.append(f"{key}: {before} -> {now}")
        elif now < before:
            better.append(f"{key}: {before} -> {now}")
    return worse, better


def report_gate(findings: Counter) -> int:
    files = read_gate_list()
    problems = gate_problems(files, findings)
    for line in problems:
        print("ERROR [mypy gate] " + line)
    if problems:
        print("  These modules were clean and are hard-gated: fix the findings (a baseline update cannot excuse them).")
    else:
        print(f"[mypy gate] ok: {len(files)} modules without findings")
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "update", "gate", "promote"])
    parser.add_argument("--tool", choices=sorted(RUNNERS), action="append",
                        help="limit to one tool (default: all configured)")
    parser.add_argument("--write", action="store_true", help="with `promote`: add the clean modules to the gate list")
    args = parser.parse_args(argv)
    config = settings()
    if args.command == "gate":
        return report_gate(run_mypy(config["mypy"]))
    if args.command == "promote":
        gated = set(read_gate_list())
        fresh = [name for name in clean_modules(config["mypy"]) if name not in gated]
        print(f"{len(fresh)} clean module(s) not in the gate list" + (":" if fresh else ""))
        for name in fresh:
            print("  " + name)
        if args.write and fresh:
            write_gate_list(gated | set(fresh))
            print(f"added to {GATE_LIST.relative_to(ROOT).as_posix()}")
        return 0
    tools = args.tool or [name for name in RUNNERS if name in config]
    baseline = load_baseline()

    results = {name: RUNNERS[name](config[name]) for name in tools}
    if args.command == "update":
        for name, counts in results.items():
            baseline[name] = dict(sorted(counts.items()))
        BASELINE.write_text(json.dumps({"schema": "quality-baseline/v1", "tools": baseline}, indent=2,
                                       sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print("baseline updated: " + ", ".join(f"{n}={sum(c.values())}" for n, c in results.items()))
        return 0

    failed = False
    if "mypy" in results and report_gate(results["mypy"]):
        failed = True
    for name, counts in results.items():
        worse, better = compare(counts, baseline.get(name, {}))
        total, known = sum(counts.values()), sum(baseline.get(name, {}).values())
        if worse:
            failed = True
            print(f"ERROR [{name}] new findings above the baseline ({total} now, {known} allowed):")
            for line in worse[:60]:
                print("  " + line)
            if len(worse) > 60:
                print(f"  ... and {len(worse) - 60} more")
            print(f"  Fix them, or run `python scripts/{Path(__file__).name} check --tool {name}` locally to see details.")
        else:
            print(f"[{name}] ok: {total} findings, baseline allows {known}")
        if better:
            print(f"note [{name}]: {len(better)} entries improved; run `python scripts/{Path(__file__).name} update` to lock them in")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
