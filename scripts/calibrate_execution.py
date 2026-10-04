"""Join offline execution observations; never submits orders or contacts a venue."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.execution_calibration import execution_sample_report, read_fill_provenance
from analysis.paper_study import write_json
from core.events import EventCodec
from core.order_latency import read_order_observations
from core.quote_observations import read_quote_observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
        help="JSON with events, request_records, independent_quotes, source, as_of")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request-store", type=Path,
        help="Optional append-only request observation sidecar; combined with input request_records")
    parser.add_argument("--quote-store", type=Path,
        help="Independent public BBO sidecar; automatically backward join before decision/submission")
    parser.add_argument("--order-store", type=Path,
        help="Read-only fill provenance: rejects synthetic cumulative fills and unverified matching clocks")
    parser.add_argument("--max-quote-age-seconds", type=float, default=1.)
    parser.add_argument("--prequote-at", choices=("decision", "submit"), default="submit")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Choose a fresh output; existing evidence is immutable")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    events = [EventCodec.decode(json.dumps(e)) if "format" in e else e for e in data.get("events", [])]
    records = data.get("request_records", [])
    if args.request_store:
        records = [*records, *read_order_observations(args.request_store)]
    quotes = data.get("raw_quotes")
    if args.quote_store:
        quotes = [*(quotes or []), *read_quote_observations(args.quote_store)]
    provenance = data.get("fill_provenance")
    if args.order_store:
        provenance = [*(provenance or []), *read_fill_provenance(args.order_store)]
    output = execution_sample_report(events, records,
        independent_quotes=data.get("independent_quotes"),
        terminal_valuations=data.get("terminal_valuations"), as_of=data["as_of"],
        source=data["source"], predictions=data.get("predictions"), raw_quotes=quotes,
        market_context=data.get("market_context"), fill_provenance=provenance,
        max_quote_age_seconds=args.max_quote_age_seconds, prequote_at=args.prequote_at)
    write_json(args.output, output)
    print(json.dumps({"status": output["status"], "coverage": output["coverage"],
        "real_venue_calibration": output["real_venue_calibration"]}))


if __name__ == "__main__":
    main()
