"""Pair original/current live adapters under explicit synthetic I/O latency.

No external requests or real orders. Warm-up, input copies and equivalence
checks are outside timed intervals. Each adapter retains its worker pool.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import platform
import statistics
import sys
from pathlib import Path
from threading import Lock
from time import perf_counter, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core.market_data import HistoricalMarketDataAdapter, LiveMarketDataAdapter
from composition.factory import build_router, build_state_machine, build_strategy_registry, build_risk_manager
from config.config import config
from core.broker import Broker
from core.portfolio import Portfolio
from core.runtime import EventProcessor


class SyntheticFetcher:
    def __init__(self, frames, delay):
        self.frames, self.delay = frames, delay
        self.version = 0
        self.lock = Lock()
        self.active = self.peak = 0

    def fetch_ccxt(self, symbol, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            sleep(self.delay)
            frame = self.frames[symbol].copy(deep=True)
            if self.version:
                frame.iloc[-1, frame.columns.get_loc("close")] += self.version * .01
            return frame
        finally:
            with self.lock:
                self.active -= 1


def frames_for(count, rows=160):
    close = np.linspace(100., 130., rows)
    close[-1] += 5.
    frame = pd.DataFrame({"open": close, "high": close + 1., "low": close - 1.,
                          "close": close, "volume": 100000.},
                         index=pd.date_range("2026-01-01", periods=rows, freq="min"))
    return {f"ASSET{i}/USDT": frame.copy(deep=True) for i in range(count)}


def summarize(samples):
    return {"samples_seconds": samples, "p50_seconds": statistics.median(samples),
            "p95_seconds": float(np.quantile(samples, .95)), "sample_count": len(samples)}


def adapter_pair(original, count, workers, pairs, delay, changing):
    frames = frames_for(count)
    before_fetch, after_fetch = (SyntheticFetcher(frames, delay) for _ in range(2))
    before = original(list(frames), before_fetch, lookback=160, timeframe="1m")
    after = LiveMarketDataAdapter(list(frames), after_fetch, lookback=160, timeframe="1m", max_workers=workers)
    samples = {"before": [], "after": []}
    try:
        before.refresh()
        after.refresh()
        for pair in range(pairs):
            before_fetch.version = after_fetch.version = pair + 1 if changing else 0
            ordering = (("before", before), ("after", after))
            if pair % 2:
                ordering = ordering[::-1]
            for name, adapter in ordering:
                started = perf_counter()
                adapter.refresh()
                samples[name].append(perf_counter() - started)
            for symbol in frames:
                pd.testing.assert_frame_equal(before.data_map[symbol], after.data_map[symbol], check_freq=False)
        ratios = [old / new for old, new in zip(samples["before"], samples["after"])]
        return {"symbols": count, "workers": workers, "changing": changing,
                "synthetic_request_delay_seconds": delay,
                "before": summarize(samples["before"]), "after": summarize(samples["after"]),
                "median_paired_speedup": statistics.median(ratios),
                "peak_fetch_concurrency": after_fetch.peak,
                "refresh_metrics": after.refresh_metrics, "equivalent": True}
    finally:
        after.close()


def decision_samples(count, repeats):
    samples, counts = [], []
    for _ in range(repeats):
        portfolio = Portfolio(1000000.)
        broker = Broker(portfolio)
        strategies = build_strategy_registry(config)
        router = build_router(strategies, config)
        market = HistoricalMarketDataAdapter(frames_for(count), timeframe="1m")
        event = next(market.stream(start_at=market.timestamps[-1]))
        processor = EventProcessor(portfolio=portfolio, execution=broker,
            risk_manager=build_risk_manager(config), state_machine=build_state_machine(config),
            router=router, allocator=router.allocator)
        started = perf_counter()
        processor.process(event, execute_market_event=False)
        samples.append(perf_counter() - started)
        counts.append(len(broker.pending_orders))
    if len(set(counts)) != 1:
        raise ValueError("local decision order counts differ across repetitions")
    return {"symbols": count, "timing": summarize(samples), "accepted_simulated_orders": counts[0],
            "scope": "one shared bar decision including states, strategy candidates, risk and simulated order submission; no venue ACK"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", type=int, nargs="+", default=[10, 60])
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--io-ms", type=float, default=20.)
    args = parser.parse_args()
    if args.pairs < 1 or any(n < 1 for n in args.symbols) or args.io_ms < 0:
        parser.error("counts must be positive and I/O delay nonnegative")
    spec = importlib.util.spec_from_file_location("original_market_adapter", args.baseline_source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    logging.disable(logging.CRITICAL)
    rows = []
    for count in args.symbols:
        for changing in (False, True):
            for workers in args.workers:
                result = adapter_pair(module.LiveMarketDataAdapter, count, workers, args.pairs, args.io_ms / 1000., changing)
                rows.append(result)
                print(f"symbols={count} changed={changing} workers={workers} "
                      f"before={result['before']['p50_seconds']:.4f}s after={result['after']['p50_seconds']:.4f}s "
                      f"speedup={result['median_paired_speedup']:.2f}x", flush=True)
    report = {"schema": "live-latency-benchmark/v1",
              "environment": {"python": platform.python_version(), "pandas": pd.__version__,
                              "numpy": np.__version__, "platform": platform.platform()},
              "scope": "offline synthetic I/O; paired and alternating; warmed pools; not real exchange latency",
              "adapter_comparisons": rows,
              "local_decisions": [decision_samples(count, args.pairs) for count in args.symbols]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
