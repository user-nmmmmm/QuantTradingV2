import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd

from core.broker import Broker
from core.domain import OrderIntent, OrderStatus
from core.events import (
    EVENT_SCHEMA_VERSION,
    EventCodec,
    EventEnvelope,
    FillEvent,
    OrderEvent,
    TradingEventPipeline,
    causation_id_for,
    correlation_id_for,
    event_id_for,
)
from core.live_broker import LiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.safe_live_broker import SafeLiveBroker


UTC_NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)


def intent() -> OrderIntent:
    return OrderIntent(
        exchange="binance",
        account="sandbox",
        symbol="BTC/USDT",
        timeframe="1m",
        bar_time="2026-08-06T08:00:00Z",
        strategy_id="contract",
        action="buy",
        sequence=7,
        requested_qty=1,
        order_type="market",
        signal_id="signal-contract-7",
        causation_id="risk-contract-7",
        created_at="2026-08-06T08:00:00Z",
    )


class TestEventEnvelopeAndSerialization(unittest.TestCase):
    def test_time_semantics_require_aware_values_and_normalize_to_utc(self):
        offset = timezone(timedelta(hours=8))
        envelope = EventEnvelope(
            event_id=event_id_for("time"),
            event_type="order",
            schema_version=EVENT_SCHEMA_VERSION,
            occurred_at=datetime(2026, 8, 6, 16, 0, tzinfo=offset),
            observed_at=datetime(2026, 8, 6, 16, 0, 1, tzinfo=offset),
            correlation_id=correlation_id_for("time"),
            causation_id=None,
            run_id="run",
            account_id="account",
            source="test",
            symbol=None,
            timeframe=None,
            payload={"value": Decimal("0.100")},
        )
        self.assertEqual(envelope.occurred_at, UTC_NOW)
        self.assertEqual(envelope.observed_at, UTC_NOW + timedelta(seconds=1))
        with self.assertRaises(ValueError):
            EventEnvelope(
                event_id=event_id_for("naive"),
                event_type="generic",
                schema_version="1.0",
                occurred_at=datetime(2026, 8, 6),
                observed_at=UTC_NOW,
                correlation_id=correlation_id_for("naive"),
                causation_id=None,
                run_id="run",
                account_id="",
                source="test",
                symbol=None,
                timeframe=None,
                payload={},
            )

    def test_codec_preserves_decimal_utc_enum_payload_and_schema_version(self):
        pipeline = TradingEventPipeline(run_id="codec", clock=lambda: UTC_NOW)
        original = pipeline.publish(
            OrderEvent(
                client_order_id="order-1",
                status=OrderStatus.PARTIALLY_FILLED,
                requested_qty=Decimal("1.000"),
                filled_qty=Decimal("0.100"),
                remaining_qty=Decimal("0.900"),
                average_fill_price=Decimal("50000.0100"),
            ),
            occurred_at=UTC_NOW,
            idempotency_key="order-1:partial",
            source="test",
        )
        document = EventCodec.encode(original)
        decoded = EventCodec.decode(document)

        self.assertEqual(decoded, original)
        self.assertIsInstance(decoded.payload, OrderEvent)
        self.assertEqual(decoded.payload.filled_qty, Decimal("0.100"))
        self.assertEqual(decoded.schema_version, EVENT_SCHEMA_VERSION)
        self.assertEqual(json.loads(document)["format"], "quant-trading-event/1")
        self.assertTrue(json.loads(document)["occurred_at"]["value"].endswith("Z"))

    def test_deterministic_ids_are_order_independent_and_purpose_scoped(self):
        left = event_id_for({"b": 2, "a": Decimal("1.0")}, "x")
        right = event_id_for({"a": Decimal("1.0"), "b": 2}, "x")
        self.assertEqual(left, right)
        self.assertEqual(correlation_id_for("x"), correlation_id_for("x"))
        self.assertEqual(causation_id_for("x"), causation_id_for("x"))
        self.assertNotEqual(
            left, correlation_id_for({"a": Decimal("1.0"), "b": 2}, "x")
        )

    def test_duplicate_delivery_is_idempotent_across_observation_times(self):
        ticks = iter((UTC_NOW, UTC_NOW + timedelta(seconds=1)))
        pipeline = TradingEventPipeline(run_id="retry", clock=lambda: next(ticks))
        first = pipeline.publish(
            {"value": Decimal("1.00")},
            occurred_at=UTC_NOW,
            idempotency_key="stable",
            source="test",
        )
        second = pipeline.publish(
            {"value": Decimal("1.00")},
            occurred_at=UTC_NOW,
            idempotency_key="stable",
            source="test",
        )
        self.assertIs(first, second)
        self.assertEqual(len(pipeline.events), 1)


