"""Simulated execution adapter for the shared runtime."""

from __future__ import annotations

from typing import Any

from core.broker import Broker
from core.runtime import MarketDataSlice


class SimulatedExecutionAdapter:
    """Execution adapter that advances the historical matching broker."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.portfolio = broker.portfolio

    def on_market_data(self, event: MarketDataSlice):
        bars = dict(event.bars)
        trades = self.broker.process_orders(bars)
        self.broker.accrue_carry(bars)
        return trades

    def __getattr__(self, name: str) -> Any:
        return getattr(self.broker, name)
