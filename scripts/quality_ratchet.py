"""Quality ratchet: existing lint/type findings are tolerated, new ones are not.

For each tool the findings are counted per (file, rule) and compared with the
committed baseline ``scripts/quality_baseline.json``. A count may go down but not
up, and a (file, rule) pair that is not in the baseline must stay at zero. Line
numbers are deliberately ignored so unrelated edits do not disturb the check.

  python scripts/quality_ratchet.py check            # CI / pre-commit
  python scripts/quality_ratchet.py check --tool ruff
  python scripts/quality_ratchet.py update           # after fixing findings: lock the improvement in

Configuration lives in ``pyproject.toml`` under ``[tool.quality_ratchet]``.
The hard gate (`ruff check .` with the minimal rule set, mypy on the typed
contract files) is unchanged; this adds a wider net on top of it.
"""
from __future__ import annotations

import argparse
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


def run_mypy(config: dict) -> Counter:
    cmd = [sys.executable, "-m", "mypy", *config["paths"], "--no-error-summary", "--show-error-codes",
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


RUNNERS = {"ruff": run_ruff, "mypy": run_mypy}


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "update"])
    parser.add_argument("--tool", choices=sorted(RUNNERS), action="append",
                        help="limit to one tool (default: all configured)")
    args = parser.parse_args(argv)
    config = settings()
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
