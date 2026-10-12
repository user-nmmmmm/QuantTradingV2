"""Ledger event payloads that the core event codec must be able to rebuild.

``CashEvent`` and ``MarkPriceEvent`` are defined here so ``core`` does not import ``research``. They are
persisted under their historical qualified names (see ``core.events.codec``), which are part of the wire
format: do not rename or move them without a migration. ``research.audit.ledger`` re-exports them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

ZERO = Decimal("0")


def as_decimal(value: Any, name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _currency(value: str, name: str = "currency") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip().upper()


@dataclass(frozen=True)
class CashEvent:
    currency: str
    amount: Decimal
    reason: str = "adjustment"
    reference: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", as_decimal(self.amount, "amount"))
        if not self.reason:
            raise ValueError("reason is required")


@dataclass(frozen=True)
class MarkPriceEvent:
    symbol: str
    price: Decimal
    quote_currency: str
    price_time: datetime

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        price = as_decimal(self.price, "price")
        if price <= ZERO:
            raise ValueError("price must be positive")
        if self.price_time.tzinfo is None or self.price_time.utcoffset() is None:
            raise ValueError("price_time must be timezone-aware")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quote_currency", _currency(self.quote_currency))
        object.__setattr__(
            self, "price_time", self.price_time.astimezone(timezone.utc)
        )
