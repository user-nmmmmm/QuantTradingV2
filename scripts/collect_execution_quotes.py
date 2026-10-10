"""Collect independent public BBO facts only; no credentials and no orders."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]

from analysis.execution_calibration import execution_sample_report
from analysis.paper_study import write_json
from core.quote_observations import BackgroundQuoteSampler, QuoteObservationStore, utc_now, read_quote_observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--market-type", default="spot",
                        choices=("spot", "spot_margin", "swap", "future", "perpetual"))
    parser.add_argument("--environment", default="live", choices=("live", "sandbox"))
    parser.add_argument("--duration-seconds", type=float, default=30.)
    parser.add_argument("--interval-seconds", type=float, default=1.)
    parser.add_argument("--timeout-seconds", type=float, default=5.)
    parser.add_argument("--stale-after-seconds", type=float, default=5.)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-store", type=Path,
        help="Append this new bounded session to an existing quote store; old output evidence is never overwritten")
    args = parser.parse_args(argv)
    if not 0 < args.duration_seconds <= 86400:
        raise ValueError("duration must be positive and at most one day")
    if args.resume_store is not None and not args.resume_store.is_file():
        raise ValueError("resume-store must be an existing durable quote database")
    args.output.mkdir(parents=True, exist_ok=False)
    store = QuoteObservationStore(args.resume_store or args.output/"execution_quotes.sqlite3")
    existing = store.read_all()
    starting_sequence = existing[-1]["sequence"] if existing else 0
    sampler = BackgroundQuoteSampler(exchange_id=args.exchange, symbols=args.symbols,
        market_type=args.market_type, environment=args.environment, store=store,
        interval_seconds=args.interval_seconds, timeout_seconds=args.timeout_seconds,
        stale_after_seconds=args.stale_after_seconds)
    manifest = {"schema": "public-quote-collection/v1", "source": args.environment,
        "started_at": utc_now().isoformat(), "duration_seconds": args.duration_seconds,
        "exchange_id": args.exchange, "market_type": args.market_type, "symbols": args.symbols,
        "collector_run_id": sampler.run_id, "starting_sequence": starting_sequence,
        "quote_store": str(Path(store.path).resolve()), "resumed_store": args.resume_store is not None,
        "interval_seconds": args.interval_seconds, "timeout_seconds": args.timeout_seconds,
        "stale_after_seconds": args.stale_after_seconds,
        "public_read_only": True, "credentials_used": False, "orders_submitted": 0}
    write_json(args.output/"manifest.json", manifest)
    sampler.start()
    try:
        Event().wait(args.duration_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        health = sampler.stop(join_timeout_seconds=min(args.timeout_seconds+1., 10.))
        write_json(args.output/"sampler_health.json", health)
    quotes = [row for row in read_quote_observations(store.path, after_sequence=starting_sequence)
              if row["collector_run_id"] == sampler.run_id]
    write_json(args.output/"quote_observations.json", quotes)
    # Public quotes alone never manufacture an execution sample.
    diagnostic = execution_sample_report([], raw_quotes=quotes, request_records=[],
        market_context={}, fill_provenance=[], as_of=utc_now().isoformat(),
        source="live" if args.environment == "live" else "sandbox")
    write_json(args.output/"calibration_without_fills.json", diagnostic)
    summary = {"status": "observed_quotes" if quotes else "unavailable",
        "observations": len(quotes), "health": health["status"], "lifecycle": health["lifecycle"],
        "error_categories": sorted({s["last_error_category"] for s in health["symbols"].values()
                                    if s["last_error_category"]}),
        "calibration_status": diagnostic["status"], "fills": 0,
        "real_venue_calibration": False, "output": str(args.output.resolve())}
    summary.update(starting_sequence=starting_sequence,
                   last_sequence=quotes[-1]["sequence"] if quotes else starting_sequence,
                   retained_store_observations=store.health()["persisted_records"])
    write_json(args.output/"summary.json", summary)
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    main()
