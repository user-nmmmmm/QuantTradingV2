"""Create tests/fixtures/backtest/engine/engine_baseline_v4.json once.

Only run this after a deliberate, reviewed behavior change to the backtest
engine (data adapters, EventProcessor, Router, strategies, or Broker).
Running it blindly to "fix" a failing equivalence test defeats its purpose —
diff the old and new baseline first and confirm the change is intentional.

Usage:
    python -m tests.generate_engine_baseline
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from tests.engine_baseline_harness import (
    DEFAULT_BARS,
    DEFAULT_SEED,
    DEFAULT_SYMBOLS,
    DEFAULT_WARMUP_PERIOD,
    SCHEMA_VERSION,
    build_synthetic_data_map,
    canonical_json,
    data_digest,
    run_engine,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "backtest" / "engine" / "engine_baseline_v4.json"
PREVIOUS_FIXTURE_PATH = FIXTURE_PATH.with_name("engine_baseline_v3.json")


def build_bundle() -> dict:
    data_map = build_synthetic_data_map(
        seed=DEFAULT_SEED, symbols=DEFAULT_SYMBOLS, bars=DEFAULT_BARS
    )
    artifacts = run_engine(data_map, warmup_period=DEFAULT_WARMUP_PERIOD)

    if artifacts["metrics"].get("TotalTrades", 0) <= 0:
        raise SystemExit(
            "Refusing to write a baseline with zero closed trades — the "
            "dataset/seed no longer exercises the strategy pipeline."
        )

    data_records = {
        symbol: json.loads(frame.reset_index().to_json(orient="records", date_format="iso"))
        for symbol, frame in data_map.items()
    }
    data_summary = {
        symbol: {
            "rows": len(frame),
            "start": frame.index.min().isoformat(),
            "end": frame.index.max().isoformat(),
        }
        for symbol, frame in data_map.items()
    }

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "behavior_contract": "roadmap-v4-independent-fact-metric-reporting/v1",
            "text_hash_policy": "UTF-8 text with CRLF and CR normalized to LF",
            "supersedes": PREVIOUS_FIXTURE_PATH.name,
            "previous_baseline_lf_sha256": hashlib.sha256(PREVIOUS_FIXTURE_PATH.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
            "config_lf_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] / "config" / "params.yaml").read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
            "seed": DEFAULT_SEED,
            "symbols": list(DEFAULT_SYMBOLS),
            "bars_per_symbol": DEFAULT_BARS,
            "warmup_period": DEFAULT_WARMUP_PERIOD,
            "data_summary": data_summary,
            # Hashes the *generated* OHLCV values, not just row counts, so a
            # silent change in generate_scenario's output (e.g. a numpy
            # version bump altering the random stream) is caught even if it
            # happens not to move any downstream metric.
            "data_sha256": data_digest(data_records),
        },
        "artifacts": artifacts,
    }
    return bundle


def main() -> None:
    if FIXTURE_PATH.exists():
        raise SystemExit("Refusing to overwrite a frozen engine baseline; review the diff and select a new version.")
    bundle = build_bundle()

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(bundle) + "\n")
    print(f"Wrote {FIXTURE_PATH} ({len(bundle['artifacts']['trades'])} trades, "
          f"{bundle['artifacts']['metrics'].get('TotalTrades')} closed trades)")


if __name__ == "__main__":
    main()
