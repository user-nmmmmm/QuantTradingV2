"""Deterministic amount/price quantization to market increments.

Split out of core/exchange_boundary.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from core.domain import OrderIntent
from core.exchange.metadata import MarketSpecification, OrderValidationError, _decimal


class OrderNormalizer:
    """Quantize amount and price deterministically using market increments."""

    def normalize(self, intent: OrderIntent, market: Optional[MarketSpecification]) -> OrderIntent:
        if market is None:
            return intent
        qty = self._floor(_decimal(intent.requested_qty, "quantity"), market.amount_step)
        price = _decimal(intent.price, "price", optional=True)
        if price is not None:
            price = self._floor(price, market.price_step)
        if qty <= 0:
            raise OrderValidationError("quantity normalizes to zero")
        if price is not None and price <= 0:
            raise OrderValidationError("price normalizes to zero")
        return replace(
            intent,
            requested_qty=float(qty),
            price=None if price is None else float(price),
            order_type=str(intent.order_type).lower(),
            time_in_force=(str(intent.time_in_force).upper() if intent.time_in_force else None),
        )

    @staticmethod
    def _floor(value: Decimal, step: Optional[Decimal]) -> Decimal:
        if step is None:
            return value
        units = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step
