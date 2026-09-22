"""Compare optimized arithmetic with the frozen pre-optimization pandas loop."""
from __future__ import annotations

from typing import Mapping, Optional

import numpy as np
import pandas as pd
import pytest

from core.benchmarks import BenchmarkResult, dynamic_equal_weight_rebalanced


def _close_matrix(data_map: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    closes = pd.DataFrame({
        symbol: pd.to_numeric(frame["close"], errors="coerce")
        for symbol, frame in sorted(data_map.items())
        if frame is not None and not frame.empty and "close" in frame
    }).sort_index()
    return closes.where(closes > 0)


def _pandas_reference(
    data_map: Mapping[str, pd.DataFrame],
    initial_capital: float,
    *,
    start_idx: int = 0,
    cost_bps: float = 0.0,
) -> Optional[BenchmarkResult]:
    """Rebalance equally across assets with an actual bar at each timestamp.

    Turnover is one half of the absolute weight change, and transaction cost is
    charged on traded notional.  Both weights and costs are returned for audit.
    """

    if cost_bps < 0:
        raise ValueError("cost_bps cannot be negative")
    closes = _close_matrix(data_map)
    if closes.empty:
        return None
    start_idx = min(max(int(start_idx), 0), len(closes) - 1)
    index = closes.index
    weights = pd.DataFrame(0.0, index=index, columns=closes.columns)
    turnover = pd.Series(0.0, index=index, name="turnover")
    costs = pd.Series(0.0, index=index, name="cost")
    equity = pd.Series(float(initial_capital), index=index, name="dynamic_equal_weight")

    previous_weights = pd.Series(0.0, index=closes.columns)
    current_equity = float(initial_capital)
    previous_prices: Optional[pd.Series] = None
    for position, timestamp in enumerate(index):
        prices = closes.loc[timestamp]
        if position < start_idx:
            previous_prices = prices.combine_first(previous_prices) if previous_prices is not None else prices
            equity.iloc[position] = current_equity
            continue

        if previous_prices is not None:
            common = previous_prices.notna() & prices.notna() & (previous_prices > 0)
            asset_returns = pd.Series(0.0, index=closes.columns)
            asset_returns.loc[common] = prices.loc[common] / previous_prices.loc[common] - 1.0
            growth = 1.0 + float((previous_weights * asset_returns).sum())
            current_equity *= growth
            drifted_weights = previous_weights * (1.0 + asset_returns) / growth
        else:
            drifted_weights = previous_weights

        active = prices.dropna().index
        target = pd.Series(0.0, index=closes.columns)
        if len(active):
            target.loc[active] = 1.0 / len(active)
        # Moving from cash into the initial portfolio trades 100% of capital;
        # subsequent asset-to-asset rebalances use one-way turnover (half the
        # sum of absolute weight changes, avoiding double-counting buy+sell).
        step_turnover = 0.5 * (float((target - drifted_weights).abs().sum())
                               + abs(float(target.sum() - drifted_weights.sum())))
        step_cost = current_equity * step_turnover * cost_bps / 10000.0
        current_equity -= step_cost

        weights.loc[timestamp] = target
        turnover.loc[timestamp] = step_turnover
        costs.loc[timestamp] = step_cost
        equity.loc[timestamp] = current_equity
        previous_weights = target
        previous_prices = prices

    return BenchmarkResult(
        equity=equity,
        weights=weights,
        turnover=turnover,
        costs=costs,
        metadata={
            "benchmark_id": "equal_weight_event_rebalanced/v2",
            "name": "dynamic_equal_weight_rebalanced",
            "start_time": index[start_idx],
            "asset_join_rule": "assets with a valid close on each rebalance timestamp",
            "rebalance_rule": "every event timestamp",
            "turnover_formula": "0.5 * sum(abs(target_weight - drifted_weight)) including cash sleeve",
            "cost_bps": float(cost_bps),
        },
    )


def _assert_equivalent(data, *, start_idx=0, cost_bps=0.0, initial_capital=10_000.0):
    original = {symbol: frame.copy(deep=True) if frame is not None else None
                for symbol, frame in data.items()}
    arguments = dict(start_idx=start_idx, cost_bps=cost_bps)
    expected = _pandas_reference(data, initial_capital, **arguments)
    actual = dynamic_equal_weight_rebalanced(data, initial_capital, **arguments)
    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        for name in ("equity", "turnover", "costs"):
            pd.testing.assert_series_equal(getattr(actual, name), getattr(expected, name),
                                           check_exact=True)
        pd.testing.assert_frame_equal(actual.weights, expected.weights, check_exact=True)
        assert actual.metadata == expected.metadata
        assert actual.equity.attrs == expected.equity.attrs
    for symbol, frame in data.items():
        if frame is not None:
            pd.testing.assert_frame_equal(frame, original[symbol], check_exact=True)
    return actual


@pytest.mark.parametrize("start_idx", [-4, 0, 1, 9, 1000])
@pytest.mark.parametrize("cost_bps", [0.0, 12.5])
def test_dynamic_benchmark_matches_pandas_for_sparse_prices(start_idx, cost_bps):
    rng = np.random.default_rng(20260922)
    dates = pd.date_range("2024-01-01", periods=48, freq="4h", tz="UTC", name="event")
    data = {}
    for number in range(7):
        close = 100 * np.exp(np.cumsum(rng.normal(0, .05, len(dates))))
        close[rng.random(len(dates)) < .2] = np.nan
        frame = pd.DataFrame({"close": close}, index=dates)
        data[f"asset_{number}"] = frame.loc[rng.random(len(dates)) > .15].iloc[::-1]
    data["ignored"] = pd.DataFrame({"open": [100.]}, index=dates[:1])
    data["empty"] = pd.DataFrame({"close": []})
    data["none"] = None
    _assert_equivalent(data, start_idx=start_idx, cost_bps=cost_bps)


@pytest.mark.parametrize("dtype", ["float64", "float32", "Float64", "Float32", "Int64"])
@pytest.mark.parametrize("start_idx", [0, 3])
def test_dynamic_benchmark_preserves_numeric_input_precision(dtype, start_idx):
    dates = pd.date_range("2024-01-01", periods=7)
    data = {"B": pd.DataFrame({"close": pd.Series([41, 47, None, 29, 17, 31, 79],
                                                    index=dates, dtype=dtype)}),
            "A": pd.DataFrame({"close": pd.Series([103, None, 107, 113, 101, 109, 103],
                                                    index=dates, dtype=dtype)})}
    _assert_equivalent(data, start_idx=start_idx, cost_bps=8.0)


@pytest.mark.parametrize("start_idx", [0, 3])
def test_dynamic_benchmark_matches_mixed_numeric_columns(start_idx):
    dates = pd.date_range("2024-01-01", periods=6)
    values = [None, 103, 107, None, 101, 109]
    data = {dtype: pd.DataFrame({"close": pd.Series(values, index=dates, dtype=dtype)})
            for dtype in ("Int64", "Float32", "float64", "object")}
    data["strings"] = pd.DataFrame({"close": ["102.25", "invalid", "104.5", "-1", "106", None]},
                                  index=dates)
    _assert_equivalent(data, start_idx=start_idx, cost_bps=7.5)


@pytest.mark.parametrize("start_idx", [0, 3, 6])
def test_dynamic_benchmark_cash_warmup_and_missing_price_policy(start_idx):
    dates = pd.date_range("2024-01-01", periods=9, tz="Asia/Singapore")
    data = {"A": pd.DataFrame({"close": [100, None, None, 110, None, None, 120, 130, 140]},
                               index=dates),
            "LATE": pd.DataFrame({"close": [None, None, None, None, 40, None, 80, 90, 100]},
                                  index=dates)}
    actual = _assert_equivalent(data, start_idx=start_idx, cost_bps=10.0)
    assert actual.weights.iloc[:start_idx].eq(0.0).all().all()
    assert actual.turnover.iloc[5] == (1.0 if start_idx <= 4 else 0.0)


@pytest.mark.parametrize("start_idx", [0, 1, 4])
@pytest.mark.parametrize("cost_bps", [0.0, 10.0])
def test_dynamic_benchmark_preserves_nonfinite_skipna_semantics(start_idx, cost_bps):
    dates = pd.date_range("2024-01-01", periods=8)
    data = {"A": pd.DataFrame({"close": [100, np.inf, np.inf, 100, 1e-300, 1e300, 0, -1]},
                               index=dates),
            "B": pd.DataFrame({"close": [100, 90, np.nan, np.inf, 1e300, 1e-300, 0, -np.inf]},
                               index=dates)}
    _assert_equivalent(data, start_idx=start_idx, cost_bps=cost_bps)


@pytest.mark.parametrize("initial_capital", [0.0, -100.0, float("nan"), float("inf")])
def test_dynamic_benchmark_keeps_existing_capital_semantics(initial_capital):
    data = {"A": pd.DataFrame({"close": [100., 110.]})}
    _assert_equivalent(data, initial_capital=initial_capital, cost_bps=5.0)


def test_dynamic_benchmark_empty_invalid_and_single_bar_inputs():
    _assert_equivalent({})
    _assert_equivalent({"A": pd.DataFrame({"open": [100.]})})
    _assert_equivalent({"A": pd.DataFrame({"close": ["invalid", 0, -10, None]})})
    _assert_equivalent({"A": pd.DataFrame({"close": ["123.45"]})}, start_idx=100)
    with pytest.raises(ValueError, match="cost_bps cannot be negative"):
        dynamic_equal_weight_rebalanced({}, 1000., cost_bps=-1)
