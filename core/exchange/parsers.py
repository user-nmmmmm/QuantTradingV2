"""Parse heterogeneous CCXT order/position payloads into canonical facts.

Split out of core/exchange_boundary.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional, Tuple

from core.domain import OrderStatus
from core.exchange.metadata import _decimal, _mapping


@dataclass(frozen=True)
class CanonicalOrder:
    exchange_order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: Optional[str]
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal
    remaining_qty: Decimal
    average_fill_price: Optional[Decimal]


class OrderParser:
    """Parse heterogeneous CCXT order payloads into a canonical order fact."""

    def parse(self, payload: Mapping[str, Any], *, requested_qty: Any = 0) -> CanonicalOrder:
        if not isinstance(payload, Mapping):
            raise ValueError("order payload must be a mapping")
        amount_value = payload.get("amount")
        filled_value = payload.get("filled")
        requested = _decimal(
            requested_qty if amount_value in (None, "") else amount_value, "amount"
        ) or Decimal("0")
        filled = _decimal(0 if filled_value in (None, "") else filled_value, "filled") or Decimal("0")
        remaining_raw = payload.get("remaining")
        remaining = max(requested - filled, Decimal("0")) if remaining_raw is None else (_decimal(remaining_raw, "remaining") or Decimal("0"))
        raw_status = str(payload.get("status") or "").lower()
        if raw_status in {"closed", "filled"} or (requested > 0 and filled >= requested):
            status = OrderStatus.FILLED
            remaining = Decimal("0")
        elif raw_status == "expired":
            status = OrderStatus.EXPIRED
        elif raw_status in {"canceled", "cancelled"}:
            status = OrderStatus.CANCELED
        elif raw_status in {"rejected", "failed"}:
            status = OrderStatus.REJECTED
        elif filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        elif raw_status in {"open", "new", "accepted", "pending"}:
            status = OrderStatus.ACCEPTED
        elif remaining == 0 and requested > 0:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.UNKNOWN
        average = _decimal(payload.get("average") or payload.get("price"), "average", optional=True)
        return CanonicalOrder(
            exchange_order_id=None if payload.get("id") is None else str(payload.get("id")),
            client_order_id=_first_text(payload, "clientOrderId", "client_order_id"),
            symbol=_first_text(payload, "symbol"),
            status=status,
            requested_qty=requested,
            filled_qty=filled,
            remaining_qty=remaining,
            average_fill_price=average if average and average > 0 else None,
        )


@dataclass(frozen=True)
class CanonicalPosition:
    symbol: str
    qty: Decimal
    average_entry_price: Decimal = Decimal("0")
    position_side: Optional[str] = None


class PositionParser:
    """Parse signed and side+magnitude position formats without venue branches."""

    def parse(self, payload: Mapping[str, Any]) -> Optional[CanonicalPosition]:
        if not isinstance(payload, Mapping):
            raise ValueError("position payload must be a mapping")
        info = _mapping(payload.get("info"))
        symbol = _first_text(payload, "symbol") or _first_text(info, "symbol")
        if not symbol:
            raise ValueError("position symbol is unavailable")
        direction, position_side = self._direction(payload, info)
        qty: Optional[Decimal] = None
        for source in (payload, info):
            if source.get("positionAmt") not in (None, ""):
                qty = _decimal(source.get("positionAmt"), "positionAmt")
                if qty and direction is not None and (qty > 0) != (direction > 0):
                    raise ValueError("derivative position side conflicts with signed quantity")
                break
        if qty is None:
            for name in ("contracts", "qty", "size"):
                value = payload.get(name, info.get(name))
                if value in (None, ""):
                    continue
                magnitude = abs(_decimal(value, name) or Decimal("0"))
                if magnitude and direction is None:
                    raise ValueError("derivative position direction is unavailable")
                qty = magnitude * (direction or Decimal("1"))
                break
        qty = qty or Decimal("0")
        if qty == 0:
            return None
        average = Decimal("0")
        for name in ("entryPrice", "avgPrice", "average"):
            value = payload.get(name, info.get(name))
            candidate = _decimal(value, name, optional=True)
            if candidate is not None and candidate > 0:
                average = candidate
                break
        return CanonicalPosition(symbol, qty, average, position_side)

    @staticmethod
    def _direction(payload: Mapping[str, Any], info: Mapping[str, Any]) -> Tuple[Optional[Decimal], Optional[str]]:
        for name in ("side", "positionSide"):
            value = payload.get(name, info.get(name))
            normalized = str(value or "").strip().lower()
            if normalized in {"long", "buy"}:
                return Decimal("1"), normalized
            if normalized in {"short", "sell"}:
                return Decimal("-1"), normalized
        return None, None


def _first_text(payload: Mapping[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return None
