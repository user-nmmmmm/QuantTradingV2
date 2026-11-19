"""Startup check that live market data is long enough for what is routed (A3, F-03).

The live adapter keeps a rolling window of ``lookback`` closed bars. A
strategy whose look-back is longer than that window never produces a signal,
and nothing reports it: the engine just never enters. TrendPortfolioV2/V3
need 121 bars by default while the adapter used to keep 100.

This check fails closed at construction time and names who needs what.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping


#: Raw rows a fetched snapshot may spend on the still-forming candle.
UNFINISHED_CANDLE_SLOTS = 1
#: Window floor when the configuration names none (the pre-C2 value).
LEGACY_MIN_LOOKBACK = 100


def live_min_lookback(configuration: Any) -> int:
    """Smallest raw live window, from ``data.live_min_lookback_bars`` (C2).

    SR-DATA-001 found 200 closed bars reproduce full-history states and
    entry candidates while 100 do not; 201 raw rows leave room for the
    unfinished candle. Configurations without the key keep the old floor.
    """
    data = configuration.get("data") if configuration is not None else None
    value = data.get("live_min_lookback_bars") if isinstance(data, Mapping) else None
    if value is None:
        return LEGACY_MIN_LOOKBACK
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise ValueError("data.live_min_lookback_bars must be an integer of at least 2")
    return value


class InsufficientLiveHistory(ValueError):
    """The configured live window cannot serve a routed strategy or the state machine."""


def history_requirements(
    strategies: Mapping[str, Any], regime_map: Mapping[str, str], state_machine: Any,
) -> Dict[str, int]:
    """Bars required by the state machine and by every strategy a regime routes to."""
    routed: Iterable[str] = sorted({name for name in regime_map.values()
                                    if name and name != "Cash"})
    needs: Dict[str, int] = {}
    required = getattr(state_machine, "required_history", None)
    if isinstance(required, int):
        needs["MarketStateMachine"] = required
    for name in routed:
        strategy = strategies.get(name)
        bars = getattr(strategy, "required_history", None)
        if isinstance(bars, int):
            needs[name] = bars
    return needs


def check_live_history(
    lookback: int, strategies: Mapping[str, Any], regime_map: Mapping[str, str],
    state_machine: Any,
) -> Dict[str, int]:
    """Raise :class:`InsufficientLiveHistory` unless ``lookback`` covers every need.

    ``lookback`` is the adapter's raw window. The adapter trims to that many
    rows *before* the unfinished candle is dropped, so one slot is reserved
    for it: only ``lookback - 1`` closed bars are guaranteed to reach routing.
    """
    needs = history_requirements(strategies, regime_map, state_machine)
    closed = lookback - UNFINISHED_CANDLE_SLOTS
    short = {name: bars for name, bars in needs.items() if bars > closed}
    if short:
        detail = ", ".join(f"{name} needs {bars}" for name, bars in sorted(short.items()))
        raise InsufficientLiveHistory(
            f"live market data keeps {lookback} bars ({closed} closed after the "
            f"unfinished candle) but {detail}; raise lookback_days to at least "
            f"{max(short.values()) + UNFINISHED_CANDLE_SLOTS}"
        )
    return needs


def check_router_history(lookback: int, router: Any, state_machine: Any) -> Dict[str, int]:
    """Engine entry point: check the window against a built router's routes."""
    return check_live_history(lookback, router.strategies, router.regime_map, state_machine)
