"""Recorded execution adapter and deterministic order/fill replay."""

from __future__ import annotations

import inspect
import math
from typing import Any, Callable, Iterable, Optional

from core.domain import OrderErrorCode
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
        opening_guard: Optional[Callable[[], bool]] = None,
    ) -> None:
        if broker is None and portfolio is None:
            portfolio = Portfolio(0)
        resolved_portfolio = broker.portfolio if broker is not None else portfolio
        if resolved_portfolio is None:
            raise ValueError("recorded execution requires a portfolio")
        self.broker = broker
        self.opening_guard = opening_guard
        self.portfolio: Portfolio = resolved_portfolio
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
        store = getattr(self.broker, "order_store", None)
        if self.opening_guard is not None and store is not None and callable(getattr(type(store), "get", None)):
            existing = store.get(intent.client_order_id)
            if existing is not None and not (
                    existing["status"] == "submitting" and not existing["submission_attempted"]):
                # A durable attempted order is recovered, not resubmitted.
                # Never turn an accepted/UNKNOWN order into a local rejection.
                return self.broker.submit_intent(intent)
        if self.opening_guard is not None and not self._is_proven_reduction(
                getattr(intent, "symbol", None), getattr(intent, "action", None),
                getattr(intent, "requested_qty", None), getattr(intent, "reduce_only", False)):
            if self.opening_guard() is not True:
                reject = getattr(type(self.broker), "record_local_rejection", None)
                if callable(reject):
                    return self.broker.record_local_rejection(
                        intent, "independent account facts block new risk", OrderErrorCode.SAFETY_POLICY)
                raise AccountEntryBlocked("independent account facts block new risk")
        return self.broker.submit_intent(intent)

    def submit_order(self, *args, **kwargs):
        if self.broker is None:
            raise RuntimeError("offline recorded execution cannot submit orders")
        if self.opening_guard is not None:
            try:
                bound = inspect.signature(self.broker.submit_order).bind(*args, **kwargs)
                bound.apply_defaults()
                facts = bound.arguments
            except (TypeError, ValueError):
                facts = {}
            if not self._is_proven_reduction(facts.get("symbol"), facts.get("side"),
                                            facts.get("qty"), facts.get("reduce_only", False)):
                if self.opening_guard() is not True:
                    # No broker submission has occurred, so no fictitious
                    # durable order id or accepted fill is returned.
                    raise AccountEntryBlocked("independent account facts block new risk")
        return self.broker.submit_order(*args, **kwargs)

    def _is_proven_reduction(self, symbol, action, quantity, reduce_only):
        """Check signed inventory and size, not a blanket buy/sell exemption."""
        if action not in {"buy", "sell", "short", "cover"}:
            return False
        if action in {"buy", "short"} and reduce_only is not True:
            return False
        try:
            if isinstance(quantity, bool):
                return False
            qty = float(quantity)
            held = float(self.portfolio.get_position(symbol)["qty"])
            signed = qty if action in {"buy", "cover"} else -qty
            return (math.isfinite(qty) and math.isfinite(held) and qty > 0
                    and qty <= abs(held) + 1e-12 and signed * held < 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

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


class AccountEntryBlocked(ValueError):
    """A legacy opening request was rejected before any venue side effect."""
