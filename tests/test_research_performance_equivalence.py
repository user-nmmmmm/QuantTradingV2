"""Skipping unused benchmarks must preserve real research results."""

from dataclasses import replace

import pandas as pd

from analysis import optimize, walk_forward
from backtest.engine import BacktestEngine
from tests.engine_baseline_harness import build_synthetic_data_map


def test_optimizer_without_benchmarks_matches_full_calculation(monkeypatch):
    data = build_synthetic_data_map(bars=180)
    task = (data, 20, 10, 10000.0)
    flags = []

    def full_engine(**kwargs):
        flags.append(kwargs["calculate_benchmarks"])
        return BacktestEngine(**{**kwargs, "calculate_benchmarks": True})

    with monkeypatch.context() as context:
        context.setattr(optimize, "BacktestEngine", full_engine)
        expected = optimize.evaluate_one_candidate(task)
    actual = optimize.evaluate_one_candidate(task)

    assert flags == [False]
    pd.testing.assert_series_equal(actual.pop("returns"), expected.pop("returns"), check_exact=True)
    assert actual == expected
    assert actual["Trades"] > 0


def test_walk_forward_default_and_explicit_benchmarks_are_equivalent(monkeypatch):
    data = build_synthetic_data_map(bars=180)
    timeline = walk_forward.common_timeline(data)
    settings = walk_forward.WalkForwardConfig(
        train_size=60, validation_size=30, test_size=30, warmup_period=30,
    )
    flags = []

    def observed_engine(**kwargs):
        flags.append(kwargs["calculate_benchmarks"])
        return BacktestEngine(**kwargs)

    monkeypatch.setattr(walk_forward, "BacktestEngine", observed_engine)
    arguments = dict(
        data_map=data,
        build_strategies=optimize._candidate_factory(20, 10),
        start=timeline[30], end=timeline[-1], warmup_start=timeline[0],
    )
    actual = walk_forward._run_window(**arguments, config=settings)
    expected = walk_forward._run_window(
        **arguments, config=replace(settings, engine_kwargs={"calculate_benchmarks": True}),
    )

    assert flags == [False, True]
    assert settings.engine_kwargs == {}
    assert actual["trades"] == expected["trades"] > 0
    pd.testing.assert_series_equal(actual["returns"], expected["returns"], check_exact=True)
