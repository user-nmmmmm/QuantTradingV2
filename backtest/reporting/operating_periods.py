"""Calendar presentation never turns a missing market observation into a fact."""
from __future__ import annotations

import pandas as pd
from typing import Any


def requested_period_curve(curve, start, end, *, capital, lifecycle, activity=()):
    source = curve.copy()
    source.index = pd.to_datetime(source.index, utc=True).tz_convert(None).normalize()
    source = source.groupby(level=0).last()
    dates = pd.date_range(pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None))
    result = source.reindex(dates).ffill()
    for column in result:
        result[column] = result[column].fillna(capital if column in {'equity', 'cash'} else 0)
    observation_dates = source.index
    termination = lifecycle.get('termination_timestamp')
    if termination is not None:
        # Legacy engine cash-tail rows are presentation marks, not evaluated
        # market events. They cannot advance the observation timestamp.
        observation_dates = observation_dates[observation_dates <= pd.Timestamp(termination).tz_localize(None)]
    observed = dates.isin(observation_dates)
    result['market_observed'] = observed
    result['valuation_stale'] = ~observed
    marks = pd.Series(observation_dates, index=observation_dates)
    result['last_market_observation'] = marks.reindex(dates).ffill()
    result['phase'] = 'market_evaluation'
    result['account_state'] = 'normal'
    result['strategy_state'] = 'unknown'
    states = pd.DataFrame(activity)
    if not states.empty:
        states.index = pd.to_datetime(states.pop('timestamp'), utc=True).dt.tz_convert(None).dt.normalize()
        states = states.groupby(level=0).last().reindex(dates).ffill()
        if 'account_action' in states:
            result['account_state'] = states.account_action.fillna('normal')
        if 'strategy_states' in states:
            result['strategy_state'] = states.strategy_states.map(lambda x: str(x) if isinstance(x, dict) else 'unknown')
        if 'all_routed_strategies_blocked' in states:
            paused = states.all_routed_strategies_blocked.eq(True)
            result.loc[paused, 'phase'] = 'strategy_health_pause'
    result.loc[~observed, 'phase'] = 'market_data_missing'
    before = dates < source.index.min() if len(source) else pd.Series(True, index=dates)
    result.loc[before, 'phase'] = 'pre_market_cash'
    result.loc[before, 'valuation_stale'] = False
    if termination is not None:
        after = dates > pd.Timestamp(termination).tz_localize(None)
        result.loc[after, 'phase'] = 'risk_halted_cash'
        result.loc[after, 'account_state'] = 'halted'
        result.loc[after, 'valuation_stale'] = False
    return result


def split_execution_records(trades, *, mark_to_market):
    actual: list[dict[str, Any]] = []
    valuations: list[dict[str, Any]] = []
    for row in trades:
        (valuations if mark_to_market and row.get('exit_reason') == 'EndOfBacktest' else actual).append(row)
    return actual, valuations
