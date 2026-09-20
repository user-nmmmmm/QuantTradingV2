"""Auditable fixed and dynamic equal-weight benchmark implementations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, Mapping, Optional

import pandas as pd


@dataclass(frozen=True)
class BenchmarkResult:
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    metadata: Dict[str, object]

    def __post_init__(self):
        self.equity.attrs["benchmark"] = dict(self.metadata)


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
    weights.loc[weights.index < start_time, :] = 0.0
    turnover = pd.Series(0.0, index=closes.index, name="turnover")
    costs = pd.Series(0.0, index=closes.index, name="cost")
    return BenchmarkResult(
        equity=equity,
        weights=weights,
        turnover=turnover,
        costs=costs,
        metadata={
            "benchmark_id": "equal_weight_initial_close_buy_hold/v1",
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


def btc_eth_first_open_buy_hold(data_map, initial_capital, *, start, end):
    """9/14 frozen research convention: two fixed cash sleeves, no rebalance."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if start > end or not math.isfinite(float(initial_capital)) or not float(initial_capital) > 0:
        raise ValueError("valid benchmark range and positive capital required")
    dates = pd.date_range(start, end, freq="D")
    values = pd.DataFrame(index=dates)
    entries = {}
    for symbol in ("BTC/USDT", "ETH/USDT"):
        if symbol not in data_map:
            raise ValueError(f"benchmark source missing: {symbol}")
        frame = data_map[symbol].copy()
        frame.index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError("benchmark timestamps must be ordered and unique")
        frame = frame.loc[start:end]
        sleeve = pd.Series(float(initial_capital) / 2, index=dates)
        if not frame.empty:
            if not (frame[["open", "close"]].gt(0).all().all()) or not frame[["open", "close"]].map(lambda v: pd.notna(v) and float(v) < float("inf")).all().all():
                raise ValueError("benchmark prices must be positive and finite")
            entry = frame.index[0]
            if not entry == entry.normalize() or not frame.index.isin(dates).all():
                raise ValueError("research benchmark requires daily UTC bars")
            units = float(initial_capital) / 2 / float(frame.open.iloc[0])
            sleeve.loc[entry:] = (units * frame.close).reindex(dates[dates >= entry]).ffill()
            entries[symbol] = entry.isoformat()
        else:
            entries[symbol] = None
        values[symbol] = sleeve
    equity = values.sum(axis=1).rename("btc_eth_equal_buy_hold_gross")
    weights = values.div(equity, axis=0)
    for symbol, entry in entries.items():
        if entry is None:
            weights[symbol] = 0.0
        else:
            weights.loc[weights.index < pd.Timestamp(entry), symbol] = 0.0
    return BenchmarkResult(equity, weights, pd.Series(0.0, index=dates), pd.Series(0.0, index=dates), {
        "benchmark_id": "btc_eth_50_50_first_open_buy_hold_gross/2026-09-14",
        "initial_weights": {"BTC/USDT": 0.5, "ETH/USDT": 0.5}, "initial_capital": float(initial_capital),
        "entry_rule": "first observed UTC daily open in each evaluation period",
        "entry_times": entries, "rebalance_rule": "never", "pre_listing": "cash per sleeve",
        "cost_bps": 0.0, "cost_policy": "gross unlevered reference; no trading fees",
        "missing_mark_policy": "carry last observed close; never backfill before listing"})


__all__ = [
    "BenchmarkResult",
    "dynamic_equal_weight_rebalanced",
    "fixed_equal_weight_buy_hold",
    "btc_eth_first_open_buy_hold",
]
