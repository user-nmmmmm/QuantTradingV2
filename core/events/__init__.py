"""Event envelope, codec wiring, and the synchronous trading event pipeline.

Facade + orchestration layer over the split event modules (A4) — see
docs/architecture_review.md. Every name that used to live in this module is
re-exported unchanged so existing ``from core.events import ...`` call sites
do not need to move:

- core/event_types.py — StructuredPayload, OrderEvent, FillEvent, etc.
- core/event_codec.py — type-preserving JSON encode/decode of event values
- core/event_ids.py   — deterministic event/correlation/causation UUID5s

``EventEnvelope`` and ``EventCodec`` (encode/decode of the envelope itself),
``EventStore``, and ``TradingEventPipeline`` stay here: they are the piece
that composes all three split modules and did not have a single clean owner
among them.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Type, Union
from uuid import UUID, uuid4, uuid5

from core.domain import OrderIntent, RiskDecision, RiskReservation
from core.events.codec import _decode_value, _encode_value
from core.events.ids import (
    EVENT_NAMESPACE,
    _coerce_uuid,
    causation_id_for,
    correlation_id_for,
    deterministic_causation_id,
    deterministic_correlation_id,
    deterministic_event_id,
    event_id_for,
    stable_uuid5,
)
from core.events.types import (
    FillEvent,
    MarketEvent,
    OrderEvent,
    PAYLOAD_EVENT_TYPES,
    PortfolioSnapshotEvent,
    RiskDecisionEvent,
    Signal,
    StructuredPayload,
    _aware_utc,
    _normalize_value,
    event_type_for,
)

EVENT_SCHEMA_VERSION = "1.0"
EVENT_CODEC_FORMAT = "quant-trading-event/1"


@dataclass(frozen=True)
class EventEnvelope:
    event_id: UUID
    event_type: str
    schema_version: str
    occurred_at: datetime
    observed_at: datetime
    correlation_id: UUID
    causation_id: Optional[UUID]
    run_id: str
    account_id: str
    source: str
    symbol: Optional[str]
    timeframe: Optional[str]
    payload: Any
    idempotency_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type:
            raise ValueError("event_type is required")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("schema_version is required")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id is required")
        if not isinstance(self.account_id, str):
            raise TypeError("account_id must be a string")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source is required")
        if self.idempotency_key is not None and not isinstance(self.idempotency_key, str):
            raise TypeError("idempotency_key must be a string or None")
        object.__setattr__(self, "event_id", _coerce_uuid(self.event_id, "event"))
        object.__setattr__(self, "correlation_id", _coerce_uuid(self.correlation_id, "correlation"))
        if self.causation_id is not None:
            object.__setattr__(self, "causation_id", _coerce_uuid(self.causation_id, "causation"))
        object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "payload", _normalize_value(self.payload))


class EventCodec:
    """Strict JSON codec that preserves event and payload value types."""

    format_version = EVENT_CODEC_FORMAT

    @classmethod
    def encode(cls, event: EventEnvelope) -> str:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be EventEnvelope")
        document = {
            "format": cls.format_version,
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "occurred_at": _encode_value(event.occurred_at),
            "observed_at": _encode_value(event.observed_at),
            "correlation_id": str(event.correlation_id),
            "causation_id": str(event.causation_id) if event.causation_id else None,
            "run_id": event.run_id,
            "account_id": event.account_id,
            "source": event.source,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "payload": _encode_value(event.payload),
            "idempotency_key": event.idempotency_key,
        }
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def decode(cls, document: Union[str, bytes, bytearray]) -> EventEnvelope:
        if isinstance(document, (bytes, bytearray)):
            document = bytes(document).decode("utf-8")
        if not isinstance(document, str):
            raise TypeError("document must be str or UTF-8 bytes")
        raw = json.loads(document)
        if not isinstance(raw, dict) or raw.get("format") != cls.format_version:
            raise ValueError("Unsupported event document format")
        required = {
            "event_id", "event_type", "schema_version", "occurred_at", "observed_at",
            "correlation_id", "run_id", "account_id", "source", "payload",
        }
        missing = required.difference(raw)
        if missing:
            raise ValueError(f"Event document missing fields: {', '.join(sorted(missing))}")
        return EventEnvelope(
            event_id=UUID(raw["event_id"]),
            event_type=raw["event_type"],
            schema_version=raw["schema_version"],
            occurred_at=_decode_value(raw["occurred_at"]),
            observed_at=_decode_value(raw["observed_at"]),
            correlation_id=UUID(raw["correlation_id"]),
            causation_id=UUID(raw["causation_id"]) if raw.get("causation_id") else None,
            run_id=raw["run_id"],
            account_id=raw["account_id"],
            source=raw["source"],
            symbol=raw.get("symbol"),
            timeframe=raw.get("timeframe"),
            payload=_decode_value(raw["payload"]),
            idempotency_key=raw.get("idempotency_key"),
        )


class EventStore(Protocol):
    def append(self, event: EventEnvelope) -> Any:
        ...


Handler = Callable[[EventEnvelope], Any]


class TradingEventPipeline:
    """Synchronous typed event bus with deterministic in-process idempotency."""

    def __init__(
        self,
        *,
        run_id: Optional[Union[str, UUID]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        store: Optional[EventStore] = None,
        schema_version: str = EVENT_SCHEMA_VERSION,
        retention_limit: int = 10000,
    ) -> None:
        self.run_id = str(run_id or uuid4())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = store
        self.schema_version = schema_version
        if retention_limit <= 0:
            raise ValueError("retention_limit must be positive")
        self.retention_limit = int(retention_limit)
        self._subscribers: Dict[str, List[Handler]] = {}
        self._events = deque(maxlen=self.retention_limit)
        self._by_id: Dict[UUID, EventEnvelope] = {}

        self._transaction_lock = RLock()
    @property
    def events(self) -> Tuple[EventEnvelope, ...]:
        return tuple(self._events)

    def subscribe(
        self,
        event_type: Union[str, Type[Any], Handler, None] = None,
        handler: Optional[Handler] = None,
    ):
        """Subscribe to an event type, or pass only a handler for all events."""
        if callable(event_type) and not isinstance(event_type, type) and handler is None:
            handler = event_type
            key = "*"
        else:
            key = self._subscription_key(event_type)
        if handler is None:
            def decorator(callback: Handler):
                return self.subscribe(event_type, callback)
            return decorator
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._subscribers.setdefault(key, []).append(handler)

        def unsubscribe() -> None:
            callbacks = self._subscribers.get(key, [])
            if handler in callbacks:
                callbacks.remove(handler)

        return unsubscribe

    def publish(
        self,
        payload: Any,
        *,
        event_type: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        correlation_id: Optional[Union[str, UUID]] = None,
        causation_id: Optional[Union[str, UUID, EventEnvelope]] = None,
        idempotency_key: Optional[str] = None,
        account_id: str = "",
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        source: str = "runtime",
    ) -> EventEnvelope:
        observed_at = _aware_utc(self._clock(), "clock result")
        normalized_payload = _normalize_value(payload)
        occurred = _aware_utc(occurred_at, "occurred_at") if occurred_at else self._payload_time(
            normalized_payload, observed_at
        )
        resolved_type = event_type or event_type_for(normalized_payload)
        resolved_account = account_id or self._payload_field(normalized_payload, "account") or ""
        resolved_symbol = symbol or self._payload_field(normalized_payload, "symbol")
        resolved_timeframe = timeframe or self._payload_field(normalized_payload, "timeframe")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("idempotency_key must be a non-empty string or None")
        has_consumers = self.store is not None or any(
            self._subscribers.values()
        )
        if idempotency_key is not None:
            # An explicit key already defines business identity. Avoid
            # canonicalizing the complete payload only to derive the UUID.
            event_id = uuid5(
                EVENT_NAMESPACE,
                repr((
                    "idempotent", source, resolved_type,
                    resolved_account, idempotency_key,
                )),
            )
        elif not has_consumers:
            # Ephemeral pipelines without persistence or subscribers need a
            # unique causal handle, not an expensive payload hash.
            event_id = uuid4()
        else:
            event_id = event_id_for(
                source, self.run_id, resolved_type, occurred,
                resolved_account, resolved_symbol, resolved_timeframe,
                normalized_payload,
            )
        correlation = (
            _coerce_uuid(correlation_id, "correlation")
            if correlation_id is not None
            else (
                uuid5(
                    EVENT_NAMESPACE,
                    f"correlation:{self.run_id}:{event_id}",
                )
                if not has_consumers
                else correlation_id_for(self.run_id, event_id)
            )
        )
        if isinstance(causation_id, EventEnvelope):
            causation: Optional[UUID] = causation_id.event_id
        elif causation_id is not None:
            causation = _coerce_uuid(causation_id, "causation")
        else:
            causation = None
        event = EventEnvelope(
            event_id=event_id,
            event_type=resolved_type,
            schema_version=self.schema_version,
            occurred_at=occurred,
            observed_at=observed_at,
            correlation_id=correlation,
            causation_id=causation,
            run_id=self.run_id,
            account_id=resolved_account,
            source=source,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            payload=normalized_payload,
            idempotency_key=idempotency_key,
        )
        return self._accept(event)

    def publish_intent(self, intent: OrderIntent, **metadata: Any) -> EventEnvelope:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        metadata.setdefault("event_type", "order_intent")
        metadata.setdefault("idempotency_key", intent.client_order_id)
        metadata.setdefault("account_id", intent.account)
        metadata.setdefault("symbol", intent.symbol)
        metadata.setdefault("timeframe", intent.timeframe)
        metadata.setdefault("correlation_id", intent.correlation_id)
        if intent.causation_id is not None:
            metadata.setdefault("causation_id", intent.causation_id)
        return self.publish(intent, **metadata)

    def publish_approved_intent(
        self,
        decision: RiskDecision,
        reservation: RiskReservation,
        intent: OrderIntent,
        **metadata: Any,
    ) -> Tuple[EventEnvelope, EventEnvelope, EventEnvelope]:
        """Atomically publish approval, reservation, and its order intent."""
        if not decision.approved:
            raise ValueError("only approved decisions can create a reservation")
        if reservation.risk_decision_id != decision.decision_id:
            raise ValueError("reservation does not belong to decision")
        if reservation.intent_id != intent.intent_id:
            raise ValueError("reservation does not belong to intent")
        occurred_at = metadata.get("occurred_at")
        source = metadata.get("source", "runtime")
        with self._transaction_lock:
            decision_event = self.publish(
                decision,
                occurred_at=occurred_at,
                correlation_id=intent.correlation_id,
                causation_id=metadata.get("causation_id"),
                idempotency_key=decision.decision_id,
                account_id=intent.account,
                symbol=intent.symbol,
                timeframe=intent.timeframe,
                source=source,
            )
            reservation_event = self.publish(
                reservation,
                occurred_at=occurred_at,
                correlation_id=intent.correlation_id,
                causation_id=decision_event,
                idempotency_key=reservation.reservation_id,
                account_id=intent.account,
                symbol=intent.symbol,
                timeframe=intent.timeframe,
                source=source,
            )
            intent_event = self.publish_intent(
                intent,
                occurred_at=occurred_at,
                causation_id=reservation_event,
                source=source,
            )
        return decision_event, reservation_event, intent_event


    def consume(
        self, events: Union[EventEnvelope, Iterable[EventEnvelope]]
    ) -> Union[EventEnvelope, Tuple[EventEnvelope, ...]]:
        single = isinstance(events, EventEnvelope)
        batch = (events,) if single else tuple(events)
        accepted = tuple(self._accept(event) for event in batch)
        return accepted[0] if single else accepted

    def replay(
        self, events: Optional[Iterable[EventEnvelope]] = None
    ) -> Tuple[EventEnvelope, ...]:
        if events is None:
            if self.store is not None and hasattr(self.store, "read"):
                events = getattr(self.store, "read")()
            elif self.store is not None and hasattr(self.store, "read_all"):
                events = getattr(self.store, "read_all")()
            else:
                events = self.events
        replayed = tuple(events)
        for event in replayed:
            if not isinstance(event, EventEnvelope):
                raise TypeError("replay accepts only EventEnvelope values")
            self._dispatch(event)
        return replayed

    def _accept(self, event: EventEnvelope) -> EventEnvelope:
        if not isinstance(event, EventEnvelope):
            raise TypeError("consume accepts only EventEnvelope values")
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            # Observation time is transport metadata, not business identity.
            existing_doc = json.loads(EventCodec.encode(existing))
            candidate_doc = json.loads(EventCodec.encode(event))
            existing_doc["observed_at"] = candidate_doc["observed_at"]
            if existing_doc != candidate_doc:
                raise ValueError(f"Idempotency conflict for event_id={event.event_id}")
            return existing
        if self.store is not None:
            self.store.append(event)
        evicted_id = (
            self._events[0].event_id
            if len(self._events) == self.retention_limit
            else None
        )
        self._by_id[event.event_id] = event
        self._events.append(event)
        if evicted_id is not None and evicted_id != event.event_id:
            self._by_id.pop(evicted_id, None)
        self._dispatch(event)
        return event

    def _dispatch(self, event: EventEnvelope) -> None:
        callbacks = tuple(self._subscribers.get(event.event_type, ())) + tuple(
            self._subscribers.get("*", ())
        )
        for callback in callbacks:
            callback(event)

    @staticmethod
    def _subscription_key(event_type: Union[str, Type[Any], None]) -> str:
        if event_type is None or event_type == "*":
            return "*"
        if isinstance(event_type, str):
            if not event_type:
                raise ValueError("event_type cannot be empty")
            return event_type
        if isinstance(event_type, type):
            for payload_type, name in PAYLOAD_EVENT_TYPES.items():
                if issubclass(event_type, payload_type):
                    return name
        raise TypeError("event_type must be string, registered payload type, or None")

    @staticmethod
    def _payload_field(payload: Any, name: str) -> Any:
        if isinstance(payload, Mapping):
            return payload.get(name)
        return getattr(payload, name, None)

    @classmethod
    def _payload_time(cls, payload: Any, fallback: datetime) -> datetime:
        for name in ("occurred_at", "bar_time", "timestamp", "synced_at"):
            value = cls._payload_field(payload, name)
            if isinstance(value, datetime):
                return _aware_utc(value, name)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return _aware_utc(parsed, name)
                except (TypeError, ValueError):
                    continue
        return fallback


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EVENT_NAMESPACE",
    "EventEnvelope",
    "EventCodec",
    "StructuredPayload",
    "MarketEvent",
    "Signal",
    "RiskDecisionEvent",
    "OrderEvent",
    "FillEvent",
    "PortfolioSnapshotEvent",
    "TradingEventPipeline",
    "event_type_for",
    "stable_uuid5",
    "event_id_for",
    "correlation_id_for",
    "causation_id_for",
    "deterministic_event_id",
    "deterministic_correlation_id",
    "deterministic_causation_id",
]
