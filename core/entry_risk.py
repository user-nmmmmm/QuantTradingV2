"""Recover entry approvals from immutable order facts, never current equity."""
from __future__ import annotations

import math
from typing import Any, Mapping


def resolve_approved_risk(intent: Mapping[str, Any]) -> tuple[float, str]:
    """Return the whole order's approved stop-loss amount in quote currency.

    Legacy orders can be reconstructed only from their original requested
    quantity, reference price and initial stop. A present but invalid approval
    is an error; it must never fall back to a more permissive budget.
    """
    amount = intent.get("approved_risk_amount")
    if amount is not None:
        return _positive(amount, "approved_risk_amount"), "order_approval"
    qty = _positive(intent.get("requested_qty"), "requested_qty")
    reference = intent.get("reference_price")
    if reference is None:
        reference = intent.get("price")
    price = _positive(reference, "reference_price")
    stop = _positive(intent.get("initial_stop"), "initial_stop")
    return _positive(qty * abs(price - stop), "reconstructed_risk"), "legacy_order_reference"


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number
