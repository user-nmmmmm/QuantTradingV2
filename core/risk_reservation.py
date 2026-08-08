"""Event-sourced risk reservations shared by simulated and live execution."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock
from typing import Any, Dict, Iterator, Optional, Set, Tuple

from core.domain import OrderIntent, OrderStatus, RiskDecision, RiskReservation
from core.events import (
    EventEnvelope,
    FillEvent,
    OrderEvent,
    TradingEventPipeline,
    stable_uuid5,
)


OPENING_ACTIONS = {"buy", "short"}
RELEASING_STATUSES = {
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.FILLED,
}


def as_decimal(value: Any, field_name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


@dataclass
class ReservationState:
    reservation: RiskReservation
    remaining_qty: Decimal
    released: bool = False

    @property
    def intent_id(self) -> str:
        return self.reservation.intent_id


class RiskReservationProjection:
    """Idempotent projection of currently occupied pre-trade risk capacity."""

    def __init__(self, pipeline: Optional[TradingEventPipeline] = None) -> None:
        self._lock = RLock()
        self._states: Dict[str, ReservationState] = {}
        self._by_intent: Dict[str, str] = {}
        self._seen_events: Set[str] = set()
        self._unsubscribe = (
            pipeline.subscribe(self.apply) if pipeline is not None else None
        )

    @contextmanager
    def transaction(self) -> Iterator["RiskReservationProjection"]:
        with self._lock:
            yield self

    def apply(self, event: EventEnvelope) -> bool:
        if not isinstance(event, EventEnvelope):
            raise TypeError("reservation projection accepts EventEnvelope only")
        key = str(event.event_id)
        with self._lock:
            if key in self._seen_events:
                return False
            payload = event.payload
            if isinstance(payload, RiskReservation):
                current = self._states.get(payload.reservation_id)
                if current is not None and current.reservation != payload:
                    raise ValueError(f"reservation conflict: {payload.reservation_id}")
                if current is None:
                    self._states[payload.reservation_id] = ReservationState(
                        payload, payload.reserved_qty
                    )
                    self._by_intent[payload.intent_id] = payload.reservation_id
            elif isinstance(payload, FillEvent):
                self._consume(payload.client_order_id, payload.qty)
            elif isinstance(payload, OrderEvent):
                # UNKNOWN deliberately does nothing: ambiguous exchange state must
                # occupy capacity until an authoritative lifecycle fact arrives.
                if payload.status in RELEASING_STATUSES:
                    self._release(payload.client_order_id)
            self._seen_events.add(key)
            return True

    def _consume(self, intent_id: str, qty: Any) -> None:
        reservation_id = self._by_intent.get(intent_id)
        if reservation_id is None:
            return
        state = self._states[reservation_id]
        if state.released:
            return
        state.remaining_qty = max(
            state.remaining_qty - as_decimal(qty, "fill qty"), Decimal("0")
        )
        if state.remaining_qty == 0:
            state.released = True

    def _release(self, intent_id: str) -> None:
        reservation_id = self._by_intent.get(intent_id)
        if reservation_id is None:
            return
        state = self._states[reservation_id]
        state.remaining_qty = Decimal("0")
        state.released = True

    def pending_notional(
        self, current_prices: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        prices = current_prices or {}
        totals: Dict[str, Decimal] = {}
        with self._lock:
            for state in self._states.values():
                if state.released or state.remaining_qty <= 0:
                    continue
                item = state.reservation
                market_price = as_decimal(prices.get(item.symbol, 0), "market price")
                reference_price = max(item.reference_price, market_price)
                totals[item.symbol] = totals.get(item.symbol, Decimal("0")) + (
                    state.remaining_qty * reference_price
                )
        return {symbol: float(value) for symbol, value in totals.items()}

    def remaining_qty(self, reservation_id: str) -> Decimal:
        with self._lock:
            state = self._states.get(reservation_id)
            return Decimal("0") if state is None else state.remaining_qty

    def rebuild(self, events) -> "RiskReservationProjection":
        with self._lock:
            self._states.clear()
            self._by_intent.clear()
            self._seen_events.clear()
        for event in events:
            self.apply(event)
        return self


def ensure_opening_reservation(
    pipeline: TradingEventPipeline,
    intent: OrderIntent,
    *,
    reference_price: Any,
    occurred_at,
    source: str,
    reason: str = "restored_from_durable_intent",
) -> Tuple[OrderIntent, EventEnvelope]:
    """Idempotently enrich and publish an opening intent with its reservation."""
    if (
        intent.action not in OPENING_ACTIONS
        or intent.reduce_only
        or intent.requested_qty <= 0
    ):
        return intent, pipeline.publish_intent(
            intent, occurred_at=occurred_at, source=source
        )
    price = as_decimal(reference_price, "reference_price")
    if price <= 0:
        # Invalid intents are published without manufacturing unusable capacity;
        # the venue will emit a rejection in the normal lifecycle.
        return intent, pipeline.publish_intent(
            intent, occurred_at=occurred_at, source=source
        )
    decision_id = intent.risk_decision_id or str(
        stable_uuid5("risk-decision", intent.account, intent.intent_id)
    )
    reservation_id = intent.reservation_id or str(
        stable_uuid5("risk-reservation", intent.account, intent.intent_id)
    )
    enriched = replace(
        intent, risk_decision_id=decision_id, reservation_id=reservation_id
    )
    qty = as_decimal(enriched.requested_qty, "requested_qty")
    decision = RiskDecision(
        decision_id=decision_id,
        account=enriched.account,
        symbol=enriched.symbol,
        action=enriched.action,
        requested_qty=qty,
        approved_qty=qty,
        reference_price=price,
        approved=True,
        reason=reason,
        intent_id=enriched.intent_id,
    )
    reservation = RiskReservation(
        reservation_id=reservation_id,
        risk_decision_id=decision_id,
        intent_id=enriched.intent_id,
        account=enriched.account,
        symbol=enriched.symbol,
        action=enriched.action,
        reserved_qty=qty,
        reference_price=price,
    )
    _, _, intent_event = pipeline.publish_approved_intent(
        decision, reservation, enriched, occurred_at=occurred_at, source=source
    )
    return enriched, intent_event


__all__ = [
    "RiskReservationProjection",
    "ReservationState",
    "ensure_opening_reservation",
]
