"""Compare ordinary and fast historical bars in paired, same-process runs.

Example::

    python scripts/benchmark_kline_modes.py --bars 720 --symbols 10 --pairs 3 \
        --output outputs/kline_modes.json

The timer covers only ``BacktestEngine.run``. Each run gets a new engine and
deep-copied inputs; serialization and correctness checks happen afterward.
Pair order alternates to reduce drift from warm caches and machine load.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import random
import statistics
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from config.config import config
from core.events import EventCodec
from scripts.benchmark_backtest import (
    ENGINE_SETTINGS,
    _digest,
    _json_safe,
    _records,
    correctness_artifacts,
)
from tests.engine_baseline_harness import build_synthetic_data_map, canonical_json, compare_artifacts

SCHEMA_VERSION = "backtest-kline-modes-v1"
AUDIT_KEYS = (
    "margin_ledger",
    "financing_ledger",
    "execution_audit",
    "breaker_audit",
    "risk_budget_reconciliation",
    "lifecycle",
    "valuation_quality",
)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=_positive_int, default=720)
    parser.add_argument("--symbols", type=_positive_int, default=10)
    parser.add_argument("--pairs", type=_positive_int, default=3,
                        help="number of ordinary/fast run pairs")
    parser.add_argument("--seed", type=int, default=20260812)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/kline_modes.json")
    args = parser.parse_args(argv)
    if args.bars <= ENGINE_SETTINGS["warmup_period"]:
        parser.error(f"--bars must exceed the {ENGINE_SETTINGS['warmup_period']}-bar warmup")
    if not 0 <= args.seed < 2**32:
        parser.error("--seed must be between 0 and 4294967295")
    return args


def _event_facts(events: Any) -> list[str]:
    """Retain all encoded event facts except the wall-clock observation time."""
    facts = []
    for event in events:
        document = json.loads(EventCodec.encode(event))
        document.pop("observed_at")
        facts.append(canonical_json(document))
    return facts


def _snapshot(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "core": correctness_artifacts(result),
        "audit": {key: result[key] for key in AUDIT_KEYS},
        "events": _event_facts(result["event_log"]),
    }


def _require_equivalent(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    for section in ("core", "audit"):
        problems = compare_artifacts(actual[section], expected[section], section)
        if problems:
            raise ValueError(f"{label} {section} differs: " + "; ".join(problems[:5]))
    if actual["events"] != expected["events"]:
        for index, (left, right) in enumerate(zip(actual["events"], expected["events"])):
            if left != right:
                raise ValueError(f"{label} event_log differs at event {index}")
        raise ValueError(
            f"{label} event_log length differs: "
            f"{len(actual['events'])} != {len(expected['events'])}"
        )


def _one_run(
    *, fast_bars: bool, seed: int, data: dict[str, pd.DataFrame],
) -> tuple[float, dict[str, Any]]:
    random.seed(seed)
    np.random.seed(seed)
    engine = BacktestEngine(**ENGINE_SETTINGS, fast_bars=fast_bars)
    inputs = {symbol: frame.copy(deep=True) for symbol, frame in data.items()}
    started = perf_counter()
    result = engine.run(inputs, routing_log_enabled=False)
    elapsed = perf_counter() - started
    return elapsed, _snapshot(result)


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        symbols = [f"ASSET{index}/USDT" for index in range(args.symbols)]
        data = build_synthetic_data_map(seed=args.seed, symbols=symbols, bars=args.bars)
        workload = {
            "bars": args.bars,
            "symbols": symbols,
            "seed": args.seed,
            "data_generator": "tests.engine_baseline_harness.build_synthetic_data_map",
            "data_sha256": _digest({symbol: _records(frame) for symbol, frame in data.items()}),
            "config_sha256": _digest(_json_safe(config._config)),
            "engine_settings": dict(ENGINE_SETTINGS),
            "routing_log_enabled": False,
        }

        reference: dict[str, Any] | None = None
        times: dict[str, list[float]] = {"ordinary": [], "fast": []}
        pairs = []
        for pair_index in range(args.pairs):
            order = (False, True) if pair_index % 2 == 0 else (True, False)
            snapshots: dict[bool, dict[str, Any]] = {}
            elapsed: dict[bool, float] = {}
            for fast_bars in order:
                elapsed[fast_bars], snapshots[fast_bars] = _one_run(
                    fast_bars=fast_bars, seed=args.seed, data=data,
                )
                if reference is None and not fast_bars:
                    reference = snapshots[False]
                elif reference is not None:
                    _require_equivalent(
                        snapshots[fast_bars], reference,
                        f"pair {pair_index + 1} {'fast' if fast_bars else 'ordinary'}",
                    )
            # When the first run of the first pair was ordinary, it becomes
            # the reference above. Every subsequent result has been checked.
            times["ordinary"].append(elapsed[False])
            times["fast"].append(elapsed[True])
            pairs.append({
                "pair": pair_index + 1,
                "order": ["fast" if mode else "ordinary" for mode in order],
                "ordinary_seconds": elapsed[False],
                "fast_seconds": elapsed[True],
                "speedup": elapsed[False] / elapsed[True],
            })

        assert reference is not None
        median_speedup = statistics.median(pair["speedup"] for pair in pairs)
        report = {
            "schema_version": SCHEMA_VERSION,
            "workload": workload,
            "environment": {
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "pandas": pd.__version__,
                "numpy": np.__version__,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "timing": {
                "scope": "BacktestEngine.run only; engine construction, input copies, "
                         "serialization and checks excluded",
                "pairs": pairs,
                "ordinary_median_seconds": statistics.median(times["ordinary"]),
                "fast_median_seconds": statistics.median(times["fast"]),
                "paired_median_speedup": median_speedup,
                "paired_median_time_reduction_pct": (1 - 1 / median_speedup) * 100,
            },
            "correctness": {
                "core_and_audit_equivalent": True,
                "event_log_equal_excluding_observed_at": True,
                "event_count": len(reference["events"]),
                "event_facts_sha256": hashlib.sha256(
                    canonical_json(reference["events"]).encode("utf-8")
                ).hexdigest(),
                "relative_tolerance": 1e-9,
                "absolute_tolerance": 1e-12,
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return report
    finally:
        logging.disable(previous_logging_level)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_benchmark(args)
    except (ValueError, OSError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    timing = report["timing"]
    print(f"Ordinary median: {timing['ordinary_median_seconds']:.6f}s; "
          f"fast median: {timing['fast_median_seconds']:.6f}s "
          f"({args.pairs} pairs, {args.bars} bars, {args.symbols} symbols)")
    print(f"Paired median speedup: {timing['paired_median_speedup']:.3f}x; "
          f"time reduction: {timing['paired_median_time_reduction_pct']:.1f}%")
    print(f"Core, audit and {report['correctness']['event_count']} event facts equivalent.")
    print(f"Saved: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
