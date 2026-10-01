"""Append-only operator journal for R7 halt and alert dispositions."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def record_incident(
    path: str,
    *,
    event_key: str,
    outcome: str,
    explanation: str,
    operator: str,
) -> dict[str, str]:
    if not event_key or "|" not in event_key:
        raise ValueError("event_key must be '<alert timestamp>|<event>'")
    if outcome not in {"resolved", "explained"}:
        raise ValueError("outcome must be resolved or explained")
    if not explanation.strip() or not operator.strip():
        raise ValueError("explanation and operator are required")
    record = {
        "schema_version": "1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "event_key": event_key,
        "outcome": outcome,
        "explanation": explanation.strip(),
        "operator": operator.strip(),
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Record an R7 incident disposition")
    parser.add_argument("--event-key", required=True)
    parser.add_argument("--outcome", required=True, choices=["resolved", "explained"])
    parser.add_argument("--explanation", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--path", default="reports/incident_resolutions.jsonl")
    args = parser.parse_args()
    print(json.dumps(record_incident(
        args.path,
        event_key=args.event_key,
        outcome=args.outcome,
        explanation=args.explanation,
        operator=args.operator,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
