"""Collect finite public sequenced spot depth, never accounts or orders."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.paper_study import write_json
from core.binance_depth_observer import DepthJournal, observe_depth, verify_depth_journal, displayed_depth_sweep, PublicDepthSnapshot
from core.quote_observations import QuoteObservationStore, utc_now


def source_identity():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
        ("scripts/collect_execution_depth.py", "core/binance_depth_observer.py",
         "core/quote_observations.py", "core/request_budget.py")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--snapshot-limit", type=int, choices=(100, 500, 1000, 5000), default=1000)
    parser.add_argument("--export-levels", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=5.)
    parser.add_argument("--maximum-reconnects", type=int, default=3)
    parser.add_argument("--size-notionals", nargs="+", type=float, default=[500., 5000., 25000.])
    parser.add_argument("--clock-probes", type=int, choices=(0, 1, 2, 3), default=0,
        help="Explicit finite public serverTime comparisons before depth collection")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if any(not math.isfinite(v) or v <= 0 for v in args.size_notionals):
        raise ValueError("positive finite diagnostic notionals required")
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    identity = source_identity()
    manifest = {"schema": "sequenced-depth-observation/v1", "collector_run_id": run_id,
        "started_at": utc_now().isoformat(), "symbols": args.symbols, "duration_seconds": args.duration_seconds,
        "snapshot_limit": args.snapshot_limit, "export_levels": args.export_levels,
        "timeout_seconds": args.timeout_seconds, "maximum_reconnects": args.maximum_reconnects,
        "size_notionals": args.size_notionals, "clock_probes": args.clock_probes, "source_sha256": identity,
        "exchange_id": "binance", "environment": "live", "market_type": "spot",
        "websocket_endpoint": "wss://data-stream.binance.vision", "snapshot_endpoint": "https://api.binance.com/api/v3/depth",
        "official_protocol": "https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#how-to-manage-a-local-order-book-correctly",
        "public_read_only": True, "credentials_used": False, "orders_submitted": 0,
        "clock_semantics": "E is depth publication event time; REST snapshot time is unknown",
        "duration_scope": "collection budget plus one bounded REST timeout and one second websocket close"}
    write_json(args.output / "manifest.json", manifest)
    journal = DepthJournal(args.output / "depth_evidence.jsonl")
    store = QuoteObservationStore(args.output / "execution_quotes.sqlite3")
    failure = None
    try:
        if args.clock_probes:
            fetcher = PublicDepthSnapshot(timeout_seconds=args.timeout_seconds, limit=args.snapshot_limit)
            try:
                for _ in range(args.clock_probes):
                    try:
                        journal.append({**fetcher.clock_probe(), "collector_run_id": run_id})
                    except Exception as exc:
                        journal.append({"kind": "clock_probe_failed", "error_category": type(exc).__name__,
                            "collector_run_id": run_id, "observed_at": utc_now().isoformat()})
            finally:
                fetcher.close()
        health = asyncio.run(observe_depth(symbols=args.symbols, duration_seconds=args.duration_seconds,
            journal=journal, quote_store=store, run_id=run_id, export_levels=args.export_levels,
            snapshot_limit=args.snapshot_limit, timeout_seconds=args.timeout_seconds,
            maximum_reconnects=args.maximum_reconnects))
    except (Exception, KeyboardInterrupt) as exc:
        failure = type(exc).__name__
        health = {"status": "failed", "lifecycle": "stopped", "error_category": failure}
    finally:
        journal.close()
    unchanged = identity == source_identity()
    rows = verify_depth_journal(journal.path)
    books = [r for r in rows if r["kind"] == "synchronized_book"]
    sweeps = [dict(displayed_depth_sweep(book, quote_notional=notional, side=side), quote_id=book["quote_id"])
              for book in books for notional in args.size_notionals for side in ("buy", "sell")]
    write_json(args.output / "sampler_health.json", health)
    write_json(args.output / "quote_observations.json", store.read_all())
    write_json(args.output / "clock_diagnostics.json", {
        "probes": [r for r in rows if r["kind"] in {"public_server_time_probe", "clock_probe_failed"}],
        "synchronized_book_clock_conflicts": sum(not r["clock_consistent"] for r in books),
        "exchange_event_minus_local_receipt_seconds": {
            "min": min((r["exchange_event_minus_local_receipt_seconds"] for r in books), default=None),
            "max": max((r["exchange_event_minus_local_receipt_seconds"] for r in books), default=None)},
        "system_clock_modified": False, "timestamps_corrected": False,
        "conflicted_quotes_excluded_from_execution_calibration": True})
    write_json(args.output / "displayed_depth_diagnostics.json", {"schema": "displayed-depth-diagnostics/v1",
        "status": "observed_depth" if books else "unavailable", "observations": len(books), "sweeps": sweeps,
        "realized_fills": 0, "realized_capacity_validated": False,
        "limitations": ["visible liquidity may cancel before an order reaches the venue",
            "bounded snapshot depth; unseen unchanged levels are unknown", "fees, queue priority, latency and impact require real fills"]})
    summary = {"schema": "sequenced-depth-summary/v1",
        "status": "invalid_source_changed" if not unchanged else "failed" if failure else health["status"],
        "source_identity_unchanged": unchanged, "collector_run_id": run_id,
        "depth_observations": len(books), "quotes": store.health()["persisted_records"],
        "clock_conflicts": sum(not r["clock_consistent"] for r in books),
        "journal_records": journal.sequence, "journal_sha256": journal.digest,
        "realized_fills": 0, "real_venue_calibration": False, "realized_capacity_validated": False,
        "orders_submitted": 0, "output": str(args.output.resolve())}
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    result = main()
    raise SystemExit(0 if result["status"] == "observed_depth" else 2)
