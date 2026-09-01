"""Mapping from canonical order intents to CCXT request keyword arguments.

Split out of core/exchange_boundary.py (A4) — see docs/architecture_review.md.
This is the only module that should know CCXT's ``create_order`` call shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from core.domain import OrderIntent
from core.exchange.metadata import ExchangeCapabilities


@dataclass(frozen=True)
class CCXTOrderRequest:
    symbol: str
    type: str
    side: str
    amount: float
    price: Optional[float]
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_kwargs(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": self.type,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "params": dict(self.params),
        }


class CCXTRequestMapper:
    """The only order-request mapping from domain language to CCXT language."""

    def map(self, intent: OrderIntent, capabilities: ExchangeCapabilities) -> CCXTOrderRequest:
        params: Dict[str, Any] = {}
        if capabilities.supports_client_order_id:
            params["clientOrderId"] = intent.client_order_id
        if intent.reduce_only:
            params["reduceOnly"] = True
        if intent.position_side:
            params["positionSide"] = intent.position_side
        if intent.time_in_force:
            params["timeInForce"] = str(intent.time_in_force).upper()
        side = {"short": "sell", "cover": "buy"}.get(intent.action, intent.action)
        return CCXTOrderRequest(
            symbol=intent.symbol,
            type=str(intent.order_type).lower(),
            side=side,
            amount=float(intent.requested_qty),
            price=None if intent.price is None else float(intent.price),
            params=params,
        )
