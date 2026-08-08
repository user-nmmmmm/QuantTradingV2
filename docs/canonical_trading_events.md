# Canonical Trading Events (P1.1)

core.events.EventEnvelope is the wire and persistence boundary for both backtest
and live trading. The payload is one of the registered domain types, including
OrderIntent, OrderEvent, and FillEvent.

## Time semantics

- occurred_at is event time: when the market, strategy, risk, order, or fill fact
  happened.
- observed_at is processing time: when this process first observed the fact.
- Both values must be timezone-aware. The envelope normalizes them to UTC.
- Legacy broker adapters interpret naive backtest timestamps as UTC at their
  boundary; canonical envelopes never contain naive datetimes.

## Identity and causality

IDs are UUIDv5 values derived from canonical JSON under a fixed namespace.
Canonical JSON sorts mapping keys and preserves Decimal, datetime, UUID, Enum,
tuple, and dataclass types. event_id_for, correlation_id_for, and
causation_id_for are purpose-scoped so equal source values cannot collide
across ID roles.

correlation_id is constant across one signal-to-fill business flow.
causation_id identifies the immediately preceding event. An idempotency key
produces the same event ID across retries; a conflicting payload is rejected.

## Numeric and schema contract

Financial quantities in OrderEvent and FillEvent are serialized as exact
base-10 Decimal strings, never JSON binary floats. Datetimes serialize as
ISO-8601 UTC values ending in Z. Every envelope carries schema_version;
the outer codec format is quant-trading-event/1. Readers reject unknown outer
formats and missing required envelope fields.

TradingEventPipeline.consume accepts the same EventEnvelope instances from
backtest and live producers. EventCodec is the supported JSON round-trip
boundary for persistence and transport.
