"""Bounded restart-safe bar selection; historical replay has no execution port."""
from dataclasses import dataclass
import pandas as pd

from core.timeframes import as_utc_timestamp, timeframe_delta, observed_bars_after


@dataclass(frozen=True)
class CatchupPlan:
    replay_times: tuple
    live_time: object
    pending: int
    gap: bool


def plan_catchup(frame, last_processed, *, timeframe, max_bars=100):
    if type(max_bars) is not int or max_bars < 1:
        raise ValueError("catchup limit must be a positive integer")
    if frame.empty:
        return CatchupPlan((), None, 0, False)
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ValueError("catchup history must be sorted and unique")
    latest = frame.index[-1]
    # First start adopts the latest bar rather than trading historical signals.
    if last_processed is None:
        return CatchupPlan((), latest, 0, False)
    last = as_utc_timestamp(last_processed)
    index = pd.DatetimeIndex(frame.index)
    comparable = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    unseen = list(index[comparable > last])
    if not unseen:
        return CatchupPlan((), None, 0, False)
    step = timeframe_delta(timeframe)
    points = [last, *[as_utc_timestamp(point) for point in unseen]]
    gap = any(b - a > step for a, b in zip(points, points[1:]))
    contiguous = []
    previous = last
    for point in unseen:
        stamp = as_utc_timestamp(point)
        if stamp - previous > step:
            break
        contiguous.append(point)
        previous = stamp
    historical = contiguous if gap else unseen[:-1]
    replay = tuple(historical[:max_bars])
    pending = max(0, len(unseen) - len(replay) - (0 if gap else 1))
    return CatchupPlan(replay, None if pending or gap else latest, pending, gap)


def replay_state_only(engine, symbol, frame, timestamp):
    """Recover routing/cooldown state with no broker or candidate allocation."""
    location = frame.index.get_loc(timestamp)
    state = engine.state_machine.get_state(frame, location)
    engine.event_processor._last_market_states[symbol] = state
    # Track transitions, but do not create historical cancel/exit/order calls.
    cooling = False
    if symbol in engine.router.cooldowns:
        started = engine.router._cooldown_started_at.get(symbol)
        if started is not None:
            last, elapsed = engine.router._cooldown_progress.get(symbol, (started, 0))
            if as_utc_timestamp(timestamp) > as_utc_timestamp(last):
                elapsed += observed_bars_after(frame, location, last)
                engine.router._cooldown_progress[symbol] = (timestamp, elapsed)
            cooling = elapsed <= engine.router.cooldown_bars
        else:
            cooling = location <= engine.router.cooldowns[symbol]
        if not cooling:
            engine.router.cooldowns.pop(symbol, None)
            engine.router._cooldown_started_at.pop(symbol, None)
            engine.router._cooldown_progress.pop(symbol, None)
    previous = engine.router.symbol_states.get(symbol)
    if not cooling and previous is not None and engine.router._map_state_to_strategy(previous) != engine.router._map_state_to_strategy(state):
        engine.router.cooldowns[symbol] = location + engine.router.cooldown_bars
        engine.router._cooldown_started_at[symbol] = timestamp
        engine.router._cooldown_progress[symbol] = (timestamp, 0)
    if not cooling:
        engine.router.symbol_states[symbol] = state
    for strategy in engine.strategies.values():
        context = strategy.context.get(symbol)
        if context:
            strategy._just_entered(location, frame, context)
        trade_state = getattr(strategy, "trade_state", {}).get(symbol, {})
        started = trade_state.get("cooldown_started_at")
        if started is not None:
            last = trade_state.get("cooldown_last_timestamp", started)
            if as_utc_timestamp(timestamp) > as_utc_timestamp(last):
                trade_state["cooldown_bar_count"] = trade_state.get("cooldown_bar_count", 0) + observed_bars_after(frame, location, last)
                trade_state["cooldown_last_timestamp"] = timestamp.isoformat()
            if trade_state.get("cooldown_bar_count", 0) > 24:
                trade_state.pop("cooldown_started_at", None)
                trade_state["cooldown_until"] = -1


def runtime_checkpoint(engine):
    values = {"router_runtime": engine.router.checkpoint()}
    for strategy in engine.strategies.values():
        values[f"strategy_runtime:{strategy.name}"] = {
            "context": strategy.context,
            "consumed_close_event_ids": sorted(strategy._consumed_close_event_ids),
            "trade_state": getattr(strategy, "trade_state", None),
        }
    return values
