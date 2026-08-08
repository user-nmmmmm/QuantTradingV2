from __future__ import annotations

import importlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Type, Union
from uuid import UUID, uuid4, uuid5

from core.domain import (
    OrderIntent,
    OrderStatus,
    PortfolioSnapshot,
    RiskDecision,
    RiskReservation,
)


EVENT_SCHEMA_VERSION = "1.0"
EVENT_CODEC_FORMAT = "quant-trading-event/1"
EVENT_NAMESPACE = UUID("319d3de2-89af-5ed8-9733-76ef45c01c41")


def _aware_utc(value: datetime, field_name: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _qualified_name(value: Union[Type[Any], Any]) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}:{cls.__qualname__}"


def _resolve_type(name: str) -> Type[Any]:
    if not isinstance(name, str) or ":" not in name:
        raise ValueError("Invalid qualified type name")
    module_name, qualname = name.split(":", 1)
    if not module_name or not qualname or "<locals>" in qualname:
        raise ValueError(f"Unsupported qualified type: {name!r}")
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    if not isinstance(value, type):
        raise TypeError(f"Resolved value is not a type: {name!r}")
    return value


def _normalize_value(value: Any) -> Any:
    """Deep-freeze event data while normalizing every datetime to UTC."""
    if value is None or isinstance(value, (str, bool, int, UUID, Enum)):
        return value
    if isinstance(value, datetime):
        return _aware_utc(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal NaN and Infinity are not valid event values")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not valid event values")
        return value
    if isinstance(value, StructuredPayload):
        return type(value)({key: _normalize_value(item) for key, item in value.items()})
    if is_dataclass(value) and not isinstance(value, type):
        values = {item.name: _normalize_value(getattr(value, item.name)) for item in fields(value)}
        return type(value)(**values)
    if isinstance(value, Mapping):
        normalized: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Event mapping keys must be strings")
            normalized[key] = _normalize_value(item)
        return MappingProxyType(normalized)
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_value(item) for item in value)
    raise TypeError(f"Unsupported event value type: {type(value).__name__}")


@dataclass(frozen=True, init=False)
class StructuredPayload(Mapping[str, Any]):
    """Immutable, schema-friendly payload with attribute and mapping access.

    Payload subclasses intentionally accept additional fields. That keeps the
    envelope stable while individual event schemas evolve under their own
    ``schema_version``.
    """

    data: Mapping[str, Any]

    def __init__(self, data: Optional[Mapping[str, Any]] = None, **values: Any) -> None:
        if data is not None and not isinstance(data, Mapping):
            raise TypeError("payload data must be a mapping")
        merged = dict(data or {})
        overlap = set(merged).intersection(values)
        if overlap:
            raise ValueError(f"Duplicate payload fields: {', '.join(sorted(overlap))}")
        merged.update(values)
        object.__setattr__(self, "data", _normalize_value(merged))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __iter__(self):
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.data)


class MarketEvent(StructuredPayload):
    """Market fact; conventional fields include symbol/timeframe/bar_time/OHLCV."""


class Signal(StructuredPayload):
    """Strategy signal; conventional fields include strategy_id/action/strength."""


class RiskDecisionEvent(StructuredPayload):
    """Risk decision; conventional fields include approved/reason/approved_qty."""


