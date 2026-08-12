from __future__ import annotations

from typing import Any, Dict

from core.domain import OrderErrorCode, OrderStatus


_ALLOWED = {
    OrderStatus.CREATED: {
        OrderStatus.SUBMITTING, OrderStatus.REJECTED, OrderStatus.EXPIRED,
    },
    OrderStatus.SUBMITTING: {
        OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
        OrderStatus.EXPIRED_UNSUBMITTED, OrderStatus.UNKNOWN,
    },
    OrderStatus.UNKNOWN: {
        OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
        OrderStatus.EXPIRED_UNSUBMITTED,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING, OrderStatus.CANCELED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.UNKNOWN,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING, OrderStatus.CANCELED, OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCELED, OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    # A late fill may race with cancel acknowledgement.
    OrderStatus.CANCELED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
    # Expiry is terminal for the order lifecycle, but exchange truth wins if
    # a late fill arrives after the expiry acknowledgement.
    OrderStatus.EXPIRED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
    # An operator can be wrong. A later authoritative fill must still win.
    OrderStatus.EXPIRED_UNSUBMITTED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
    },
}

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.EXPIRED_UNSUBMITTED,
}


def normalize_exchange_status(payload: Dict[str, Any]) -> OrderStatus:
    raw = str(payload.get("status") or "").lower()
    filled = _float(payload.get("filled"))
    amount = _float(payload.get("amount"))
    remaining = payload.get("remaining")
    if raw in {"closed", "filled"} or (amount > 0 and filled >= amount):
        return OrderStatus.FILLED
    # Expired orders remain terminal even when the exchange also reports a
    # partial fill. The persisted fills carry the executed quantity; treating
    # this response as PARTIALLY_FILLED would keep it in reconciliation forever.
    if raw == "expired":
        return OrderStatus.EXPIRED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    if raw in {"open", "new", "accepted", "pending"}:
        return OrderStatus.ACCEPTED
    if raw in {"canceled", "cancelled"}:
        return OrderStatus.CANCELED
    if raw in {"rejected", "failed"}:
        return OrderStatus.REJECTED
    if remaining == 0 and amount > 0:
        return OrderStatus.FILLED
    return OrderStatus.UNKNOWN


def classify_order_exception(exc: BaseException) -> OrderErrorCode:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return OrderErrorCode.TIMEOUT
    if "ratelimit" in name or "rate limit" in text or "too many requests" in text:
        return OrderErrorCode.RATE_LIMIT
    if "authentication" in name or "invalid key" in text or "signature" in text:
        return OrderErrorCode.AUTHENTICATION
    if "insufficient" in name or "insufficient" in text or "balance" in text:
        return OrderErrorCode.INSUFFICIENT_FUNDS
    if "network" in name or "connection" in text or "dns" in text:
        return OrderErrorCode.NETWORK
    if "unavailable" in name or "maintenance" in text or "service unavailable" in text:
        return OrderErrorCode.EXCHANGE_UNAVAILABLE
    if any(token in name or token in text for token in ("invalidorder", "badrequest", "precision", "minimum")):
        return OrderErrorCode.TRADING_RULE
    return OrderErrorCode.UNKNOWN


def is_ambiguous_error(code: OrderErrorCode) -> bool:
    return code in {
        OrderErrorCode.NETWORK,
        OrderErrorCode.TIMEOUT,
        OrderErrorCode.RATE_LIMIT,
        OrderErrorCode.EXCHANGE_UNAVAILABLE,
        OrderErrorCode.UNKNOWN,
    }


def validate_transition(current: str, target: OrderStatus) -> None:
    source = OrderStatus(current)
    if source == target:
        return
    if target not in _ALLOWED.get(source, set()):
        raise ValueError(f"Invalid order transition: {source.value} -> {target.value}")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default
