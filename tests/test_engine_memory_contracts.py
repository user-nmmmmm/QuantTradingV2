"""Compact engine bookkeeping keeps the same economic observations."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.metrics import calculate_exposure
from tests.engine_baseline_harness import build_synthetic_data_map


@pytest.mark.parametrize("equity", [1000.0, 0.0, -100.0, np.nan])
@pytest.mark.parametrize("missing_price", [None, np.nan, 0.0, -3.0])
def test_scalar_exposure_matches_existing_metric(equity, missing_price):
    timestamp = pd.Timestamp("2024-01-01")
    positions = {"long": {"qty": 2.0}, "short": {"qty": -3.0},
                 "flat": {"qty": 0.0}, "unpriced": {"qty": 5.0},
                 "optional": {"qty": 7.0}}
    portfolio = SimpleNamespace(positions=positions, get_position=positions.__getitem__)
    prices = {"long": 10.0, "short": 11.0, "flat": 500.0, "optional": missing_price}
    row = {"timestamp": timestamp, "equity": equity, "cash": 900.0}
    expected = calculate_exposure(
        {timestamp: {key: value["qty"] for key, value in positions.items()}},
        {timestamp: prices}, {timestamp: equity},
    )

    BacktestEngine._sample_exposure(portfolio, prices, row)
    actual = BacktestEngine._equity_frame([row])
    pd.testing.assert_frame_equal(actual[expected.columns], expected, check_exact=True)
    # Later position/price changes cannot rewrite an earlier equity sample.
    positions["long"]["qty"] = 999.0
    prices["short"] = 999.0
    pd.testing.assert_frame_equal(
        BacktestEngine._equity_frame([row])[expected.columns], expected, check_exact=True,
    )


def test_forced_closes_reuse_supplied_positions(monkeypatch):
    single_timestamp_lookups = []
    original = pd.DatetimeIndex.get_indexer

    def observe(index, target, *args, **kwargs):
        if isinstance(target, list) and len(target) == 1 and isinstance(target[0], pd.Timestamp):
            single_timestamp_lookups.append(target[0])
        return original(index, target, *args, **kwargs)

    data = build_synthetic_data_map(bars=240, symbols=[f"ASSET{i}/USDT" for i in range(10)])
    monkeypatch.setattr(pd.DatetimeIndex, "get_indexer", observe)
    result = BacktestEngine(run_id="forced-position-regression").run(data, routing_log_enabled=False)

    assert any(trade.get("exit_reason") == "DrawdownBudgetReduce" for trade in result["trades"])
    assert result["accounting_check"]["ok"]
    assert single_timestamp_lookups == []