class TestCanonicalSafetyBoundary(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_submit_intent_cannot_bypass_live_safety_guard(self, ccxt_mock):
        exchange = MagicMock()
        ccxt_mock.binance.return_value = exchange
        guard = MagicMock(unsafe=True)
        guard.assert_order_allowed.side_effect = ValueError("kill switch")
        broker = SafeLiveBroker(
            Portfolio(),
            safety_guard=guard,
            order_store=OrderStore(":memory:"),
            account_id="sandbox",
        )

        result = broker.submit_intent(intent())

        self.assertEqual(result.status, OrderStatus.REJECTED)
        exchange.create_order.assert_not_called()
        guard.assert_order_allowed.assert_called_once()
        broker.close()


class TestSharedBacktestAndLiveContract(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_both_venues_produce_events_consumed_by_one_handler(self, ccxt_mock):
        canonical_intent = intent()

        backtest_events = TradingEventPipeline(
            run_id="backtest-run", clock=lambda: UTC_NOW
        )
        backtest = Broker(
            Portfolio(100000),
            event_pipeline=backtest_events,
            exchange_id="binance",
            account_id="sandbox",
            timeframe="1m",
        )
        backtest.submit_intent(canonical_intent)
        next_bar = pd.Timestamp("2026-08-06T08:01:00Z")
        backtest.process_orders(
            {
                "BTC/USDT": pd.Series(
                    {"open": 50000, "high": 50100, "low": 49900, "volume": 10},
                    name=next_bar,
                )
            }
        )

        exchange = MagicMock()
        exchange.create_order.return_value = {
            "id": "exchange-1",
            "status": "closed",
            "amount": 1,
            "filled": 1,
            "remaining": 0,
            "average": 50000,
            "trades": [
                {
                    "id": "fill-live-1",
                    "amount": 1,
                    "price": 50000,
                    "timestamp": 1786003260000,
                    "fee": {"cost": 5, "currency": "USDT"},
                }
            ],
        }
        exchange.fetch_balance.return_value = {
            "free": {"USDT": 10000}, "total": {"USDT": 10000}
        }
        ccxt_mock.binance.return_value = exchange
        live_events = TradingEventPipeline(run_id="live-run", clock=lambda: UTC_NOW)
        live = LiveBroker(
            Portfolio(),
            event_pipeline=live_events,
            clock=lambda: UTC_NOW,
            account_id="sandbox",
        )
        live.submit_intent(canonical_intent)

        consumed = []
        consumer = TradingEventPipeline(run_id="consumer", clock=lambda: UTC_NOW)
        consumer.subscribe(lambda event: consumed.append(event))
        wire_events = [
            EventCodec.decode(EventCodec.encode(event))
            for event in (*backtest_events.events, *live_events.events)
        ]
        consumer.consume(wire_events)

        self.assertTrue(consumed)
        self.assertTrue(all(isinstance(event, EventEnvelope) for event in consumed))
        self.assertTrue(
            any(isinstance(event.payload, OrderIntent) for event in consumed)
        )
        self.assertTrue(any(isinstance(event.payload, OrderEvent) for event in consumed))
        self.assertTrue(any(isinstance(event.payload, FillEvent) for event in consumed))
        order_sources = {
            event.source for event in consumed if isinstance(event.payload, OrderEvent)
        }
        fill_sources = {
            event.source for event in consumed if isinstance(event.payload, FillEvent)
        }
        self.assertEqual(order_sources, {"backtest", "live"})
        self.assertEqual(fill_sources, {"backtest", "live"})
        correlations = {
            event.correlation_id
            for event in consumed
            if isinstance(event.payload, (OrderIntent, OrderEvent, FillEvent))
        }
        self.assertEqual(len(correlations), 1)
        for source in ("backtest", "live"):
            chain = [
                event
                for event in consumed
                if event.source == source
                and isinstance(event.payload, (OrderIntent, OrderEvent, FillEvent))
            ]
            for previous, current in zip(chain, chain[1:]):
                self.assertEqual(current.causation_id, previous.event_id)
        live.close()


if __name__ == "__main__":
    unittest.main()
