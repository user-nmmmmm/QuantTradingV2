"""Boundary and compatibility checks for compact event objects."""
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import pickle
from types import MappingProxyType
from uuid import UUID

import pytest

from core.domain import OrderIntent, OrderStatus
from core.events import EventCodec, EventEnvelope, FillEvent, OrderEvent, Signal, TradingEventPipeline
from core.events.ids import _coerce_uuid, _coerce_uuid_string, stable_uuid5
from core.events.types import StructuredPayload, _normalize_value


NOW = datetime(2026, 9, 22, 8, tzinfo=timezone.utc)
OFFSET = timezone(timedelta(hours=8))


def _contract_payloads():
    return [
        OrderEvent("order-1", OrderStatus.PARTIALLY_FILLED, Decimal("1.000"),
                   Decimal("0.100"), Decimal("0.900"), Decimal("50000.0100")),
        FillEvent("fill-1", "order-1", "BTC/USDT", "buy", Decimal("0.100"),
                  Decimal("50000.0100"), Decimal("0.0010"), "USDT"),
        Signal(symbol="BTC/USDT", account="main", timeframe="1d",
               nested={"times": [datetime(2026, 9, 22, 16, tzinfo=OFFSET)],
                       "amount": Decimal("1.00")}),
        OrderIntent("binance", "main", "BTC/USDT", "1d", "2026-09-22",
                    "strategy", "buy", 7, 0.1, signal_id="signal-1"),
        {"symbol": "BTC/USDT", "values": [Decimal("1.00"), 2, 3.5],
         "nested": {"when": datetime(2026, 9, 22, 16, tzinfo=OFFSET)}},
    ]


def _contract_events():
    pipeline = TradingEventPipeline(run_id="event-contract", clock=lambda: NOW)
    # Exercise deterministic payload-derived IDs as well as explicit keys.
    pipeline.subscribe(lambda event: None)
    return [pipeline.publish(payload, occurred_at=NOW, source="test",
                             correlation_id="correlation-contract",
                             causation_id="cause-contract",
                             idempotency_key="explicit-1" if index == 0 else None)
            for index, payload in enumerate(_contract_payloads())]


# Captured from the pre-optimization implementation with the fixed inputs above.
WIRE_SHA256 = [
    "4a8122b401b9e59777601b1501254338693d293ecc20c07722993444d73f6ecb",
    "63144ee840b1ea7cd0eeb92a1bd2d321b226bfc77f0f1378a4fda3399fd67200",
    "fac7f5357778a167d47df24656834a9cf07ca0d2c8f49e5a4af7a731d37d353e",
    "95f01c08779c0acb038622d5c3d93eba82113728b25a76e941f29adb1f624bf7",
    "86374d628c455caf3c5d43473fee559fa729caffa001fa4537783f6ef89e34d5",
]


def test_exact_wire_and_identity_match_previous_implementation():
    for event, expected in zip(_contract_events(), WIRE_SHA256):
        document = EventCodec.encode(event)
        assert hashlib.sha256(document.encode("utf-8")).hexdigest() == expected
        assert EventCodec.decode(document) == event
        assert "_normalized_payload" not in document
        assert "_normalized_payload" not in {item.name for item in fields(event)}


@dataclass
class MutableNestedValue:
    values: object


@pytest.mark.parametrize("structured", [False, True])
def test_publication_copies_nested_mutable_input_and_mapping_proxy(structured):
    sequence = [{"amount": Decimal("1.00")}]
    backing = {"sequence": sequence}
    payload = {"nested": MappingProxyType(backing),
               "record": MutableNestedValue(["initial"])}
    if structured:
        payload = Signal(payload)
    event = TradingEventPipeline(clock=lambda: NOW).publish(payload)
    sequence[0]["amount"] = Decimal("99")
    backing["extra"] = True
    payload["record"].values = ["changed"]
    assert event.payload["nested"]["sequence"][0]["amount"] == Decimal("1.00")
    assert "extra" not in event.payload["nested"]
    assert event.payload["record"].values == ("initial",)
    with pytest.raises(TypeError):
        event.payload["nested"]["sequence"][0]["amount"] = Decimal("5")


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), Decimal("NaN"),
                                    Decimal("Infinity"), datetime(2026, 9, 22)])
def test_public_and_pipeline_boundaries_reject_invalid_nested_values(invalid):
    pipeline = TradingEventPipeline(clock=lambda: NOW)
    with pytest.raises(ValueError):
        pipeline.publish({"nested": [invalid]})
    original = pipeline.publish({"value": 1})
    with pytest.raises(ValueError):
        replace(original, payload={"nested": [invalid]})
    with pytest.raises(ValueError):
        Signal(nested=[invalid])


