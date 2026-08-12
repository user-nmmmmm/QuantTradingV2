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
        )

    equity_curve = result["equity_curve"].reset_index()
    equity_curve.columns = ["timestamp", "equity", "cash"]
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
