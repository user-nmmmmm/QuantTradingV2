"""Read authoritative order facts without treating a missing order as canceled."""
from __future__ import annotations

from core.domain import OrderStatus
from core.orders import TERMINAL_STATUSES


def order_fact(execution, order_id):
    venue = getattr(execution, "broker", execution)
    store = getattr(venue, "order_store", None)
    if store is not None:
        return store.get(order_id)
    order = getattr(venue, "orders_by_id", {}).get(order_id)
    if order is None:
        return None
    return {"client_order_id": order.id, "symbol": order.symbol, "side": order.side,
            "strategy_id": order.strategy_id, "status": order.status.value,
            "filled_qty": order.filled_qty, "remaining_qty": order.remaining_qty}


def terminal_fact(row):
    if row is None:
        return False
    try:
        return OrderStatus(row["status"]) in TERMINAL_STATUSES
    except (KeyError, ValueError):
        return False
