"""Deterministic UUID5 generation for event/correlation/causation identity.

Split out of core/events.py (A4) — see docs/architecture_review.md.
Depends on core.events.codec.canonical_json to hash payload content.
"""
from __future__ import annotations

from typing import Any, Union
from uuid import UUID, uuid5

from core.events.codec import canonical_json

EVENT_NAMESPACE = UUID("319d3de2-89af-5ed8-9733-76ef45c01c41")


def stable_uuid5(purpose: str, *parts: Any) -> UUID:
    if not purpose:
        raise ValueError("purpose is required")
    name = canonical_json({"purpose": purpose, "parts": tuple(parts)})
    return uuid5(EVENT_NAMESPACE, name)


def event_id_for(*parts: Any) -> UUID:
    return stable_uuid5("event", *parts)


def correlation_id_for(*parts: Any) -> UUID:
    return stable_uuid5("correlation", *parts)


def causation_id_for(*parts: Any) -> UUID:
    return stable_uuid5("causation", *parts)


# Explicit aliases make the deterministic contract easy to discover.
deterministic_event_id = event_id_for
deterministic_correlation_id = correlation_id_for
deterministic_causation_id = causation_id_for


def _coerce_uuid(value: Union[str, UUID], purpose: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value:
        raise TypeError(f"{purpose} must be UUID or non-empty string")
    try:
        return UUID(value)
    except ValueError:
        return stable_uuid5(purpose, value)
