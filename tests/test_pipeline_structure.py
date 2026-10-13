"""B5 (ADR 0019): the backtest run and the command line are pipelines of small stages."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE_LIMIT = 100   # exclusive


def _functions(path: str, owner: str | None = None) -> dict[str, int]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    scope = tree
    if owner:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner)
    return {n.name: n.end_lineno - n.lineno + 1 for n in scope.body if isinstance(n, ast.FunctionDef)}


def test_main_py_is_a_thin_entry_point():
    lines = (ROOT / "main.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 200
    assert any("from cli.run import main" in line for line in lines)


def test_backtest_engine_run_is_a_driver_over_small_stages():
    sizes = _functions("backtest/engine.py", "BacktestEngine")
    assert sizes["run"] < STAGE_LIMIT
    stages = {name: size for name, size in sizes.items()
              if name.startswith(("_prepare_", "_bar_", "_finish_", "_result_"))}
    assert len(stages) >= 12, stages
    assert all(size < STAGE_LIMIT for size in stages.values()), stages


def test_cli_main_is_a_driver_over_small_stages():
    sizes = _functions("cli/run.py")
    assert sizes["main"] < STAGE_LIMIT
    stages = {name: size for name, size in sizes.items()
              if name in {"_parse_arguments", "_validate_options", "_prepare_run", "_run_backtest", "_build_report",
                          "_describe_run", "_write_artifacts", "_collect_outputs", "_write_final_artifacts",
                          "_print_summary"}}
    assert len(stages) == 10
    assert all(size < STAGE_LIMIT for size in stages.values()), stages


@pytest.mark.parametrize("module", ["cli.run", "cli.replay"])
def test_cli_modules_locate_the_repository_root_from_their_own_depth(module):
    import importlib
    loaded = importlib.import_module(module)
    assert loaded.ROOT == ROOT
    assert (loaded.ROOT / "config" / "params.yaml").is_file()


def test_run_state_rejects_typos():
    from backtest.engine import _RunState
    state = _RunState()
    state.portfolio = object()
    with pytest.raises(AttributeError):
        state.portfoilo = object()    # a misspelt field must fail, not silently create a new one
