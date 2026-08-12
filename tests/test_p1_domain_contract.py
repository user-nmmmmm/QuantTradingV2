import hashlib
import json
import unittest
from dataclasses import replace

from core.domain import FillRecord, OrderIntent, OrderStatus
from core.orders import TERMINAL_STATUSES, normalize_exchange_status, validate_transition


class TestCanonicalOrderStatus(unittest.TestCase):
    def test_statuses_have_canonical_values_and_compatibility_aliases(self):
        self.assertIs(OrderStatus.SUBMITTED, OrderStatus.SUBMITTING)
        self.assertIs(OrderStatus.OPEN, OrderStatus.ACCEPTED)
        self.assertEqual(OrderStatus.SUBMITTED.value, "submitting")
        self.assertEqual(OrderStatus.OPEN.value, "accepted")

        canonical = {status.value for status in OrderStatus}
        self.assertEqual(
            canonical,
            {
                'expired_unsubmitted',
                "created",
                "submitting",
                "accepted",
                "partial",
                "filled",
                "cancel_pending",
                "canceled",
                "rejected",
                "expired",
                "unknown",
            },
        )

    def test_expired_is_terminal_but_late_exchange_fills_win(self):
        self.assertIn(OrderStatus.EXPIRED_UNSUBMITTED, TERMINAL_STATUSES)
        self.assertIn(OrderStatus.EXPIRED, TERMINAL_STATUSES)
        validate_transition("expired", OrderStatus.PARTIALLY_FILLED)
        validate_transition("expired", OrderStatus.FILLED)
        validate_transition("canceled", OrderStatus.PARTIALLY_FILLED)
        validate_transition("canceled", OrderStatus.FILLED)
        with self.assertRaises(ValueError):
            validate_transition("expired", OrderStatus.ACCEPTED)

    def test_unknown_can_resolve_to_expired(self):
        validate_transition("unknown", OrderStatus.EXPIRED)

    def test_partial_expired_response_does_not_remain_non_terminal(self):
        status = normalize_exchange_status(
            {
                "status": "expired",
                "amount": 10,
                "filled": 3,
                "remaining": 7,
            }
        )
        self.assertIs(status, OrderStatus.EXPIRED)

    def test_fully_filled_expired_response_is_still_filled(self):
        status = normalize_exchange_status(
            {
                "status": "expired",
                "amount": 10,
                "filled": 10,
                "remaining": 0,
            }
        )
        self.assertIs(status, OrderStatus.FILLED)

    def test_canceled_partial_fill_keeps_existing_late_fill_semantics(self):
        status = normalize_exchange_status(
            {
                "status": "canceled",
                "amount": 10,
                "filled": 3,
                "remaining": 7,
            }
        )
        self.assertIs(status, OrderStatus.PARTIALLY_FILLED)


class TestOrderIntentIdentity(unittest.TestCase):
    def setUp(self):
        self.legacy_args = (
            "binance",
            "primary",
            "BTC/USDT",
            "1h",
            "2026-08-06T01:00:00Z",
            "trend",
            "buy",
            2,
            0.25,
            "limit",
            50000.0,
            "GTC",
            False,
            "long",
            "one_way",
        )
        self.intent = OrderIntent(*self.legacy_args)

    def test_legacy_positional_construction_and_digest_are_unchanged(self):
        expected_identity = {
            "exchange": "binance",
            "account": "primary",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bar_time": "2026-08-06T01:00:00Z",
            "strategy_id": "trend",
            "action": "buy",
            "sequence": 2,
        }
        digest = hashlib.sha256(
            json.dumps(
                expected_identity, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:24]

        self.assertEqual(self.intent.identity, expected_identity)
        self.assertEqual(self.intent.client_order_id, f"qt_{digest}")
        self.assertEqual(self.intent.intent_id, self.intent.client_order_id)

    def test_identity_returns_a_fresh_stable_value(self):
        first = self.intent.identity
        first["symbol"] = "MUTATED"
        self.assertEqual(self.intent.identity["symbol"], "BTC/USDT")

    def test_optional_causal_metadata_does_not_change_idempotency_key(self):
        enriched = replace(
            self.intent,
            signal_id="signal-1",
            risk_decision_id="risk-1",
            reservation_id="reservation-1",
            causation_id="event-1",
            created_at="2026-08-06T00:59:59Z",
        )
        self.assertEqual(enriched.identity, self.intent.identity)
        self.assertEqual(enriched.client_order_id, self.intent.client_order_id)
        self.assertEqual(enriched.intent_id, self.intent.intent_id)
        self.assertEqual(enriched.correlation_id, "signal-1")
        self.assertEqual(self.intent.correlation_id, self.intent.intent_id)


class TestFillRecordCompatibility(unittest.TestCase):
    def test_legacy_positional_constructor_still_works(self):
        fill = FillRecord(
            "fill-1",
            "client-1",
            "exchange-1",
            0.25,
            50000.0,
            2.5,
            "USDT",
            "2026-08-06T01:00:01Z",
            {"source": "legacy"},
        )
        self.assertEqual(fill.payload, {"source": "legacy"})
        self.assertIsNone(fill.symbol)
        self.assertIsNone(fill.side)
        self.assertIsNone(fill.correlation_id)
        self.assertIsNone(fill.causation_id)

    def test_canonical_causal_fields_are_available(self):
        fill = FillRecord(
            fill_id="fill-2",
            client_order_id="client-2",
            exchange_order_id="exchange-2",
            qty=0.1,
            price=51000.0,
            symbol="BTC/USDT",
            side="buy",
            correlation_id="signal-2",
            causation_id="order-event-2",
        )
        self.assertEqual(fill.symbol, "BTC/USDT")
        self.assertEqual(fill.side, "buy")
        self.assertEqual(fill.correlation_id, "signal-2")
        self.assertEqual(fill.causation_id, "order-event-2")


if __name__ == "__main__":
    unittest.main()
