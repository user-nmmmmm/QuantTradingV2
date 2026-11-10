"""Startup check that live market data is long enough for what is routed (A3, F-03).

The live adapter keeps a rolling window of ``lookback`` closed bars. A
strategy whose look-back is longer than that window never produces a signal,
and nothing reports it: the engine just never enters. TrendPortfolioV2/V3
need 121 bars by default while the adapter used to keep 100.

This check fails closed at construction time and names who needs what.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping


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
    """Raise :class:`InsufficientLiveHistory` unless ``lookback`` covers every need."""
    needs = history_requirements(strategies, regime_map, state_machine)
    short = {name: bars for name, bars in needs.items() if bars > lookback}
    if short:
        detail = ", ".join(f"{name} needs {bars}" for name, bars in sorted(short.items()))
        raise InsufficientLiveHistory(
            f"live market data keeps {lookback} closed bars but {detail}; "
            f"raise lookback_days to at least {max(short.values())}"
        )
    return needs
