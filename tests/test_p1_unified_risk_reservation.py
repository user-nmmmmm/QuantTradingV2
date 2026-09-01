import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.domain import OrderIntent, OrderStatus, RiskDecision, RiskReservation
from core.events import FillEvent, OrderEvent, TradingEventPipeline
from core.live_broker import LiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.risk.reservation import (
    RiskReservationProjection,
    ensure_opening_reservation,
)


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def opening_intent(sequence=0, qty=2, price=100):
    return OrderIntent(
        exchange="binance",
        account="sandbox",
        symbol="BTC/USDT",
        timeframe="1m",
        bar_time=NOW.isoformat(),
        strategy_id="reservation-test",
        action="buy",
        sequence=sequence,
        requested_qty=qty,
        price=price,
    )


class TestRiskReservationDomain(unittest.TestCase):
    def test_domain_objects_use_exact_decimal_values(self):
        decision = RiskDecision(
            "decision-1", "sandbox", "BTC/USDT", "buy",
            Decimal("2"), Decimal("2"), Decimal("100.25"), True, "approved",
            "intent-1",
        )
        reservation = RiskReservation(
            "reservation-1", decision.decision_id, "intent-1", "sandbox",
            "BTC/USDT", "buy", Decimal("2"), Decimal("100.25"),
        )

        self.assertEqual(reservation.reserved_notional, Decimal("200.50"))
        with self.assertRaises(ValueError):
            RiskDecision(
                "decision-2", "sandbox", "BTC/USDT", "buy",
                2, 1, 100, False, "rejected",
            )


class TestRiskReservationProjection(unittest.TestCase):
    def setUp(self):
        self.pipeline = TradingEventPipeline(run_id="reservation", clock=lambda: NOW)
        self.projection = RiskReservationProjection(self.pipeline)
        self.intent, self.intent_event = ensure_opening_reservation(
            self.pipeline,
            opening_intent(),
            reference_price=100,
            occurred_at=NOW,
            source="test",
        )

    def publish_order(self, status, suffix):
        return self.pipeline.publish(
            OrderEvent(
                client_order_id=self.intent.intent_id,
                status=status,
                requested_qty=2,
                filled_qty=0,
                remaining_qty=2,
            ),
            occurred_at=NOW,
            correlation_id=self.intent.correlation_id,
            causation_id=self.intent_event,
            idempotency_key=f"{self.intent.intent_id}:{suffix}",
            account_id=self.intent.account,
            symbol=self.intent.symbol,
            timeframe=self.intent.timeframe,
            source="test",
        )

    def test_fill_consumes_unknown_retains_and_cancel_releases(self):
        self.assertEqual(
            self.projection.pending_notional({"BTC/USDT": 110}),
            {"BTC/USDT": 220.0},
        )

        self.publish_order(OrderStatus.UNKNOWN, "unknown")
        self.assertEqual(
            self.projection.pending_notional({"BTC/USDT": 110}),
            {"BTC/USDT": 220.0},
        )

        fill = self.pipeline.publish(
            FillEvent(
                fill_id="fill-1",
                client_order_id=self.intent.intent_id,
                symbol=self.intent.symbol,
                side="buy",
                qty=Decimal("0.5"),
                price=Decimal("105"),
            ),
            occurred_at=NOW,
            correlation_id=self.intent.correlation_id,
            causation_id=self.intent_event,
            idempotency_key="fill-1",
            account_id=self.intent.account,
            symbol=self.intent.symbol,
            timeframe=self.intent.timeframe,
            source="test",
        )
        self.assertEqual(
            self.projection.pending_notional({"BTC/USDT": 110}),
            {"BTC/USDT": 165.0},
        )
        self.assertFalse(self.projection.apply(fill), "duplicate delivery is idempotent")

        self.publish_order(OrderStatus.CANCELED, "canceled")
        self.assertEqual(self.projection.pending_notional({"BTC/USDT": 110}), {})

    def test_reject_and_expire_release(self):
        for index, status in enumerate((OrderStatus.REJECTED, OrderStatus.EXPIRED), 1):
            pipeline = TradingEventPipeline(run_id=f"release-{index}", clock=lambda: NOW)
            projection = RiskReservationProjection(pipeline)
            intent, intent_event = ensure_opening_reservation(
                pipeline,
                opening_intent(sequence=index),
                reference_price=100,
                occurred_at=NOW,
                source="test",
            )
            pipeline.publish(
                OrderEvent(
                    client_order_id=intent.intent_id,
                    status=status,
                    requested_qty=2,
                    remaining_qty=2,
                ),
                occurred_at=NOW,
                correlation_id=intent.correlation_id,
                causation_id=intent_event,
                idempotency_key=f"{intent.intent_id}:{status.value}",
                account_id=intent.account,
                symbol=intent.symbol,
                timeframe=intent.timeframe,
                source="test",
            )
            self.assertEqual(projection.pending_notional(), {})

    def test_same_business_stream_has_same_occupancy_for_both_venues(self):
        def run(source):
            pipeline = TradingEventPipeline(run_id=source, clock=lambda: NOW)
            projection = RiskReservationProjection(pipeline)
            intent, intent_event = ensure_opening_reservation(
                pipeline,
                opening_intent(qty=3),
                reference_price=100,
                occurred_at=NOW,
                source=source,
            )
            pipeline.publish(
                FillEvent(
                    fill_id=f"{source}-fill",
                    client_order_id=intent.intent_id,
                    symbol=intent.symbol,
                    side="buy",
                    qty=1,
                    price=105,
                ),
                occurred_at=NOW,
                correlation_id=intent.correlation_id,
                causation_id=intent_event,
                idempotency_key=f"{source}-fill",
                account_id=intent.account,
                symbol=intent.symbol,
                timeframe=intent.timeframe,
                source=source,
            )
            pipeline.publish(
                OrderEvent(
                    client_order_id=intent.intent_id,
                    status=OrderStatus.UNKNOWN,
                    requested_qty=3,
                    filled_qty=1,
                    remaining_qty=2,
                ),
                occurred_at=NOW,
                correlation_id=intent.correlation_id,
                causation_id=intent_event,
                idempotency_key=f"{source}-unknown",
                account_id=intent.account,
                symbol=intent.symbol,
                timeframe=intent.timeframe,
                source=source,
            )
            return projection.pending_notional({"BTC/USDT": 120})

        self.assertEqual(run("backtest"), run("live"))
        self.assertEqual(run("backtest"), {"BTC/USDT": 240.0})


