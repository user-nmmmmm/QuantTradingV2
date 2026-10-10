"""Audit local execution evidence without credentials, network calls or orders."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from analysis.execution_calibration import read_execution_ledger, execution_readiness_report
from analysis.paper_study import write_json
from core.order_latency import read_order_observations, order_observation_sidecar_path
from core.quote_observations import read_quote_observations, utc_now
from core.binance_depth_observer import verify_depth_journal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-store", type=Path, action="append")
    parser.add_argument("--request-store", type=Path, action="append", default=[])
    parser.add_argument("--quote-store", type=Path, action="append", default=[])
    parser.add_argument("--depth-evidence", type=Path, action="append", default=[])
    parser.add_argument("--calibration-report", type=Path, action="append", default=[])
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--minimum-train-days", type=int, default=10)
    parser.add_argument("--minimum-test-days", type=int, default=5)
    parser.add_argument("--minimum-stratum-fills", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cutoff = args.as_of or utc_now().isoformat()
    ledgers, requests, quotes, depths, calibrations, errors = [], [], [], [], [], []
    order_paths = args.order_store or [ROOT / "reports/live_orders.db"]
    request_paths = list(args.request_store)
    for path in order_paths:
        try:
            ledgers.append(read_execution_ledger(path, as_of=cutoff))
        except Exception as exc:
            errors.append({"kind": "order_store", "path": str(path.resolve()), "category": type(exc).__name__})
        sidecar = Path(order_observation_sidecar_path(path))
        if sidecar.is_file():
            request_paths.append(sidecar)
    for kind, paths, reader, target in (
        ("request_store", request_paths, read_order_observations, requests),
        ("quote_store", args.quote_store, read_quote_observations, quotes),
    ):
        for path in sorted(set(p.resolve() for p in paths)):
            try:
                target.extend(reader(path))
            except Exception as exc:
                errors.append({"kind": kind, "path": str(path), "category": type(exc).__name__})
    for path in args.depth_evidence:
        try:
            depths.extend(verify_depth_journal(path))
        except Exception as exc:
            errors.append({"kind": "depth_evidence", "path": str(path.resolve()), "category": type(exc).__name__})
    for path in args.calibration_report:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            # Never import simulated fills into the order ledger.
            calibrations.append({"path": str(path.resolve()), "schema": document.get("schema"),
                "source": document.get("source", "unknown"), "coverage": document.get("coverage", {}),
                "status": document.get("status"), "included_in_live_coverage": False})
        except Exception as exc:
            errors.append({"kind": "calibration_report", "path": str(path.resolve()), "category": type(exc).__name__})
    result = execution_readiness_report(ledgers, requests, quotes, as_of=cutoff,
        symbols=args.symbols, start_date=args.start_date, depth_records=depths,
        minimum_train_days=args.minimum_train_days, minimum_test_days=args.minimum_test_days,
        minimum_stratum_fills=args.minimum_stratum_fills)
    result.update(input_errors=errors, external_calibration_reports=calibrations)
    if errors:
        result["status"] = "invalid_inputs"
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "readiness.json", result)
    for name in ("daily_coverage", "strata"):
        rows = result[name]
        with (args.output / (name + ".csv")).open("w", encoding="utf-8-sig", newline="") as stream:
            if rows:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    summary = {"status": result["status"], "coverage": result["coverage"],
        "calendar": result["calendar"], "blockers": result["blockers"],
        "input_errors": errors, "output": str(args.output.resolve())}
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
