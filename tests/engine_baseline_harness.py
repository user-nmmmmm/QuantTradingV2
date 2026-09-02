"""Utilities for a real-engine backtest equivalence baseline.

Unlike ``tests/baseline_harness.py`` (which round-trips hand-authored fixture
JSON without ever calling :class:`~backtest.engine.BacktestEngine`), this
module actually drives the production code path — data generation, the
historical adapter, ``EventProcessor``, ``Router``, strategies, and
``Broker`` — and serializes the result into a JSON-comparable dict.

The whole pipeline is deterministic for a fixed synthetic dataset: no
strategy uses ``random``/``np.random``, and the fields captured in
``Broker.trades`` never include the one genuinely random value in the stack
(``TradingEventPipeline.run_id``, a plain ``uuid4()``). See
``docs/2026_08_12_performance_technical_analysis.md`` batch-3 planning notes
for the analysis that established this.
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from core.data_fetcher import DataFetcher

SCHEMA_VERSION = "engine-baseline-v1"
DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
DEFAULT_BARS = 180
DEFAULT_WARMUP_PERIOD = 30
DEFAULT_SEED = 20260812


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Cross-machine float tolerance for the recorded baseline.
#
# The engine is bit-deterministic within one process, but the last ULP of a
# reduction (e.g. the benchmark's row-wise mean over three symbols) can differ
# between the machine that recorded the fixture and the CI runner, because
# SIMD summation order depends on the CPU and array layout. Comparing 17
# significant digits byte-for-byte therefore fails for reasons unrelated to
# behavior — this repo has hit it repeatedly.
#
# 1e-9 relative is many orders of magnitude tighter than any genuine behavior
# change: a different fill, size, or trade count moves these values in the
# first few significant digits, and non-numeric fields (counts, timestamps,
# statuses, symbols) are still compared exactly.
BASELINE_REL_TOL = 1e-9
BASELINE_ABS_TOL = 1e-12


def compare_artifacts(actual: Any, expected: Any, path: str = "artifacts") -> list[str]:
    """Structurally diff two artifact bundles, tolerating float noise.

    Returns a list of human-readable mismatch descriptions (empty when the
    bundles agree). Numbers compare with :data:`BASELINE_REL_TOL`; everything
    else — including container shapes and key sets — compares exactly.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        # bool is an int subclass; compare identity of type before numbers.
        if actual != expected or type(actual) is not type(expected):
            return [f"{path}: {actual!r} != {expected!r}"]
        return []

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if math.isclose(
            float(actual), float(expected),
            rel_tol=BASELINE_REL_TOL, abs_tol=BASELINE_ABS_TOL,
        ):
            return []
        return [f"{path}: {actual!r} != {expected!r}"]

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected a mapping, got {type(actual).__name__}"]
        problems = []
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            problems.append(f"{path}: missing keys {missing}")
        if extra:
            problems.append(f"{path}: unexpected keys {extra}")
        for key in sorted(set(expected) & set(actual)):
            problems.extend(compare_artifacts(actual[key], expected[key], f"{path}.{key}"))
        return problems

    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            return [f"{path}: expected a sequence, got {type(actual).__name__}"]
        if len(actual) != len(expected):
            return [f"{path}: length {len(actual)} != {len(expected)}"]
        problems = []
        for index, (left, right) in enumerate(zip(actual, expected)):
            problems.extend(compare_artifacts(left, right, f"{path}[{index}]"))
        return problems

    if actual != expected:
        return [f"{path}: {actual!r} != {expected!r}"]
    return []


def data_digest(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def build_synthetic_data_map(
    seed: int = DEFAULT_SEED,
    symbols=DEFAULT_SYMBOLS,
    bars: int = DEFAULT_BARS,
) -> Dict[str, pd.DataFrame]:
    """Deterministic multi-regime OHLCV data for each symbol.

    ``generate_scenario`` draws from the global ``np.random`` state and takes
    no seed of its own, so the caller must seed once up front; seeding once
    and generating symbols in a fixed order keeps the whole map reproducible.
    """
    np.random.seed(seed)
    fetcher = DataFetcher()
    start_date = "2024-01-01"
    end_date = (pd.Timestamp(start_date) + pd.Timedelta(days=bars - 1)).strftime("%Y-%m-%d")
    return {
        symbol: fetcher.generate_scenario(symbol, start_date, end_date)
        for symbol in symbols
    }


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (int, float, str, bool)):
        # Enum members (BacktestOrderStatus, OrderType, ...)
        return value.value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, float) and np.isnan(value):
        return None
    return value


def run_engine(
    data_map: Dict[str, pd.DataFrame],
    *,
    initial_capital: float = 10000.0,
    slippage: float = 0.0005,
    warmup_period: int = DEFAULT_WARMUP_PERIOD,
) -> Dict[str, Any]:
    """Run the real BacktestEngine and return a JSON-serializable artifact bundle."""
    engine = BacktestEngine(
        initial_capital=initial_capital,
        slippage=slippage,
        random_slip=False,
        warmup_period=warmup_period,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        result = engine.run(
            data_map,
            routing_log_path=str(Path(temp_dir) / "routing_log.csv"),
        )
        report_gen = ReportGenerator(str(Path(temp_dir) / "report"))
        metrics = report_gen.generate(
            trades=result["trades"],
            equity_curve=result["equity_curve"],
            benchmark_curve=result["benchmark"],
            metrics_only=True,
            # Engine-produced behavior, so the baseline should pin it: a
            # regression that stops strategies observing their own closures
            # shows up here as changed lifecycle coverage.
            close_events=result.get("close_events"),
        )

    # Renamed, not reassigned positionally: the curve carries exposure columns
    # beyond equity/cash, and overwriting the whole column list would either
    # raise or mislabel them.
    equity_curve = result["equity_curve"].rename_axis("timestamp").reset_index()
    benchmark = result["benchmark"]

    return {
        "trades": _jsonify(result["trades"]),
        "equity_curve": _jsonify(equity_curve.to_dict(orient="records")),
        "benchmark": (
            _jsonify(
                [
                    {"timestamp": ts, "value": value}
                    for ts, value in benchmark.items()
                ]
            )
            if benchmark is not None
            else None
        ),
        "metrics": _jsonify(metrics),
    }
