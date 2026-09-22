"""Measure the offline engine and verify identical results before reporting speedup.

Example: python scripts/benchmark_backtest.py --output outputs/before.json
         python scripts/benchmark_backtest.py --reference outputs/before.json
"""
from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import json
import logging
import platform
import random
import statistics
import sys
import tracemalloc
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
from tests.engine_baseline_harness import (
    _jsonify, build_synthetic_data_map, canonical_json, compare_artifacts,
)

SCHEMA_VERSION = "backtest-performance-v1"
ENGINE_SETTINGS = {
    "initial_capital": 10000.0,
    "slippage": 0.0005,
    "random_slip": False,
    "warmup_period": 30,
    "timeframe": "1d",
    "run_id": "performance-baseline",
    "calculate_benchmarks": True,
}


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=_positive_int, default=720)
    parser.add_argument("--symbols", type=_positive_int, default=10,
                        help="number of synthetic symbols")
    parser.add_argument("--repeats", type=_positive_int, default=3)
    parser.add_argument("--seed", type=int, default=20260812)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/backtest_performance.json")
    parser.add_argument("--reference", type=Path, help="earlier JSON with the same workload")
    parser.add_argument("--profile", type=Path,
                        help="write cProfile data from an extra run excluded from timings")
    parser.add_argument("--memory", action="store_true",
                        help="measure traced allocations in an extra run excluded from timings")
    args = parser.parse_args(argv)
    if args.bars <= ENGINE_SETTINGS["warmup_period"]:
        parser.error("--bars must exceed the 30-bar warmup")
    if not 0 <= args.seed < 2**32:
        parser.error("--seed must be between 0 and 4294967295")
    report_paths = [args.output.resolve()]
    if args.reference:
        if args.reference.resolve() == args.output.resolve():
            parser.error("--output must differ from --reference to preserve the baseline")
        report_paths.append(args.reference.resolve())
    if args.profile and args.profile.resolve() in report_paths:
        parser.error("--profile must differ from --output and --reference")
    return args


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    value = _jsonify(value.item() if isinstance(value, np.generic) else value)
    return None if isinstance(value, float) and np.isnan(value) else value


def _records(value: pd.DataFrame | pd.Series | None) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Series):
        value = value.rename("value").to_frame()
    return _json_safe(value.rename_axis("timestamp").reset_index().to_dict("records"))


def correctness_artifacts(result: dict[str, Any]) -> dict[str, Any]:
    """Pin the numerical results without nondeterministic event wall-clock fields."""
    artifacts = {
        name: _records(result[name])
        for name in (
            "equity_curve", "benchmark", "benchmark_fixed", "benchmark_dynamic",
            "benchmark_weights", "benchmark_turnover", "benchmark_costs",
        )
    }
    artifacts.update({
        name: _json_safe(result[name])
        for name in (
            "trades", "benchmark_metadata", "accounting_check", "close_events",
            "account_mode", "alignment_mode", "account_cost_contract",
            "terminal_valuation", "valuation_quality", "effective_max_holding_days",
        )
    })
    return artifacts


def validate_reference(reference: dict[str, Any], workload: dict[str, Any]) -> None:
    if not isinstance(reference, dict) or reference.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("reference schema differs from this benchmark")
    # Exact identity, including config and data hashes; float tolerance is only
    # for output values and must never hide a change in settings.
    if reference.get("workload") != workload:
        raise ValueError("reference workload/config/data differ; speedup comparison refused")
    median = reference.get("timing", {}).get("median_seconds")
    if isinstance(median, bool) or not isinstance(median, (int, float)):
        raise ValueError("reference must contain a positive finite median_seconds")
    if not np.isfinite(median) or median <= 0:
        raise ValueError("reference must contain a positive finite median_seconds")
    if not isinstance(reference.get("artifacts"), dict):
        raise ValueError("reference has no correctness artifacts")


