import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from core.event_store import SQLiteEventStore
from core.events import FillEvent, TradingEventPipeline
from research.audit.ledger import (
    AuthoritativeLedger,
    AverageCostPositionReducer,
    PositionState,
)


NOW = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)


class TestAverageCostPositionReducer(unittest.TestCase):
    def test_add_reduce_flatten_and_reverse_long_to_short(self):
        reducer = AverageCostPositionReducer()
        empty = PositionState("BTC/USDT", quote_currency="USDT")
        long = reducer.reduce(empty, Decimal("10"), Decimal("100")).after
        long = reducer.reduce(long, Decimal("10"), Decimal("120")).after
        self.assertEqual(long.qty, Decimal("20"))
        self.assertEqual(long.average_cost, Decimal("110"))

        reduced = reducer.reduce(long, Decimal("-5"), Decimal("130"))
        self.assertEqual(reduced.after.qty, Decimal("15"))
        self.assertEqual(reduced.after.average_cost, Decimal("110"))
        self.assertEqual(reduced.realized_delta, Decimal("100"))

        reversed_position = reducer.reduce(
            reduced.after, Decimal("-20"), Decimal("90")
        )
        self.assertEqual(reversed_position.realized_delta, Decimal("-300"))
        self.assertEqual(reversed_position.after.qty, Decimal("-5"))
        self.assertEqual(reversed_position.after.average_cost, Decimal("90"))
        self.assertEqual(reversed_position.opened_qty, Decimal("5"))

    def test_short_cover_and_reverse_uses_new_fill_as_long_cost(self):
        short = PositionState(
            "ETH/USD",
            qty=Decimal("-4"),
            average_cost=Decimal("200"),
            quote_currency="USD",
        )
        result = AverageCostPositionReducer.reduce(
            short, Decimal("6"), Decimal("150")
        )
        self.assertEqual(result.realized_delta, Decimal("200"))
        self.assertEqual(result.after.qty, Decimal("2"))
        self.assertEqual(result.after.average_cost, Decimal("150"))


class TestAppendOnlyEventStore(unittest.TestCase):
    def test_persists_ordered_events_is_idempotent_and_rejects_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.sqlite3")
            store = SQLiteEventStore(path)
            pipeline = TradingEventPipeline(
                run_id="run", clock=lambda: NOW, store=store
            )
            first = pipeline.publish(
                {"kind": "one"},
                occurred_at=NOW,
                account_id="acct",
                idempotency_key="one",
                source="test",
            )
            second = pipeline.publish(
                {"kind": "two"},
                occurred_at=NOW + timedelta(seconds=1),
                account_id="acct",
                idempotency_key="two",
                source="test",
            )
            self.assertEqual(store.last_sequence, 2)
            self.assertEqual(store.read(), (first, second))
            self.assertFalse(store.append(first))
            self.assertEqual(store.last_sequence, 2)

            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with store._connection:
                    store._connection.execute(
                        "UPDATE events SET event_type='changed' WHERE sequence=1"
                    )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with store._connection:
                    store._connection.execute(
                        "DELETE FROM events WHERE sequence=1"
                    )
            store.close()

            reopened = SQLiteEventStore(path)
            self.assertEqual(reopened.read(), (first, second))
            reopened.close()


