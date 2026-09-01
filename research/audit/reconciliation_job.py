"""Schedulable end-of-day reconciliation with durable JSON reports.

Offline audit tooling — not wired into the live or backtest order path.
See ``research/audit/ledger.py`` module docstring and docs/architecture_review.md (A3).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from core.events.store import SQLiteEventStore
from research.audit.ledger import AuthoritativeLedger, PortfolioProjection, ReconciliationReport


def report_to_dict(
    report: ReconciliationReport, *, account_id: str, trading_day: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job": "eod_reconciliation",
        "account_id": account_id,
        "trading_day": trading_day,
        "checked_at": report.checked_at.isoformat(),
        "ok": report.ok,
        "has_critical": report.has_critical,
        "discrepancy_count": len(report.discrepancies),
        "discrepancies": [
            {key: str(value) if key in {
                "expected", "actual", "difference", "tolerance",
            } else value for key, value in asdict(item).items()}
            for item in report.discrepancies
        ],
    }


class EODReconciliationJob:
    """Run PortfolioProjection.reconcile and atomically persist its report."""

    def __init__(
        self,
        projection: PortfolioProjection,
        *,
        account_id: str,
        output_dir: str = "reports/reconciliation",
    ) -> None:
        self.projection = projection
        self.account_id = account_id
        self.output_dir = Path(output_dir)

    def run(
        self,
        *,
        external_cash: Mapping[str, Any],
        external_positions: Mapping[str, Any],
        checked_at: Optional[datetime] = None,
        cash_tolerance: Any = "0.00000001",
        position_tolerance: Any = "0.00000001",
    ) -> tuple[ReconciliationReport, Path]:
        now = checked_at or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        now = now.astimezone(timezone.utc)
        report = self.projection.reconcile(
            external_cash=external_cash,
            external_positions=external_positions,
            cash_tolerance=cash_tolerance,
            position_tolerance=position_tolerance,
            checked_at=now,
        )
        day = now.date().isoformat()
        account = "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in self.account_id
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{day}_{account}.json"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = report_to_dict(
            report, account_id=self.account_id, trading_day=day,
        )
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return report, path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run EOD ledger reconciliation")
    parser.add_argument("--ledger-db", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--external-state", required=True)
    parser.add_argument("--base-currency", default="USD")
    parser.add_argument("--output-dir", default="reports/reconciliation")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with open(args.external_state, "r", encoding="utf-8") as handle:
        external = json.load(handle)
    store = SQLiteEventStore(args.ledger_db)
    try:
        ledger = AuthoritativeLedger(
            store,
            account_id=args.account_id,
            base_currency=args.base_currency,
        )
        report, path = EODReconciliationJob(
            ledger.projection,
            account_id=args.account_id,
            output_dir=args.output_dir,
        ).run(
            external_cash=external.get("cash", {}),
            external_positions=external.get("positions", {}),
        )
    finally:
        store.close()
    print(json.dumps({
        "ok": report.ok,
        "discrepancy_count": len(report.discrepancies),
        "report": str(path),
    }, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