def _decimal(value: Any, field_name: str) -> Decimal:
    """Convert a boundary numeric value without introducing binary-float noise."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class OrderEvent:
    """Canonical order-lifecycle fact shared by simulated and live venues."""

    client_order_id: str
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    remaining_qty: Decimal = Decimal("0")
    average_fill_price: Optional[Decimal] = None
    exchange_order_id: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("client_order_id is required")
        object.__setattr__(self, "status", OrderStatus(self.status))
        for name in ("requested_qty", "filled_qty", "remaining_qty"):
            value = _decimal(getattr(self, name), name)
            if name != "requested_qty" and value < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if self.average_fill_price is not None:
            price = _decimal(self.average_fill_price, "average_fill_price")
            if price <= 0:
                raise ValueError("average_fill_price must be positive")
            object.__setattr__(self, "average_fill_price", price)


@dataclass(frozen=True)
class FillEvent:
    """Canonical immutable execution fact with exact decimal quantities."""

    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    fee_currency: Optional[str] = None
    exchange_order_id: Optional[str] = None
    liquidity: Optional[str] = None
    quote_currency: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.fill_id or not self.client_order_id:
            raise ValueError("fill_id and client_order_id are required")
        if not self.symbol or not self.side:
            raise ValueError("symbol and side are required")
        for name in ("qty", "price", "fee"):
            value = _decimal(getattr(self, name), name)
            if value < 0 or (name in {"qty", "price"} and value == 0):
                qualifier = "positive" if name in {"qty", "price"} else "non-negative"
                raise ValueError(f"{name} must be {qualifier}")
            object.__setattr__(self, name, value)


class PortfolioSnapshotEvent(StructuredPayload):
    """Typed event projection for a ``PortfolioSnapshot``."""

    @classmethod
    def from_snapshot(cls, snapshot: PortfolioSnapshot) -> "PortfolioSnapshotEvent":
        if not isinstance(snapshot, PortfolioSnapshot):
            raise TypeError("snapshot must be PortfolioSnapshot")
        return cls(
            cash=snapshot.cash,
            equity=snapshot.equity,
            gross_exposure=snapshot.gross_exposure,
            net_exposure=snapshot.net_exposure,
            prices=snapshot.prices,
            price_times=snapshot.price_times,
            synced_at=snapshot.synced_at,
            positions=snapshot.positions,
            cash_balances=snapshot.cash_balances,
            realized_pnl=snapshot.realized_pnl,
            unrealized_pnl=snapshot.unrealized_pnl,
            fees=snapshot.fees,
            base_currency=snapshot.base_currency,
            last_sequence=snapshot.last_sequence,
        )


PAYLOAD_EVENT_TYPES: Dict[Type[Any], str] = {
    MarketEvent: "market",
    Signal: "signal",
    RiskDecisionEvent: "risk_decision",
    RiskDecision: "risk_decision",
    RiskReservation: "risk_reservation",
    OrderIntent: "order_intent",
    OrderEvent: "order",
    FillEvent: "fill",
    PortfolioSnapshot: "portfolio_snapshot",
    PortfolioSnapshotEvent: "portfolio_snapshot",
}


def event_type_for(payload: Any) -> str:
    for payload_type, event_type in PAYLOAD_EVENT_TYPES.items():
        if isinstance(payload, payload_type):
            return event_type
    if is_dataclass(payload) and not isinstance(payload, type):
        return type(payload).__name__
    if isinstance(payload, Mapping):
        return "generic"
    raise TypeError(f"No event type registered for {type(payload).__name__}")


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not valid event values")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal NaN and Infinity are not valid event values")
        return {"__qt_type__": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        utc_value = _aware_utc(value)
        return {"__qt_type__": "datetime", "value": utc_value.isoformat().replace("+00:00", "Z")}
    if isinstance(value, UUID):
        return {"__qt_type__": "uuid", "value": str(value)}
    if isinstance(value, Enum):
        return {
            "__qt_type__": "enum",
            "class": _qualified_name(value),
            "value": _encode_value(value.value),
        }
    if isinstance(value, StructuredPayload):
        return {
            "__qt_type__": "structured_payload",
            "class": _qualified_name(value),
            "data": _encode_value(value.data),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__qt_type__": "dataclass",
            "class": _qualified_name(value),
            "fields": {
                item.name: _encode_value(getattr(value, item.name))
                for item in fields(value)
            },
        }
    if isinstance(value, Mapping):
        items = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("Event mapping keys must be strings")
            items.append([key, _encode_value(value[key])])
        return {"__qt_type__": "mapping", "items": items}
    if isinstance(value, tuple):
        return {"__qt_type__": "tuple", "items": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return {"__qt_type__": "list", "items": [_encode_value(item) for item in value]}
    raise TypeError(f"Unsupported event value type: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict) or "__qt_type__" not in value:
        if isinstance(value, list):
            return [_decode_value(item) for item in value]
        return value
    tag = value.get("__qt_type__")
    if tag == "decimal":
        result = Decimal(value["value"])
        if not result.is_finite():
            raise ValueError("Decimal NaN and Infinity are not valid event values")
        return result
    if tag == "datetime":
        raw = value["value"]
        if not isinstance(raw, str):
            raise TypeError("Encoded datetime must be a string")
        return _aware_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    if tag == "uuid":
        return UUID(value["value"])
    if tag == "enum":
        cls = _resolve_type(value["class"])
        if not issubclass(cls, Enum):
            raise TypeError("Encoded enum class is not an Enum")
        return cls(_decode_value(value["value"]))
    if tag == "mapping":
        result: Dict[str, Any] = {}
        for item in value.get("items", ()):
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                raise ValueError("Invalid encoded mapping item")
            if item[0] in result:
                raise ValueError(f"Duplicate encoded mapping key: {item[0]}")
            result[item[0]] = _decode_value(item[1])
        return result
    if tag in {"tuple", "list"}:
        decoded = [_decode_value(item) for item in value.get("items", ())]
        return tuple(decoded) if tag == "tuple" else decoded
    if tag == "structured_payload":
        cls = _resolve_type(value["class"])
        if not issubclass(cls, StructuredPayload):
            raise TypeError("Encoded payload class is not StructuredPayload")
        data = _decode_value(value["data"])
        return cls(data)
    if tag == "dataclass":
        cls = _resolve_type(value["class"])
        if not is_dataclass(cls):
            raise TypeError("Encoded class is not a dataclass")
        raw_fields = value.get("fields")
        if not isinstance(raw_fields, dict):
            raise TypeError("Encoded dataclass fields must be an object")
        return cls(**{key: _decode_value(item) for key, item in raw_fields.items()})
    raise ValueError(f"Unknown encoded event value type: {tag!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _encode_value(_normalize_value(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_uuid5(purpose: str, *parts: Any) -> UUID:
    if not purpose:
        raise ValueError("purpose is required")
    name = canonical_json({"purpose": purpose, "parts": tuple(parts)})
    return uuid5(EVENT_NAMESPACE, name)


def event_id_for(*parts: Any) -> UUID:
    return stable_uuid5("event", *parts)


def correlation_id_for(*parts: Any) -> UUID:
    return stable_uuid5("correlation", *parts)


def causation_id_for(*parts: Any) -> UUID:
    return stable_uuid5("causation", *parts)


# Explicit aliases make the deterministic contract easy to discover.
deterministic_event_id = event_id_for
deterministic_correlation_id = correlation_id_for
deterministic_causation_id = causation_id_for


def _coerce_uuid(value: Union[str, UUID], purpose: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value:
        raise TypeError(f"{purpose} must be UUID or non-empty string")
    try:
        return UUID(value)
    except ValueError:
        return stable_uuid5(purpose, value)


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
    ) -> None:
        self.run_id = str(run_id or uuid4())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = store
        self.schema_version = schema_version
        self._subscribers: Dict[str, List[Handler]] = {}
        self._events: List[EventEnvelope] = []
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
        identity = (
            (source, resolved_type, resolved_account, idempotency_key)
            if idempotency_key is not None
            else (
                source, self.run_id, resolved_type, occurred, resolved_account,
                resolved_symbol, resolved_timeframe, normalized_payload,
            )
        )
        event_id = event_id_for(*identity)
        correlation = (
            _coerce_uuid(correlation_id, "correlation")
            if correlation_id is not None
            else correlation_id_for(self.run_id, event_id)
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
        self._by_id[event.event_id] = event
        self._events.append(event)
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
