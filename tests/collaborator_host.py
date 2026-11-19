"""Test double for the live engine: exposes exactly what ``TickOrchestratorMixin`` (and its base) used to.

Before B4 (ADR 0018) several tests subclassed the mixin to get an "engine" with only the tick/risk-action
methods. The collaborators now hold a ``host`` reference, so the double is a plain class whose ``__getattr__``
forwards method names to the two collaborators. It deliberately does *not* inherit ``LiveTradingEngine``:
code such as ``getattr(host, "_ensure_state_store", None)`` must keep seeing the same missing methods.
"""
from __future__ import annotations

from functools import cached_property
from typing import Any, Dict, List, Optional, Set

from core.risk.delegation import Delegated

from live_trading.risk_actions import PortfolioRiskActions
from live_trading.tick_orchestrator import TickOrchestrator


class TickOrchestratorHost:
    # Same forwarding as LiveTradingEngine (B6, ADR 0031): the tick loop's bookkeeping lives on the orchestrator.
    _tick_count = Delegated[int]("_tick_orchestrator")
    _consecutive_tick_crashes = Delegated[int]("_tick_orchestrator")
    _next_retry_delay = Delegated[float]("_tick_orchestrator")
    _consecutive_strategy_failures = Delegated[int]("_tick_orchestrator")
    _last_strategy_error = Delegated[Optional[str]]("_tick_orchestrator")
    _last_tick_seconds = Delegated[Optional[float]]("_tick_orchestrator")
    _alerted_health_transitions = Delegated[Set[Any]]("_tick_orchestrator")
    _live_fill_risk_audit = Delegated[List[Dict[str, Any]]]("_tick_orchestrator")

    @cached_property
    def _tick_orchestrator(self) -> TickOrchestrator:
        return TickOrchestrator(self)       # type: ignore[arg-type]

    @cached_property
    def _risk_actions(self) -> PortfolioRiskActions:
        return PortfolioRiskActions(self)   # type: ignore[arg-type]

    def __getattr__(self, name: str):
        # only reached when normal lookup fails; dunder names must not be forwarded (copy/pickle probes)
        if name.startswith("__"):
            raise AttributeError(name)
        for collaborator in (self._tick_orchestrator, self._risk_actions):
            if name != "_host" and hasattr(type(collaborator), name):
                return getattr(collaborator, name)
        raise AttributeError(name)
