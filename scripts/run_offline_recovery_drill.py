"""Measure stopped-writer SQLite recovery on synthetic sandbox facts only.

This never constructs an exchange, sends an alert or opens an existing runtime
directory. Its RTO/RPO describe this local drill, not production readiness.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.domain import FillRecord, OrderIntent, OrderStatus
from core.live_broker.fill_projection import replay_fill_projection
from core.live_safety import StartupSafetyPolicy
from core.order_store import OrderStore
from core.risk.persistent_guard import PersistentOrderSafetyGuard
from core.runtime_identity import RuntimeIdentity
from core.sqlite_utils import DatabaseIntegrityError
from core.state_store_v2 import StateStore


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")


def facts(directory, identity):
    state = StateStore(str(directory / "state.db"), identity=identity)
    orders = OrderStore(str(directory / "orders.db"), identity=identity)
    try:
        records = orders.list_all()
        books, closes, issues = replay_fill_projection(orders, "USDT")
        with closing(sqlite3.connect(directory / "risk.db")) as conn:
            daily_risk = list(conn.execute("SELECT risk_day,notional FROM daily_risk ORDER BY risk_day"))
        return {
            "orders": records,
            "fills": [fill for record in records for fill in orders.fills_for(record["client_order_id"])],
            "lots": {symbol: [asdict(lot) for lot in book.open_lots] for symbol, book in books.items()},
            "close_count": len(closes), "projection_issues": issues,
            "processed_bar": state.status("offline-bar"),
            "watermark": state.get("fill_watermark"),
            "risk_action": state.get("portfolio_risk_action:drill"),
            "daily_risk": daily_risk,
        }
    finally:
        orders.close()
        state.close()


def run_drill(output: Path, *, rto_seconds: float = 30.0) -> dict:
    if not 0 < rto_seconds < 3600:
        raise ValueError("rto_seconds must be positive and below one hour")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    identity = RuntimeIdentity("offline_fixture", "sandbox", "recovery-drill", "spot")
    identity_file = output / "runtime_identity.json"
    write_json(identity_file, asdict(identity))
    protocol = {
        "schema": "offline-recovery-protocol/v1", "scope": "synthetic_stopped_writer_drill",
        "rto_seconds": rto_seconds, "rpo_lost_pre_backup_committed_records": 0,
        "restore_identity": asdict(identity), "writers_stopped_before_backup": True,
        "real_alert_delivery": "not_executed", "real_exchange": "not_connected",
    }
    # Freeze the targets before constructing facts or measuring the result.
    write_json(output / "protocol.json", protocol)
    now = datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat()
    state = StateStore(str(output / "state.db"), identity=identity)
    orders = OrderStore(str(output / "orders.db"), identity=identity)
    policy = StartupSafetyPolicy(True, identity.exchange, "spot", ("BTC/USDT",),
        (identity.exchange,), ("spot",), ("BTC/USDT",), "USDT", 1000, 5000)
    guard = PersistentOrderSafetyGuard(policy, str(output / "risk.db"),
        clock=lambda: datetime.fromisoformat(now).date(), identity=identity)
    intent = OrderIntent(exchange=identity.exchange, account=identity.account,
        symbol="BTC/USDT", timeframe="1d", bar_time=now, strategy_id="drill-strategy",
        action="buy", sequence=1, requested_qty=1, reference_price=100,
        initial_stop=90, approved_risk_amount=10, created_at=now)
    try:
        orders.create_intent(intent, now)
        orders.mark_submission_attempted(intent.client_order_id, now)
        orders.transition(intent.client_order_id, OrderStatus.PARTIALLY_FILLED, now,
            exchange_order_id="fixture-order", filled_qty=.4, remaining_qty=.6, average_fill_price=100)
        orders.add_fill(FillRecord("fixture-fill", intent.client_order_id, "fixture-order",
            .4, 100, 0, "USDT", now, {"id": "fixture-fill"}))
        state.claim_bar("offline-bar", now)
        state.complete_bar("offline-bar", now)
        state.set_many({"fill_watermark": 1, "portfolio_risk_action:drill": {
            "action_id": "drill", "status": "active", "target_qty": .2,
            "original_order_id": intent.client_order_id,
        }})
        guard.assert_order_allowed("BTC/USDT", "buy", 1, 100)
    finally:
        state.close()
        orders.close()
        guard.close()
    before = facts(output, identity)
    commands = []
    def execute(command):
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        commands.append({"command": command, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        write_json(output / "command_results.json", commands)
        if result.returncode:
            write_json(output / "recovery_drill.json", {
                "schema": "offline-recovery-drill/v1", "passed": False,
                "engineering_status": "fail", "failed_command": commands[-1],
                "protocol_sha256": hashlib.sha256((output / "protocol.json").read_bytes()).hexdigest(),
            })
            raise RuntimeError(f"Offline recovery command failed: {result.stderr}")
        return result
    snapshots = {}
    for name in ("state.db", "orders.db", "risk.db"):
        command = [sys.executable, "-m", "core.sqlite_backup", "backup", str(output / name),
                   "--snapshot-dir", str(output / "snapshots"), "--retention", "3"]
        result = execute(command)
        snapshots[name] = Path(result.stdout.strip())
    for name in snapshots:
        (output / name).write_bytes(b"offline injected database corruption\n")
    refused = {}
    for name, constructor in (("state.db", StateStore), ("orders.db", OrderStore)):
        try:
            constructor(str(output / name))
        except (sqlite3.DatabaseError, DatabaseIntegrityError):
            refused[name] = True
        else:
            refused[name] = False
    started = perf_counter()
    for name, snapshot in snapshots.items():
        command = [sys.executable, "-m", "core.sqlite_backup", "restore", str(snapshot),
                   str(output / name), "--identity-file", str(identity_file)]
        execute(command)
    after = facts(output, identity)
    elapsed = perf_counter() - started
    # Re-delivery to the recovered authoritative order/fill ledger is a no-op.
    orders = OrderStore(str(output / "orders.db"), identity=identity)
    try:
        no_duplicate_intent = not orders.create_intent(intent, now)
        fill = before["fills"][0]
        no_duplicate_fill = not orders.add_fill(FillRecord(
            fill["fill_id"], intent.client_order_id, fill["exchange_order_id"],
            fill["qty"], fill["price"], fill["fee"], fill["fee_currency"], fill["timestamp"], fill["payload"]))
    finally:
        orders.close()
    checks = {
        "corrupt_stores_fail_closed": all(refused.values()),
        "order_fill_lot_state_budget_facts_equal": before == after,
        "all_three_corrupt_originals_preserved": len(list(output.glob("*.db.corrupt.*"))) == 3,
        "duplicate_intent_suppressed": no_duplicate_intent,
        "duplicate_fill_suppressed": no_duplicate_fill,
        "rto_within_predeclared_target": elapsed <= rto_seconds,
        "rpo_no_pre_backup_committed_fact_lost": before == after,
    }
    report = {
        "schema": "offline-recovery-drill/v1", "passed": all(checks.values()),
        "engineering_status": "pass" if all(checks.values()) else "fail",
        "operational_status": "real_delivery_and_deployment_drill_pending",
        "rto_seconds_observed": elapsed, "rto_seconds_target": rto_seconds,
        "rpo_lost_pre_backup_records": 0 if before == after else None,
        "protocol_sha256": hashlib.sha256((output / "protocol.json").read_bytes()).hexdigest(),
        "checks": checks, "commands": commands, "before": before, "after": after,
        "limitations": ["Stopped writers; no concurrent exchange fills after backup.",
                        "Only synthetic sandbox records; no live notifications or exchange access.",
                        "Local RTO is not a production SLA and excludes human response time."],
    }
    write_json(output / "recovery_drill.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rto-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    report = run_drill(args.output, rto_seconds=args.rto_seconds)
    print(json.dumps({"passed": report["passed"], "rto_seconds": report["rto_seconds_observed"],
                      "output": str(args.output / "recovery_drill.json")}))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
