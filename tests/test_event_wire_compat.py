"""The persisted wire names of ledger events are frozen (infra 0.4).

``CashEvent`` and ``MarkPriceEvent`` were defined in ``research.audit.ledger`` and are written to the
event store under that qualified name. Their definitions live in ``core`` now, but stored events,
their idempotency comparison and ``core.ledger:*`` legacy names must keep working unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.events import EventCodec, TradingEventPipeline
from core.events import codec

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
WIRE = {"CashEvent": "research.audit.ledger:CashEvent", "MarkPriceEvent": "research.audit.ledger:MarkPriceEvent"}


def _payloads():
    from research.audit.ledger import CashEvent, MarkPriceEvent  # public names must keep resolving
    return [CashEvent("USDT", Decimal("100"), reason="deposit"),
            MarkPriceEvent("BTC/USDT", Decimal("100.5"), "USDT", NOW)]


@pytest.mark.parametrize("index", [0, 1])
def test_encoded_class_name_is_unchanged(index):
    payload = _payloads()[index]
    event = TradingEventPipeline(run_id="wire", clock=lambda: NOW).publish(
        payload, event_type="t", occurred_at=NOW, source="test", account_id="a", idempotency_key=f"k{index}")
    document = json.loads(EventCodec.encode(event))
    assert document["payload"]["class"] == WIRE[type(payload).__name__]


@pytest.mark.parametrize("index", [0, 1])
def test_event_roundtrip_and_canonical_names(index):
    payload = _payloads()[index]
    event = TradingEventPipeline(run_id="wire", clock=lambda: NOW).publish(
        payload, event_type="t", occurred_at=NOW, source="test", account_id="a", idempotency_key=f"k{index}")
    assert EventCodec.decode(EventCodec.encode(event)) == event
    # historical spellings still normalize to the persisted name
    for legacy in (f"core.ledger:{type(payload).__name__}", WIRE[type(payload).__name__]):
        wire = {"__qt_type__": "dataclass", "class": legacy, "fields": {}}
        assert codec.canonical_wire_types(wire)["class"] == WIRE[type(payload).__name__]


def test_unregistered_names_are_still_rejected():
    for name in ("research.audit.ledger:PositionState", "os:system", "core.ledger:Other"):
        with pytest.raises(ValueError, match="Unregistered event type"):
            codec._resolve_type(name)
