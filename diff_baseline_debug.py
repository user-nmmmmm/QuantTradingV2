"""Throwaway diagnostic: structurally diff a fresh engine run against the
committed baseline fixture, to find exactly which field first diverges.

Usage (run from repo root, same Python env used to run the test suite):
    python diff_baseline_debug.py

Not part of the test suite / not meant to be committed.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.engine_baseline_harness import (
    build_synthetic_data_map,
    run_engine,
)

FIXTURE_PATH = Path(__file__).parent / "tests" / "fixtures" / "backtest" / "engine" / "engine_baseline_v1.json"


def first_divergence(path, a, b):
    """Yield (json_path, a_value, b_value) for the first leaf mismatch."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            yield path + ".<keys>", sorted(a), sorted(b)
            return
        for key in sorted(a):
            yield from first_divergence(f"{path}.{key}", a[key], b[key])
            return  # only need the first mismatch, stop as soon as one is found
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield path + ".<len>", len(a), len(b)
            return
        for i, (av, bv) in enumerate(zip(a, b)):
            found = list(first_divergence(f"{path}[{i}]", av, bv))
            if found:
                yield from found
                return
    else:
        if a != b:
            # For floats, show the raw repr so we can see ULP-level drift.
            yield path, repr(a), repr(b)


def walk_all_divergences(path, a, b, limit=15, found=None):
    if found is None:
        found = []
    if len(found) >= limit:
        return found
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            found.append((path + ".<keys>", sorted(a), sorted(b)))
            return found
        for key in sorted(a):
            walk_all_divergences(f"{path}.{key}", a[key], b[key], limit, found)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            found.append((path + ".<len>", len(a), len(b)))
            return found
        for i, (av, bv) in enumerate(zip(a, b)):
            walk_all_divergences(f"{path}[{i}]", av, bv, limit, found)
    else:
        if a != b:
            found.append((path, repr(a), repr(b)))
    return found


def main() -> None:
    with FIXTURE_PATH.open(encoding="utf-8") as stream:
        bundle = json.load(stream)

    baseline_artifacts = bundle["artifacts"]

    data_map = build_synthetic_data_map(
        seed=bundle["metadata"]["seed"],
        symbols=bundle["metadata"]["symbols"],
        bars=bundle["metadata"]["bars_per_symbol"],
    )
    fresh_artifacts = run_engine(data_map, warmup_period=bundle["metadata"]["warmup_period"])

    if json.dumps(fresh_artifacts, sort_keys=True) == json.dumps(baseline_artifacts, sort_keys=True):
        print("MATCH: fresh run is identical to the committed baseline on this machine.")
        return

    print("MISMATCH found. First divergences (path, baseline_value, fresh_value):\n")
    diffs = walk_all_divergences("artifacts", baseline_artifacts, fresh_artifacts, limit=15)
    for i, (path, a_val, b_val) in enumerate(diffs, 1):
        print(f"{i}. {path}")
        print(f"   baseline: {a_val}")
        print(f"   fresh   : {b_val}")
        print()


if __name__ == "__main__":
    main()