def test_direct_envelope_and_replace_keep_normalization_and_metadata_validation():
    original = _contract_events()[0]
    supplied = {"times": [datetime(2026, 9, 22, 16, tzinfo=OFFSET)]}
    envelope = EventEnvelope(**{item.name: getattr(original, item.name)
                                for item in fields(original) if item.name != "payload"},
                             payload=supplied)
    supplied["times"].append(NOW)
    assert envelope.payload["times"] == (NOW,)
    changed = replace(envelope, payload={"times": [NOW]})
    assert changed.payload["times"] == (NOW,)
    assert changed.payload is not envelope.payload
    with pytest.raises(ValueError):
        replace(envelope, occurred_at=datetime(2026, 9, 22))
    with pytest.raises(ValueError):
        TradingEventPipeline(clock=lambda: NOW).publish({"valid": True}, source="")
    with pytest.raises(TypeError):
        TradingEventPipeline(clock=lambda: NOW).publish({"valid": True}, account_id=42)


def test_custom_structured_constructor_still_receives_normalized_values():
    seen = []

    class CustomPayload(StructuredPayload):
        def __init__(self, data):
            seen.append(type(data["items"]))
            super().__init__(data)

    payload = CustomPayload({"items": [1, 2]})
    seen.clear()
    normalized = _normalize_value(payload)
    assert seen == [tuple]
    assert type(normalized) is CustomPayload
    assert normalized["items"] == (1, 2)


def test_custom_structured_mapping_view_keeps_its_normalization_contract():
    class CustomView(StructuredPayload):
        def items(self):
            return [("projected", self.data["original"])]

    payload = CustomView(original=[1, 2])
    normalized = _normalize_value(payload)
    assert type(normalized) is CustomView
    assert normalized.data == {"projected": (1, 2)}


class _LegacyPickle:
    """Produce the dict BUILD state written by the old unslotted dataclass."""
    def __init__(self, value):
        self.value = value

    def __reduce__(self):
        return (object.__new__, (type(self.value),),
                {item.name: getattr(self.value, item.name) for item in fields(self.value)})


@pytest.mark.parametrize("index", [0, 1])
def test_slotted_payload_and_envelope_accept_new_and_legacy_pickle_state(index):
    event = _contract_events()[index]
    for value in (event, event.payload):
        assert not hasattr(value, "__dict__")
        assert pickle.loads(pickle.dumps(value)) == value
        assert pickle.loads(pickle.dumps(_LegacyPickle(value))) == value
        assert replace(value) == value


def test_uuid_cache_preserves_identity_scope_passthrough_and_is_bounded():
    _coerce_uuid_string.cache_clear()
    try:
        identity = UUID("319d3de2-89af-5ed8-9733-76ef45c01c41")
        assert _coerce_uuid(identity, "event") is identity
        assert _coerce_uuid_string.cache_info().currsize == 0
        assert _coerce_uuid(str(identity), "event") == identity
        result = _coerce_uuid("signal-1", "correlation")
        assert result == stable_uuid5("correlation", "signal-1")
        assert _coerce_uuid("signal-1", "correlation") is result
        assert _coerce_uuid("signal-1", "causation") != result
        assert _coerce_uuid_string.cache_info().hits == 1
        for index in range(1100):
            _coerce_uuid(f"signal-{index}", "correlation")
        assert _coerce_uuid_string.cache_info().currsize == 1024
        with pytest.raises(TypeError):
            _coerce_uuid("", "correlation")
        with pytest.raises(TypeError):
            _coerce_uuid(42, "correlation")
    finally:
        _coerce_uuid_string.cache_clear()


def test_uuid_string_subclasses_preserve_uncached_behavior():
    class UnhashableString(str):
        __hash__ = None

    _coerce_uuid_string.cache_clear()
    for value in (UnhashableString("signal-1"),
                  UnhashableString("319d3de2-89af-5ed8-9733-76ef45c01c41")):
        assert _coerce_uuid(value, "event") == _coerce_uuid(str(value), "event")
    initial = _coerce_uuid_string.cache_info()
    assert _coerce_uuid("signal-2", UnhashableString("correlation")) == stable_uuid5(
        "correlation", "signal-2")
    assert _coerce_uuid_string.cache_info() == initial
    _coerce_uuid_string.cache_clear()
