"""Shared event runtime used by historical and live trading modes.

The runtime intentionally knows nothing about polling, exchanges, or historical
matching.  Those concerns live behind market-data and execution adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence

import pandas as pd

from core.execution_port import ExecutionPort
from core.portfolio import Portfolio
from core.risk import BreakerAction, RiskControlDecision, RiskManager


@dataclass(frozen=True)
class MarketDataSlice:
    """One deterministic market-data event on the shared runtime timeline."""

    timestamp: pd.Timestamp
    bars: Mapping[str, pd.Series]
    histories: Mapping[str, pd.DataFrame]
    timeframe: str = "unknown"
    source: str = "market_data"
    positions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp = pd.Timestamp(self.timestamp)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "bars", dict(self.bars))
        object.__setattr__(self, "histories", dict(self.histories))
        object.__setattr__(self, "positions", dict(self.positions))


class MarketDataAdapter(Protocol):
    """Produces the canonical timeline consumed by :class:`EventProcessor`."""

    def stream(self) -> Iterable[MarketDataSlice]: ...


class RuntimeExecutionAdapter(ExecutionPort, Protocol):
    """Execution port with an optional market-event hook."""

    portfolio: Portfolio

    def on_market_data(self, event: MarketDataSlice) -> Any: ...


class CandidateRouter(Protocol):
    """Static routing contract consumed by the shared runtime."""

    def collect_candidate(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: Any,
        portfolio: Portfolio,
        broker: ExecutionPort,
        risk_manager: RiskManager,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Any: ...

    def process_position_management(
        self, symbol: str, i: int, df: pd.DataFrame, state: Any,
        portfolio: Portfolio, broker: ExecutionPort,
    ) -> bool: ...

    def collect_entry_candidate(
        self, symbol: str, i: int, df: pd.DataFrame, state: Any,
        portfolio: Portfolio, broker: ExecutionPort, risk_manager: RiskManager,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Any: ...


class CandidateAllocator(Protocol):
    """Portfolio-level allocation contract consumed by the shared runtime."""

    def allocate(
        self,
        candidates: Iterable[Any],
        *,
        portfolio: Portfolio,
        broker: RuntimeExecutionAdapter,
        risk_manager: RiskManager,
        current_prices: Mapping[str, float],
    ) -> Any: ...


@dataclass
class RuntimeResult:
    equity: float
    cash: float
    prices: Dict[str, float]
    routed_symbols: list[str] = field(default_factory=list)
    circuit_breaker: bool = False
    breaker_action: str = "normal"
    risk_decision: Optional[RiskControlDecision] = None


class EventProcessor:
    """Mode-independent bar processor.

    Both engines call this object for the business path from a closed bar to a
    routed strategy.  Mode-specific durability and scheduling remain outside.
    """

    def __init__(
        self,
        *,
        portfolio: Portfolio,
        execution: RuntimeExecutionAdapter,
        risk_manager: RiskManager,
        state_machine: Any,
        router: CandidateRouter,
        allocator: CandidateAllocator,
        warmup_period: int = 0,
        initial_equity: Optional[float] = None,
    ) -> None:
        self.portfolio = portfolio
        self.execution = execution
        self.risk_manager = risk_manager
        self.state_machine = state_machine
        self.router = router
        self.allocator = allocator
        self.warmup_period = max(int(warmup_period), 0)
        self.last_prices: Dict[str, float] = {}
        self._current_day = None
        self._daily_start_equity = float(
            initial_equity if initial_equity is not None else portfolio.cash
        )
        self._previous_session_close_equity = self._daily_start_equity
        self._bar_index = -1

    @staticmethod
    def _utc_datetime(value: Any) -> datetime:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.to_pydatetime()

    def process(
        self,
        event: MarketDataSlice,
        *,
        symbols: Optional[Sequence[str]] = None,
        execute_market_event: bool = True,
        check_circuit_breaker: bool = True,
    ) -> RuntimeResult:
        """Process one canonical market-data event in deterministic order."""

        if not isinstance(event, MarketDataSlice):
            raise TypeError("event must be MarketDataSlice")
        if execute_market_event:
            hook = getattr(self.execution, "on_market_data", None)
            if callable(hook):
                hook(event)

        for symbol, bar in event.bars.items():
            close = float(bar["close"])
            if pd.notna(close):
                self.last_prices[symbol] = close

        equity = self.portfolio.get_total_value(self.last_prices)
        self._bar_index += 1
        current_day = event.timestamp.date()
        if current_day != self._current_day:
            if self._current_day is not None:
                # Daily bars compare the current close with the prior session's
                # close.  Intraday bars retain the first session baseline.
                self._daily_start_equity = self._previous_session_close_equity
            self.risk_manager.reset_daily_breaker()
            self._current_day = current_day

        raw_decision: Any = False
        if check_circuit_breaker:
            try:
                raw_decision = self.risk_manager.check_circuit_breaker(
                    equity, self._daily_start_equity,
                    occurred_at=event.timestamp, bar_index=self._bar_index,
                )
            except TypeError:
                # Compatibility for adapters/test doubles implementing the
                # pre-decision two-positional-argument contract.
                raw_decision = self.risk_manager.check_circuit_breaker(
                    equity, self._daily_start_equity
                )
        decision = self._normalize_risk_decision(raw_decision)

        routed: list[str] = []
        selected = set(symbols) if symbols is not None else None
        candidates = []
        # Position management and entry collection are deliberately separate:
        # BLOCK_NEW must never disable stops or strategy exits.
        for symbol in event.bars:
            if selected is not None and symbol not in selected:
                continue
            candidate, processed = self._collect_symbol_candidate(
                event, symbol,
                allow_position_management=decision.allow_position_management,
                allow_new_entries=decision.allow_new_entries,
            )
            if processed:
                routed.append(symbol)
            if candidate is not None:
                candidates.append(candidate)
        if decision.allow_new_entries:
            self.allocator.allocate(
                candidates, portfolio=self.portfolio, broker=self.execution,
                risk_manager=self.risk_manager, current_prices=self.last_prices,
            )

        self._previous_session_close_equity = equity

        return RuntimeResult(
            equity=equity,
            cash=float(self.portfolio.cash),
            prices=self.last_prices,
            routed_symbols=routed,
            circuit_breaker=bool(decision),
            breaker_action=decision.action.value,
            risk_decision=decision,
        )

    def _normalize_risk_decision(self, value: Any) -> RiskControlDecision:
        if isinstance(value, RiskControlDecision):
            return value
        action_value = getattr(self.risk_manager, "breaker_action", BreakerAction.NORMAL)
        try:
            action = action_value if isinstance(action_value, BreakerAction) else BreakerAction(
                getattr(action_value, "value", action_value)
            )
        except (TypeError, ValueError):
            action = BreakerAction.NORMAL
        blocked = bool(value)
        return RiskControlDecision(
            action=action,
            allow_position_management=action not in {
                BreakerAction.LIQUIDATE, BreakerAction.LOCKED
            },
            allow_new_entries=not blocked,
            force_reduce_fraction=(
                getattr(self.risk_manager, "reduced_risk_multiplier", 0.5)
                if action is BreakerAction.REDUCE else None
            ),
            force_liquidate=action in {
                BreakerAction.LIQUIDATE, BreakerAction.LOCKED
            } or (blocked and action is BreakerAction.NORMAL),
            terminal=action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED},
        )

    def process_symbol(self, event: MarketDataSlice, symbol: str) -> bool:
        """Run the shared state/strategy/risk path for one real bar."""

        candidate, processed = self._collect_symbol_candidate(event, symbol)
        if candidate is not None:
            self.allocator.allocate(
                [candidate], portfolio=self.portfolio, broker=self.execution,
                risk_manager=self.risk_manager, current_prices=self.last_prices,
            )
        return processed

    def _collect_symbol_candidate(
        self, event: MarketDataSlice, symbol: str, *,
        allow_position_management: bool = True,
        allow_new_entries: bool = True,
    ):
        """Return ``(candidate, processed)`` without allocating capital."""

        df = event.histories.get(symbol)
        if df is None or df.empty or symbol not in event.bars:
            return None, False
        location = event.positions.get(symbol)
        if location is None:
            try:
                location = df.index.get_loc(event.timestamp)
            except KeyError:
                return None, False
        if not isinstance(location, int) or location < self.warmup_period:
            return None, False
        state = self.state_machine.get_state(df, location)
        router_type = type(self.router)
        legacy_override = "collect_candidate" in vars(self.router)
        manager = (
            getattr(self.router, "process_position_management", None)
            if not legacy_override and hasattr(router_type, "process_position_management") else None
        )
        entry_collector = (
            getattr(self.router, "collect_entry_candidate", None)
            if not legacy_override and hasattr(router_type, "collect_entry_candidate") else None
        )
        if callable(manager) and callable(entry_collector):
            held = False
            if allow_position_management:
                held = bool(manager(
                    symbol, location, df, state, self.portfolio, self.execution,
                ))
            if held or not allow_new_entries:
                candidate = None
            else:
                candidate = entry_collector(
                    symbol, location, df, state, self.portfolio, self.execution,
                    self.risk_manager, self.last_prices,
                )
        elif allow_position_management or allow_new_entries:
            candidate = self.router.collect_candidate(
                symbol, location, df, state, self.portfolio, self.execution,
                self.risk_manager, self.last_prices,
            )
            if not allow_new_entries:
                candidate = None
        else:
            candidate = None
        return candidate, True

    def run(self, market_data: MarketDataAdapter) -> list[RuntimeResult]:
        return [self.process(event) for event in market_data.stream()]