def _require_equivalent(actual: dict, expected: dict, label: str, *, exact: bool = False) -> None:
    if exact:
        if canonical_json(actual) != canonical_json(expected):
            raise ValueError(f"{label} results differ; repeats must match exactly")
        return
    problems = compare_artifacts(actual, expected)
    if problems:
        raise ValueError(f"{label} results differ: " + "; ".join(problems[:5]))


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        symbols = [f"ASSET{index}/USDT" for index in range(args.symbols)]
        data = build_synthetic_data_map(seed=args.seed, symbols=symbols, bars=args.bars)
        resolved_config = _json_safe(config._config)
        workload = {
            "bars": args.bars,
            "symbols": symbols,
            "seed": args.seed,
            "data_generator": "tests.engine_baseline_harness.build_synthetic_data_map",
            "data_sha256": _digest({symbol: _records(frame) for symbol, frame in data.items()}),
            "config_sha256": _digest(resolved_config),
            "engine_settings": dict(ENGINE_SETTINGS),
            "routing_log_enabled": False,
        }
        reference = None
        if args.reference:
            reference = json.loads(args.reference.read_text(encoding="utf-8"))
            validate_reference(reference, workload)

        artifacts = None
        wall_times = []
        for repeat in range(args.repeats):
            # Engine construction, input copies and seeds are outside the timed
            # interval. Every measured run starts with a fresh engine and data.
            random.seed(args.seed)
            np.random.seed(args.seed)
            engine = BacktestEngine(**ENGINE_SETTINGS)
            inputs = {symbol: frame.copy(deep=True) for symbol, frame in data.items()}
            started = perf_counter()
            result = engine.run(inputs, routing_log_enabled=False)
            wall_times.append(perf_counter() - started)
            current = correctness_artifacts(result)
            if artifacts is None:
                artifacts = current
            else:
                _require_equivalent(current, artifacts, f"repeat {repeat + 1}", exact=True)
            del result, engine, inputs

        if reference:
            _require_equivalent(artifacts, reference["artifacts"], "reference")

        if args.profile:
            args.profile.parent.mkdir(parents=True, exist_ok=True)
            random.seed(args.seed)
            np.random.seed(args.seed)
            engine = BacktestEngine(**ENGINE_SETTINGS)
            inputs = {symbol: frame.copy(deep=True) for symbol, frame in data.items()}
            profiler = cProfile.Profile()
            result = profiler.runcall(engine.run, inputs, routing_log_enabled=False)
            _require_equivalent(correctness_artifacts(result), artifacts, "profile", exact=True)
            profiler.dump_stats(str(args.profile))
            del result, engine, inputs

        memory = None
        if args.memory:
            if tracemalloc.is_tracing():
                raise ValueError("--memory requires exclusive use of tracemalloc")
            random.seed(args.seed)
            np.random.seed(args.seed)
            inputs = {symbol: frame.copy(deep=True) for symbol, frame in data.items()}
            gc.collect()
            tracemalloc.start(1)
            try:
                engine = BacktestEngine(**ENGINE_SETTINGS)
                result = engine.run(inputs, routing_log_enabled=False)
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            # Serialization and result comparisons must not inflate the probe.
            _require_equivalent(correctness_artifacts(result), artifacts, "memory", exact=True)
            memory = {
                "scope": "traced allocations during extra engine construction + run after timed repeats; "
                         "excludes inputs, imports, existing caches, serialization and tracer overhead; not process RSS",
                "retained_bytes": retained, "peak_bytes": peak,
                "retained_mib": retained / 1024**2, "peak_mib": peak / 1024**2,
            }
            del result, engine, inputs

        report = {
            "schema_version": SCHEMA_VERSION,
            "workload": workload,
            "resolved_config": resolved_config,
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
                "scope": "BacktestEngine.run only; logging disabled; profile excluded",
                "repeats": args.repeats,
                "wall_seconds": wall_times,
                "median_seconds": statistics.median(wall_times),
            },
            "correctness": {"repeats_identical": True, "reference_relative_tolerance": 1e-9,
                            "reference_absolute_tolerance": 1e-12},
            "artifacts": artifacts,
        }
        if reference:
            report["comparison"] = {
                "reference": str(args.reference.resolve()),
                "artifacts_equivalent": True,
                "environment_matches": report["environment"] == reference.get("environment"),
                "reference_median_seconds": reference["timing"]["median_seconds"],
                "speedup": reference["timing"]["median_seconds"] / report["timing"]["median_seconds"],
            }
        if args.profile:
            report["profile"] = str(args.profile.resolve())
        if memory is not None:
            report["memory"] = memory
            reference_memory = (reference or {}).get("memory", {})
            reference_peak = reference_memory.get("peak_bytes")
            if (reference_memory.get("scope") == memory["scope"]
                    and type(reference_peak) in (int, float)
                    and np.isfinite(reference_peak) and reference_peak > 0):
                report["comparison"]["traced_peak_reduction_pct"] = (1 - peak / reference_peak) * 100
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
                               + "\n", encoding="utf-8")
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
    print(f"Median: {report['timing']['median_seconds']:.6f}s "
          f"({args.repeats} runs, {args.bars} bars, {args.symbols} symbols)")
    if "comparison" in report:
        comparison = report["comparison"]
        print(f"Equivalent results; speedup: {comparison['speedup']:.3f}x")
        if not comparison["environment_matches"]:
            print("Environment differs from reference; timings also reflect that difference.")
    if "memory" in report:
        print(f"Traced memory: retained {report['memory']['retained_mib']:.3f} MiB, "
              f"peak {report['memory']['peak_mib']:.3f} MiB (not process RSS)")
    print(f"Saved: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
