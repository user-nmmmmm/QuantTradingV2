"""Immutable event payload types: StructuredPayload, OrderEvent, FillEvent, etc.

Split out of core/events.py (A4) — see docs/architecture_review.md.
Depends only on core.domain; no codec or ID-generation logic lives here.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Optional, Type
from uuid import UUID

from core.domain import (
    OrderIntent,
    OrderStatus,
    PortfolioSnapshot,
    RiskDecision,
    RiskReservation,
)


def _aware_utc(value: datetime, field_name: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


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
