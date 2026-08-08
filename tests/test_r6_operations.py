import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.alerting import AlertState, HysteresisAlertSink
from core.event_store import SQLiteEventStore
from core.ledger import AuthoritativeLedger
from core.reconciliation_job import EODReconciliationJob
from core.sqlite_backup import SQLiteSnapshotManager, restore_snapshot
from core.state_store_v2 import StateStore
from dashboard.__main__ import load_dashboard, render_text


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


class CaptureSink:
    def __init__(self):
        self.records = []

    def notify(self, level, event, context):
        self.records.append((level, event, context))


class AlertHysteresisTests(unittest.TestCase):
    def test_ten_identical_alerts_emit_trigger_and_one_summary(self):
        downstream = CaptureSink()
        sink = HysteresisAlertSink(downstream, summary_every=9)
        context = {"reason_codes": ["ACCOUNT_SYNC_STALE"]}

        for _ in range(10):
            sink.notify("critical", "risk_halt", context)

        self.assertEqual(
            [record[1] for record in downstream.records],
            ["risk_halt", "alert_suppression_summary"],
        )
        self.assertEqual(downstream.records[1][2]["suppressed_count"], 9)
        self.assertEqual(
            sink.state("risk_halt", context), AlertState.SUPPRESSED,
        )
        self.assertEqual(sink.ack("risk_halt", context), 1)
        sink.trigger("critical", "risk_halt", context)
        self.assertEqual(len(downstream.records), 3)


class EODReconciliationTests(unittest.TestCase):
    def test_job_persists_structured_daily_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(str(Path(directory) / "ledger.db"))
            ledger = AuthoritativeLedger(
                store, account_id="acct/main", base_currency="USD",
                clock=lambda: NOW,
            )
            ledger.record_cash(
                "USD", "100", occurred_at=NOW, idempotency_key="opening",
            )
            report, path = EODReconciliationJob(
                ledger.projection,
                account_id="acct/main",
                output_dir=str(Path(directory) / "reconciliation"),
            ).run(
                external_cash={"USD": "99"},
                external_positions={},
                checked_at=NOW,
            )
            store.close()

            self.assertFalse(report.ok)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["trading_day"], "2026-08-08")
            self.assertEqual(payload["discrepancy_count"], 1)
            self.assertEqual(payload["discrepancies"][0]["category"], "cash")
            self.assertEqual(payload["discrepancies"][0]["difference"], "-1")


class SQLiteBackupTests(unittest.TestCase):
    def test_snapshot_restores_usable_state_and_prunes_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "state.db")
            snapshots = str(Path(directory) / "snapshots")
            store = StateStore(database)
            store.set("mode", "before")
            store.close()
            manager = SQLiteSnapshotManager(
                database, snapshot_dir=snapshots, retention=2,
            )
            first = manager.create_snapshot(now=NOW)

            store = StateStore(database)
            store.set("mode", "after")
            store.close()
            manager.create_snapshot(now=NOW + timedelta(seconds=1))
            manager.create_snapshot(now=NOW + timedelta(seconds=2))
            self.assertEqual(len(manager.snapshots()), 2)
            self.assertFalse(first.exists())

            oldest_retained = manager.snapshots()[0]
            restore_snapshot(str(oldest_retained), database)
            restored = StateStore(database)
            self.assertEqual(restored.get("mode"), "after")
            restored.close()
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok",
            )
            connection.close()


class DashboardConsumerTests(unittest.TestCase):
    def test_cli_consumer_shows_equity_positions_health_and_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "live_status.json"
            alerts = Path(directory) / "live_alerts.jsonl"
            status.write_text(json.dumps({
                "last_update": NOW.isoformat(),
                "equity": 12345.0,
                "cash": 1000.0,
                "positions": {
                    "BTC/USDT": {"qty": 0.2, "avg_price": 50000.0},
                },
                "healthy": False,
                "operational_state": "RISK_HALTED",
                "health_reason_codes": ["MARKET_DATA_STALE"],
                "health_assessment": {"reasons": [{
                    "code": "MARKET_DATA_STALE",
                    "subject": "BTC/USDT@1m",
                    "message": "latest market data is stale",
                }]},
            }), encoding="utf-8")
            alerts.write_text(json.dumps({
                "timestamp": NOW.isoformat(),
                "level": "critical",
                "event": "risk_halt",
                "context": {"reason_codes": ["MARKET_DATA_STALE"]},
            }) + "\n", encoding="utf-8")

            data = load_dashboard(str(status), str(alerts))
            output = render_text(data)
            self.assertIn("Equity: 12345.0", output)
            self.assertIn("BTC/USDT: qty=0.2", output)
            self.assertIn("MARKET_DATA_STALE", output)
            self.assertIn("risk_halt", output)


if __name__ == "__main__":
    unittest.main()
