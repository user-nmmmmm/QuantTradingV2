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
from core.risk import RiskManager


@dataclass(frozen=True)
class MarketDataSlice:
    """One deterministic market-data event on the shared runtime timeline."""

    timestamp: pd.Timestamp
    bars: Mapping[str, pd.Series]
    histories: Mapping[str, pd.DataFrame]
    timeframe: str = "unknown"
    source: str = "market_data"

    def __post_init__(self) -> None:
        timestamp = pd.Timestamp(self.timestamp)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "bars", dict(self.bars))
        object.__setattr__(self, "histories", dict(self.histories))


class MarketDataAdapter(Protocol):
    """Produces the canonical timeline consumed by :class:`EventProcessor`."""

    def stream(self) -> Iterable[MarketDataSlice]: ...


class RuntimeExecutionAdapter(ExecutionPort, Protocol):
    """Execution port with an optional market-event hook."""

    portfolio: Portfolio

    def on_market_data(self, event: MarketDataSlice) -> Any: ...


@dataclass
class RuntimeResult:
    equity: float
    cash: float
    prices: Dict[str, float]
    routed_symbols: list[str] = field(default_factory=list)
    circuit_breaker: bool = False


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
        router: Any,
        warmup_period: int = 0,
        initial_equity: Optional[float] = None,
    ) -> None:
        self.portfolio = portfolio
        self.execution = execution
        self.risk_manager = risk_manager
        self.state_machine = state_machine
        self.router = router
        self.warmup_period = max(int(warmup_period), 0)
        self.last_prices: Dict[str, float] = {}
        self._current_day = None
        self._daily_start_equity = float(
            initial_equity if initial_equity is not None else portfolio.cash
        )

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
        current_day = event.timestamp.date()
        if current_day != self._current_day:
            if self._current_day is not None:
                self._daily_start_equity = equity
            self.risk_manager.reset_daily_breaker()
            self._current_day = current_day

        breaker = False
        if check_circuit_breaker:
            breaker = bool(
                self.risk_manager.check_circuit_breaker(
                    equity, self._daily_start_equity
                )
            )

        routed: list[str] = []
        selected = set(symbols) if symbols is not None else None
        if not breaker:
            for symbol in event.bars:
                if selected is not None and symbol not in selected:
                    continue
                if self.process_symbol(event, symbol):
                    routed.append(symbol)

        equity = self.portfolio.get_total_value(self.last_prices)
        return RuntimeResult(
            equity=equity,
            cash=float(self.portfolio.cash),
            prices=dict(self.last_prices),
            routed_symbols=routed,
            circuit_breaker=breaker,
        )

    def process_symbol(self, event: MarketDataSlice, symbol: str) -> bool:
        """Run the shared state/strategy/risk path for one real bar."""

        df = event.histories.get(symbol)
        if df is None or df.empty or symbol not in event.bars:
            return False
        try:
            location = df.index.get_loc(event.timestamp)
        except KeyError:
            return False
        if not isinstance(location, int) or location < self.warmup_period:
            return False
        state = self.state_machine.get_state(df, location)
        self.router.route(
            symbol,
            location,
            df,
            state,
            self.portfolio,
            self.execution,
            self.risk_manager,
            dict(self.last_prices),
        )
        return True

    def run(self, market_data: MarketDataAdapter) -> list[RuntimeResult]:
        return [self.process(event) for event in market_data.stream()]

