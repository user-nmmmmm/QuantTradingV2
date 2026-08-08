import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.r7_acceptance import audit_r7


class R7AcceptanceTests(unittest.TestCase):
    def test_complete_evidence_passes_and_unresolved_alert_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reconciliation = root / "reconciliation"
            reconciliation.mkdir()
            start = date(2026, 8, 1)
            for offset in range(2):
                day = start + timedelta(days=offset)
                (reconciliation / f"{day.isoformat()}_sandbox.json").write_text(
                    json.dumps({
                        "trading_day": day.isoformat(),
                        "ok": True,
                        "discrepancy_count": 0,
                    }),
                    encoding="utf-8",
                )
            event_time = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).isoformat()
            (root / "alerts.jsonl").write_text(
                json.dumps({
                    "timestamp": event_time,
                    "event": "risk_halt",
                    "level": "critical",
                }) + "\n",
                encoding="utf-8",
            )
            (root / "incidents.jsonl").write_text(
                json.dumps({
                    "event_key": f"{event_time}|risk_halt",
                    "outcome": "explained",
                    "explanation": "Injected stale-market-data drill",
                }) + "\n",
                encoding="utf-8",
            )
            (root / "closures.json").write_text(
                json.dumps({f"G{number}": "closed" for number in range(11, 17)}),
                encoding="utf-8",
            )

            passed = audit_r7(
                start=start,
                end=start + timedelta(days=1),
                reconciliation_dir=str(reconciliation),
                alerts_path=str(root / "alerts.jsonl"),
                incidents_path=str(root / "incidents.jsonl"),
                closures_path=str(root / "closures.json"),
                account_id="sandbox",
                minimum_days=2,
            )
            (root / "incidents.jsonl").write_text("", encoding="utf-8")
            failed = audit_r7(
                start=start,
                end=start + timedelta(days=1),
                reconciliation_dir=str(reconciliation),
                alerts_path=str(root / "alerts.jsonl"),
                incidents_path=str(root / "incidents.jsonl"),
                closures_path=str(root / "closures.json"),
                account_id="sandbox",
                minimum_days=2,
            )

        self.assertTrue(passed["ok"])
        self.assertEqual(passed["reconciliation_reports"], 2)
        self.assertFalse(failed["ok"])
        self.assertTrue(
            any(item.startswith("unresolved_halt_or_alert:") for item in failed["issues"])
        )


if __name__ == "__main__":
    unittest.main()
