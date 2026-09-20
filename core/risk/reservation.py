"""Event-sourced risk reservations shared by simulated and live execution."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import RLock
from typing import Any, Dict, Iterator, Optional, Set, Tuple

from core.domain import OrderIntent, OrderStatus, RiskDecision, RiskReservation
from core.entry_risk import resolve_approved_risk
from core.events import (
    EventEnvelope,
    FillEvent,
    OrderEvent,
    TradingEventPipeline,
    stable_uuid5,
)


OPENING_ACTIONS = {"buy", "short"}


class OpeningRiskRejected(ValueError):
    """A final, atomic admission check rejected a new order."""

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

    def pending_cash(self, current_prices=None, *, cost=None) -> float:
        """Spot cash for pending buys; short notional is exposure, not cash."""
        prices = current_prices or {}
        total = 0.0
        with self._lock:
            for state in self._states.values():
                if state.released or state.remaining_qty <= 0 or state.reservation.action != "buy":
                    continue
                item = state.reservation
                price = max(float(item.reference_price), float(prices.get(item.symbol, 0)))
                qty = float(state.remaining_qty)
                total += qty * price + (cost(item.symbol, qty, price) if cost else 0.0)
        return total

    def remaining_qty(self, reservation_id: str) -> Decimal:
        with self._lock:
            state = self._states.get(reservation_id)
            return Decimal("0") if state is None else state.remaining_qty

    def pending_stop_risk(self, execution_cost=None, *, exclude_intent_id=None) -> Dict[str, float]:
        """Remaining approved risk only; fills transfer it into open lot risk.

        UNKNOWN and CANCEL_PENDING keep occupying capacity until an
        authoritative terminal event. Missing approvals are never zero risk.
        """
        totals: Dict[str, float] = {}
        with self._lock:
            for state in self._states.values():
                if state.released or state.remaining_qty <= 0:
                    continue
                item = state.reservation
                if item.intent_id == exclude_intent_id:
                    continue
                if item.approved_risk_amount is None:
                    raise ValueError(f"missing_approval:{item.intent_id}")
                qty = float(state.remaining_qty)
                risk = float(item.approved_risk_amount * state.remaining_qty / item.reserved_qty)
                if execution_cost is not None:
                    risk += execution_cost(item.symbol, qty, float(item.reference_price))
                totals[item.symbol] = totals.get(item.symbol, 0.0) + risk
        return totals

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
    guard=None,
) -> Tuple[OrderIntent, EventEnvelope]:
    """Idempotently enrich and publish an opening intent with its reservation."""
    if guard is not None and intent.action in OPENING_ACTIONS and not intent.reduce_only:
        with guard.broker.reservation_projection.transaction():
            rejection = guard.check_intent(intent)
            if rejection:
                raise OpeningRiskRejected(rejection)
            return ensure_opening_reservation(pipeline, intent, reference_price=reference_price,
                occurred_at=occurred_at, source=source, reason=reason)
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
        intent, risk_decision_id=decision_id, reservation_id=reservation_id,
        approved_risk_amount=(
            resolve_approved_risk({
                "approved_risk_amount": intent.approved_risk_amount,
                "requested_qty": intent.requested_qty,
                "reference_price": intent.reference_price or float(price),
                "initial_stop": intent.initial_stop,
            })[0]
            if intent.approved_risk_amount is not None or intent.initial_stop
            else None
        ),
    )
    qty = as_decimal(enriched.requested_qty, "requested_qty")
    approved_risk = (
        as_decimal(enriched.approved_risk_amount, "approved_risk_amount")
        if enriched.approved_risk_amount is not None else None
    )
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
        approved_risk_amount=approved_risk,
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
        approved_risk_amount=approved_risk,
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
