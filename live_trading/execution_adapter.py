"""Recorded execution adapter and deterministic order/fill replay."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from core.events import EventEnvelope, FillEvent, TradingEventPipeline
from core.portfolio import Portfolio
from core.runtime import MarketDataSlice


class RecordedExecutionAdapter:
    """Record live execution or replay exchange facts without resubmission.

    ``broker`` is optional for offline reconstruction.  Fill application is
    opt-in so synchronized live portfolios cannot accidentally double-apply a
    venue fact.
    """

    def __init__(
        self,
        broker: Any = None,
        *,
        portfolio: Optional[Portfolio] = None,
        pipeline: Optional[TradingEventPipeline] = None,
        events: Optional[Iterable[EventEnvelope]] = None,
    ) -> None:
        if broker is None and portfolio is None:
            portfolio = Portfolio(0)
        self.broker = broker
        self.portfolio = broker.portfolio if broker is not None else portfolio
        self.pipeline = (
            pipeline
            or (getattr(broker, "event_pipeline", None) if broker is not None else None)
            or TradingEventPipeline()
        )
        self._applied_fill_ids: set[str] = set()
        if events is not None:
            self.replay(events)

    @property
    def events(self):
        return self.pipeline.events

    def on_market_data(self, event: MarketDataSlice):
        return None

    def submit_intent(self, intent):
        if self.broker is None:
            raise RuntimeError("offline recorded execution cannot submit orders")
        return self.broker.submit_intent(intent)

    def submit_order(self, *args, **kwargs):
        if self.broker is None:
            raise RuntimeError("offline recorded execution cannot submit orders")
        return self.broker.submit_order(*args, **kwargs)

    def replay(
        self,
        events: Iterable[EventEnvelope],
        *,
        apply_fills: bool = False,
    ) -> tuple[EventEnvelope, ...]:
        replayed = tuple(events)
        for event in replayed:
            if not isinstance(event, EventEnvelope):
                raise TypeError("recorded execution replay accepts EventEnvelope values")
            self.pipeline.consume(event)
            if apply_fills and isinstance(event.payload, FillEvent):
                self._apply_fill(event.payload)
        return replayed

    def _apply_fill(self, fill: FillEvent) -> None:
        if fill.fill_id in self._applied_fill_ids:
            return
        direction = 1.0 if fill.side in {"buy", "cover"} else -1.0
        self.portfolio.update_position(
            fill.symbol,
            direction * float(fill.qty),
            float(fill.price),
            float(fill.fee),
        )
        self._applied_fill_ids.add(fill.fill_id)

    def __getattr__(self, name: str) -> Any:
        if self.broker is None:
            raise AttributeError(name)
        return getattr(self.broker, name)

