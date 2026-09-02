"""Simulated execution adapter for the shared runtime."""

from __future__ import annotations

from typing import Any

from core.broker import Broker
from core.broker.matching import is_protective_stop
from core.broker.types import Order
from core.runtime import MarketDataSlice


def _is_not_protective_stop(order: Order) -> bool:
    """Everything in the book except the venue-resident protective stops.

    Resident stops belong to :class:`backtest.protective_stops.ResidentStopSimulator`,
    which matches them in its own pass *after* this one so that protection is
    reconciled against the position that the open just created (STR-P1-01).
    Leaving them in this pass let a stop armed on an earlier bar fill here
    instead: the fill never reached ``stop_order_audit`` or ``triggered_stops``,
    and it bypassed the reconciliation that cancels a stop whose position was
    already closed at this bar's open.
    """
    return not is_protective_stop(order)


class SimulatedExecutionAdapter:
    """Execution adapter that advances the historical matching broker."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.portfolio = broker.portfolio

    def on_market_data(self, event: MarketDataSlice):
        bars = dict(event.bars)
        trades = self.broker.process_orders(
            bars, order_filter=_is_not_protective_stop
        )
        self.broker.accrue_carry(bars)
        return trades

    def __getattr__(self, name: str) -> Any:
        return getattr(self.broker, name)
