"""Pre-submission validation of canonical order intents against venue rules.

Split out of core/exchange_boundary.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from core.domain import OrderIntent
from core.exchange.metadata import (
    ExchangeCapabilities,
    MarketSpecification,
    OrderValidationError,
    _decimal,
)


@dataclass(frozen=True)
class ValidationResult:
    intent: OrderIntent
    market: Optional[MarketSpecification]


class OrderValidator:
    """Validate canonical semantics and venue constraints without I/O."""

    def validate(
        self,
        intent: OrderIntent,
        capabilities: ExchangeCapabilities,
        market: Optional[MarketSpecification],
        *,
        reference_price: Optional[Any] = None,
    ) -> ValidationResult:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        qty = _decimal(intent.requested_qty, "quantity")
        if qty is None or qty <= 0:
            raise OrderValidationError("quantity must be positive and reducible")
        action = str(intent.action).lower()
        if action not in {"buy", "sell", "short", "cover"}:
            raise OrderValidationError("unsupported order side")
        order_type = str(intent.order_type).lower()
        if order_type not in capabilities.order_types:
            raise OrderValidationError(f"unsupported order type: {order_type}")
        if order_type == "limit" and intent.price is None:
            raise OrderValidationError("limit order requires a price")
        if intent.price is not None and _decimal(intent.price, "price") <= 0:
            raise OrderValidationError("price must be positive")
        tif = str(intent.time_in_force).upper() if intent.time_in_force else None
        if tif and tif not in capabilities.time_in_force:
            raise OrderValidationError(f"unsupported time in force: {tif}")
        if intent.reduce_only and not capabilities.supports_reduce_only and market and market.is_derivative:
            raise OrderValidationError("exchange does not support reduce-only orders")
        if intent.position_mode not in {"one_way", "hedge"}:
            raise OrderValidationError("unsupported position mode")
        if intent.position_mode == "hedge" and not capabilities.supports_hedge_mode:
            raise OrderValidationError("exchange does not support hedge mode")
        if market is None:
            return ValidationResult(intent, None)
        if not market.active:
            raise OrderValidationError(f"market is inactive: {intent.symbol}")
        if action in {"short", "cover"} and not market.is_derivative:
            raise OrderValidationError(f"{action} is not supported for spot market {intent.symbol}")
        if market.order_types and order_type not in market.order_types:
            raise OrderValidationError(f"market does not support order type: {order_type}")
        if tif and market.time_in_force and tif not in market.time_in_force:
            raise OrderValidationError(f"market does not support time in force: {tif}")
        self._range(qty, market.min_amount, market.max_amount, "quantity")
        price = _decimal(intent.price, "price", optional=True)
        if price is not None:
            self._range(price, market.min_price, market.max_price, "price")
        notional_price = price or _decimal(reference_price, "reference_price", optional=True)
        if notional_price is not None:
            notional = qty * notional_price * market.contract_size
            self._range(notional, market.min_notional, market.max_notional, "notional")
        elif market.min_notional is not None:
            raise OrderValidationError("reference price is required to validate minimum notional")
        return ValidationResult(intent, market)

    @staticmethod
    def _range(value: Decimal, minimum: Optional[Decimal], maximum: Optional[Decimal], name: str) -> None:
        if minimum is not None and value < minimum:
            raise OrderValidationError(f"{name} {value} is below minimum {minimum}")
        if maximum is not None and value > maximum:
            raise OrderValidationError(f"{name} {value} exceeds maximum {maximum}")