class TestAuthoritativeLedger(unittest.TestCase):
    @staticmethod
    def publish_fill(
        ledger,
        *,
        fill_id,
        side,
        qty,
        price,
        fee="0",
        fee_currency="USD",
        seconds=0,
        symbol="BTC/USD",
    ):
        producer = TradingEventPipeline(
            run_id=f"producer-{fill_id}", clock=lambda: NOW + timedelta(seconds=seconds)
        )
        event = producer.publish(
            FillEvent(
                fill_id=fill_id,
                client_order_id=f"order-{fill_id}",
                symbol=symbol,
                side=side,
                qty=Decimal(qty),
                price=Decimal(price),
                fee=Decimal(fee),
                fee_currency=fee_currency,
            ),
            event_type="fill",
            occurred_at=NOW + timedelta(seconds=seconds),
            account_id="acct",
            idempotency_key=fill_id,
            source="test",
        )
        ledger.record_fill(event)
        return event

    def test_snapshot_is_fully_rebuilt_and_reversal_pnl_is_correct(self):
        store = SQLiteEventStore(":memory:")
        ledger = AuthoritativeLedger(
            store,
            account_id="acct",
            base_currency="USD",
            run_id="ledger",
            clock=lambda: NOW,
        )
        ledger.record_cash(
            "USD", "1000", reason="opening_balance", occurred_at=NOW,
            idempotency_key="opening-usd",
        )
        self.publish_fill(
            ledger, fill_id="f1", side="buy", qty="10", price="100",
            fee="1", seconds=1,
        )
        self.publish_fill(
            ledger, fill_id="f2", side="sell", qty="15", price="110",
            fee="1", seconds=2,
        )
        ledger.record_mark(
            "BTC/USD", "100", "USD", NOW + timedelta(seconds=3),
            idempotency_key="mark-btc-3",
        )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.cash_balances, {"USD": Decimal("1648")})
        self.assertEqual(snapshot.positions["BTC/USD"]["qty"], Decimal("-5"))
        self.assertEqual(
            snapshot.positions["BTC/USD"]["average_cost"], Decimal("110")
        )
        self.assertEqual(snapshot.realized_pnl, Decimal("100"))
        self.assertEqual(snapshot.unrealized_pnl, Decimal("50"))
        self.assertEqual(snapshot.fees, {"USD": Decimal("2")})
        self.assertEqual(snapshot.equity, 1148.0)
        self.assertEqual(snapshot.gross_exposure, 500.0)
        self.assertEqual(snapshot.net_exposure, -500.0)

        rebuilt = AuthoritativeLedger(
            store, account_id="acct", base_currency="USD", run_id="rebuild"
        )
        self.assertEqual(rebuilt.snapshot(), snapshot)
        self.assertEqual(rebuilt.projection.fills.postings, ledger.projection.fills.postings)
        store.close()

    def test_realized_pnl_survives_when_position_is_flat(self):
        store = SQLiteEventStore(":memory:")
        ledger = AuthoritativeLedger(
            store, account_id="acct", base_currency="USD", run_id="flat",
            clock=lambda: NOW,
        )
        ledger.record_cash(
            "USD", "1000", occurred_at=NOW, idempotency_key="flat-cash"
        )
        self.publish_fill(
            ledger, fill_id="flat-buy", side="buy", qty="2", price="100",
            seconds=1,
        )
        self.publish_fill(
            ledger, fill_id="flat-sell", side="sell", qty="2", price="125",
            seconds=2,
        )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.positions, {})
        self.assertEqual(snapshot.realized_pnl, Decimal("50"))
        self.assertEqual(snapshot.unrealized_pnl, Decimal("0"))
        self.assertEqual(snapshot.equity, 1050.0)

        ledger.rebuild()
        self.assertEqual(ledger.snapshot(), snapshot)
        store.close()

    def test_multicurrency_cash_fee_fx_and_reconciliation(self):
        store = SQLiteEventStore(":memory:")
        ledger = AuthoritativeLedger(
            store, account_id="acct", base_currency="USD", run_id="multi",
            clock=lambda: NOW,
        )
        ledger.record_cash(
            "USD", "1000", occurred_at=NOW, idempotency_key="usd"
        )
        ledger.record_cash(
            "EUR", "100", occurred_at=NOW, idempotency_key="eur"
        )
        self.publish_fill(
            ledger,
            fill_id="eur-fill",
            side="buy",
            qty="2",
            price="50",
            fee="1",
            fee_currency="EUR",
            seconds=1,
            symbol="SAP/EUR",
        )
        ledger.record_mark(
            "SAP/EUR", "60", "EUR", NOW + timedelta(seconds=2),
            idempotency_key="sap-mark",
        )
        snapshot = ledger.snapshot(fx_rates={"EUR/USD": "1.2"})

        self.assertEqual(
            snapshot.cash_balances,
            {"USD": Decimal("1000"), "EUR": Decimal("-1")},
        )
        self.assertEqual(snapshot.unrealized_pnl, Decimal("24.0"))
        self.assertEqual(snapshot.equity, 1142.8)
        self.assertEqual(snapshot.fees, {"EUR": Decimal("1")})

        report = ledger.projection.reconcile(
            external_cash={"USD": "999", "EUR": "-1"},
            external_positions={"SAP/EUR": {"qty": "1.5"}},
            checked_at=NOW + timedelta(seconds=3),
        )
        self.assertFalse(report.ok)
        self.assertEqual(
            {(item.category, item.key) for item in report.discrepancies},
            {("cash", "USD"), ("position", "SAP/EUR")},
        )
        self.assertTrue(report.has_critical)
        store.close()

    def test_projection_rebuild_can_stop_at_an_authoritative_sequence(self):
        store = SQLiteEventStore(":memory:")
        ledger = AuthoritativeLedger(
            store, account_id="acct", base_currency="USD", run_id="temporal",
            clock=lambda: NOW,
        )
        ledger.record_cash(
            "USD", "100", occurred_at=NOW, idempotency_key="initial"
        )
        checkpoint = store.last_sequence
        ledger.record_cash(
            "USD", "25", occurred_at=NOW, idempotency_key="deposit"
        )
        ledger.rebuild(through_sequence=checkpoint)
        self.assertEqual(
            ledger.snapshot(require_marks=False).cash_balances["USD"],
            Decimal("100"),
        )
        store.close()


if __name__ == "__main__":
    unittest.main()
