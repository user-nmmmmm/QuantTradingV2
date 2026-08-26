"""Auditable fixed and dynamic equal-weight benchmark implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional

import pandas as pd


@dataclass(frozen=True)
class BenchmarkResult:
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    metadata: Dict[str, object]


def _close_matrix(data_map: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    closes = pd.DataFrame({
        symbol: pd.to_numeric(frame["close"], errors="coerce")
        for symbol, frame in sorted(data_map.items())
        if frame is not None and not frame.empty and "close" in frame
    }).sort_index()
    return closes.where(closes > 0)


def fixed_equal_weight_buy_hold(
    data_map: Mapping[str, pd.DataFrame],
    initial_capital: float,
    *,
    start_idx: int = 0,
) -> Optional[BenchmarkResult]:
    """Buy the assets observable at the benchmark start and never rebalance.

    Assets that list later are deliberately excluded.  This makes the joining
    rule explicit and prevents a future observation from changing historical
    weights.
    """

    closes = _close_matrix(data_map)
    if closes.empty:
        return None
    start_idx = min(max(int(start_idx), 0), len(closes) - 1)
    start_time = closes.index[start_idx]
    start_prices = closes.loc[start_time].dropna()
    if start_prices.empty:
        for candidate_time, row in closes.iloc[start_idx:].iterrows():
            start_prices = row.dropna()
            if not start_prices.empty:
                start_time = candidate_time
                break
    if start_prices.empty:
        return None

    eligible = list(start_prices.index)
    weight = 1.0 / len(eligible)
    units = {symbol: initial_capital * weight / start_prices[symbol] for symbol in eligible}
    valued = closes[eligible].ffill()
    equity = sum(valued[symbol] * units[symbol] for symbol in eligible)
    equity = equity.astype(float)
    equity.loc[equity.index < start_time] = initial_capital
    equity = equity.fillna(initial_capital).rename("fixed_equal_weight")

    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for symbol in eligible:
        position_value = valued[symbol] * units[symbol]
        weights.loc[:, symbol] = (position_value / equity).fillna(0.0)
    turnover = pd.Series(0.0, index=closes.index, name="turnover")
    costs = pd.Series(0.0, index=closes.index, name="cost")
    return BenchmarkResult(
        equity=equity,
        weights=weights,
        turnover=turnover,
        costs=costs,
        metadata={
            "name": "fixed_equal_weight_buy_and_hold",
            "start_time": start_time,
            "eligible_assets": eligible,
            "initial_weights": {symbol: weight for symbol in eligible},
            "asset_join_rule": "assets with a valid close at benchmark start only",
            "rebalance_rule": "never",
            "cost_bps": 0.0,
        },
    )


def dynamic_equal_weight_rebalanced(
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
            current_equity *= 1.0 + float((previous_weights * asset_returns).sum())

        active = prices.dropna().index
        target = pd.Series(0.0, index=closes.columns)
        if len(active):
            target.loc[active] = 1.0 / len(active)
        # Moving from cash into the initial portfolio trades 100% of capital;
        # subsequent asset-to-asset rebalances use one-way turnover (half the
        # sum of absolute weight changes, avoiding double-counting buy+sell).
        step_turnover = (
            float(target.abs().sum())
            if float(previous_weights.abs().sum()) == 0.0
            else 0.5 * float((target - previous_weights).abs().sum())
        )
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
            "name": "dynamic_equal_weight_rebalanced",
            "start_time": index[start_idx],
            "asset_join_rule": "assets with a valid close on each rebalance timestamp",
            "rebalance_rule": "every event timestamp",
            "turnover_formula": "0.5 * sum(abs(target_weight - previous_weight))",
            "cost_bps": float(cost_bps),
        },
    )


__all__ = [
    "BenchmarkResult",
    "dynamic_equal_weight_rebalanced",
    "fixed_equal_weight_buy_hold",
]
