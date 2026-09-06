"""Passive, opt-in first-block attribution; never evaluates extra signals."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_row: ContextVar[Any] = ContextVar("entry_audit", default=None)


@contextmanager
def capture(row):
    token = _row.set(row)
    try:
        yield
    finally:
        _row.reset(token)


def note(reason=None, **facts):
    row = _row.get()
    if row is not None:
        row.update(facts)
        if reason is not None:
            row["reason"] = reason


def forced_trade_cost(trades):
    """Audit in quote currency: fill slip is per-unit, commission is already total."""
    return sum(float(t.get("commission", 0) or 0)
               + abs(float(t.get("slip", 0) or 0)) * abs(float(t.get("qty", 0) or 0))
               for t in trades)


def reconcile(rows, trades, execution_audit):
    """Count observations separately from orders/fills; exits cannot inflate entries."""
    fills = {}
    for trade in trades:
        if trade.get("side") in ("buy", "short"):
            key = str(trade["order_id"])
            item = fills.setdefault(key, {"fill_count": 0, "filled_qty": 0.0})
            item["fill_count"] += 1
            item["filled_qty"] += float(trade["qty"])
    execution = {}
    for item in execution_audit:
        key = str(item.get("order_id", item.get("client_order_id", "")))
        if key:
            execution[key] = item
    linked = []
    order_ids = []
    for source in rows:
        row = dict(source)
        key = row.get("order_id")
        if key:
            order_ids.append(key)
            row.update(fills.get(key, {"fill_count": 0, "filled_qty": 0.0}))
            row["execution_last_fact"] = execution.get(key)
            row["outcome"] = "filled" if key in fills else "submitted_without_fill"
        else:
            row["outcome"] = row["reason"]
        linked.append(row)
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("Opening order linked to multiple observations")
    unmatched = sorted(set(fills) - set(order_ids))
    counts = dict(sorted(Counter(row["outcome"] for row in linked).items()))
    return linked, {
        "observations": len(linked), "outcomes": counts,
        "partition_ok": sum(counts.values()) == len(linked),
        "opening_orders": len(order_ids), "filled_opening_orders": len(fills),
        "raw_setups": sum(row.get("raw_setup") is True for row in rows),
        "allocation_candidates": sum("rank" in row for row in rows),
        "unmatched_filled_order_ids": unmatched,
        "linkage_ok": not unmatched,
        "scope": "First observed gate per real symbol-bar before termination; unvisited gates are unknown, not profitable missed trades.",
    }