class TestAtomicRiskApproval(unittest.TestCase):
    def test_approval_reservation_and_intent_are_one_ordered_unit(self):
        pipeline = TradingEventPipeline(run_id="atomic", clock=lambda: NOW)
        projection = RiskReservationProjection(pipeline)
        manager = RiskManager(max_pos_size_pct=0.5)
        portfolio = Portfolio(10000)

        decision, intent = manager.approve_and_create_intent(
            portfolio,
            opening_intent(qty=10),
            reference_price=100,
            event_pipeline=pipeline,
            reservation_projection=projection,
            occurred_at=NOW,
            current_prices={"BTC/USDT": 100},
        )

        self.assertTrue(decision.approved)
        self.assertIsNotNone(intent)
        self.assertEqual(
            [event.event_type for event in pipeline.events],
            ["risk_decision", "risk_reservation", "order_intent"],
        )
        self.assertEqual(projection.pending_notional(), {"BTC/USDT": 1000.0})

        rejected, rejected_intent = manager.approve_and_create_intent(
            portfolio,
            opening_intent(sequence=1, qty=50),
            reference_price=100,
            event_pipeline=pipeline,
            reservation_projection=projection,
            occurred_at=NOW,
            current_prices={"BTC/USDT": 100},
        )
        self.assertFalse(rejected.approved)
        self.assertIsNone(rejected_intent)
        self.assertEqual(projection.pending_notional(), {"BTC/USDT": 1000.0})


class TestLiveReservationRecovery(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_unknown_reservation_survives_restart(self, ccxt_mock):
        exchange = MagicMock()
        exchange.create_order.side_effect = TimeoutError("response lost")
        ccxt_mock.binance.return_value = exchange

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "orders.db")
            first = LiveBroker(
                Portfolio(),
                order_store=OrderStore(path),
                clock=lambda: NOW,
                account_id="sandbox",
            )
            first.set_bar_context("1m", NOW)
            result = first.submit_order("BTC/USDT", "buy", 1, 100)
            self.assertEqual(result.status, OrderStatus.UNKNOWN)
            self.assertEqual(first.pending_open_notional(), {"BTC/USDT": 100.0})
            first.close()

            restarted = LiveBroker(
                Portfolio(),
                order_store=OrderStore(path),
                clock=lambda: NOW,
                account_id="sandbox",
            )
            self.assertEqual(
                restarted.pending_open_notional(),
                {"BTC/USDT": 100.0},
            )
            restarted.close()


if __name__ == "__main__":
    unittest.main()
