"""Run an explicit ML engineering test suite with all real fitting disabled.

This does not run the complete ML suite. Modules containing genuine synthetic
model fitting are deliberately absent; select specific pure/mock nodes when
additional orchestration coverage is needed. The guard fails an accidental
training call rather than skipping the test or fabricating a fitted result.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]

# Reviewed modules: data math, original engine behavior, mock orchestration and
# frozen inference. Keep this explicit; a wildcard can silently include fitting.
DEFAULT_TESTS = (
    "tests/test_ml_selection_dataset.py",
    "tests/test_ml_selection_inference.py",
    "tests/test_ml_selection_integrity.py",
    "tests/test_ml_selection_environment.py",
    "tests/test_ml_selection_diagnostics.py",
    "tests/test_ml_selection_batch_comparison.py",
    "tests/test_ml_selection_next_round.py",
    "tests/test_ml_selection_next_forward.py",
    "tests/test_ml_selection_bridge_audit.py",
    "tests/test_ml_selection_stress_failures.py",
    "tests/test_coin_selector_cli.py",
    "tests/test_selector_baseline_backtest.py",
    "tests/test_selector_execution_entrypoints.py",
    "tests/test_ml_selector_execution_contract.py",
    "tests/test_ml_policy_context.py",
    "tests/test_ml_selection_evaluation_contract.py",
    "tests/test_ml_selection_candidate_dataset.py",
    "tests/test_ml_selection_readiness.py",
    "tests/test_ml_selection_comparison_plan.py",
    "tests/test_ml_code_training_guard.py",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Explicit pytest files or node IDs; replaces defaults")
    parser.add_argument("--list-tests", action="store_true", help="Print the default manifest and exit")
    parser.add_argument("--pytest-arg", action="append", default=[],
                        help="Additional pytest argument; use --pytest-arg=-q for flags")
    args = parser.parse_args(argv)
    if args.list_tests:
        print("\n".join(DEFAULT_TESTS))
        return 0
    selected = tuple(args.tests) or DEFAULT_TESTS
    for node in selected:
        path = (ROOT / node.split("::", 1)[0]).resolve()
        if not path.is_file() or not path.is_relative_to(ROOT / "tests"):
            parser.error(f"Expected an existing test file inside tests/: {node}")

    import pytest
    from scripts.ml_training_guard import NoTrainingGuard

    print(f"Running {len(selected)} explicit test targets; real ML fitting is disabled.", flush=True)
    previous = Path.cwd()
    try:
        os.chdir(ROOT)
        return int(pytest.main(["-p", "no:cacheprovider", *selected, *args.pytest_arg],
                               plugins=[NoTrainingGuard()]))
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
