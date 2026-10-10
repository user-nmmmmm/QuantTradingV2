"""Read public OHLCV to measure refresh latency and closed-bar price velocity.

Uses no credentials, orders or private endpoints. Local CSV statistics are
reported separately as historical observations, including their actual dates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import numpy as np
import pandas as pd

from analysis.market_speed import summarize_market_speed
from core.data_fetcher import DataFetcher
from core.market_data import LiveMarketDataAdapter


def distribution(values):
    return {"samples_seconds": values, "count": len(values),
            "p50_seconds": float(np.median(values)) if values else None,
            "p95_seconds": float(np.quantile(values, .95)) if values else None}


def public_sample(venue, args):
    fetcher = DataFetcher(proxy_url=None, request_timeout_ms=args.timeout_ms)
    fetcher.CCXT_MAX_RETRIES = 1
    adapter = LiveMarketDataAdapter(args.symbols, fetcher, timeframe=args.timeframe,
        lookback=args.limit, exchange_id=venue, max_workers=args.workers)
    rounds, per_symbol = [], {symbol: [] for symbol in args.symbols}
    acquired_at = {}
    try:
        for number in range(args.rounds):
            adapter.refresh()
            observed = datetime.now(timezone.utc)
            metrics = dict(adapter.refresh_metrics)
            metrics["observed_at"] = observed.isoformat()
            metrics["round"] = number + 1
            rounds.append(metrics)
            for symbol, duration in metrics["symbol_fetch_seconds"].items():
                if symbol not in adapter.failed_symbols:
                    per_symbol[symbol].append(duration)
                    acquired_at[symbol] = observed
            print(f"venue={venue} round={number+1} refresh={metrics['total_seconds']:.3f}s "
                  f"failures={adapter.failed_symbols}", flush=True)
        speeds = {}
        for symbol, frame in adapter.data_map.items():
            observed = acquired_at[symbol]
            slug = symbol.replace("/", "_").replace(":", "_")
            path = args.output.parent / f"{venue}_{slug}_{args.timeframe}.csv"
            frame.loc[:, ["open", "high", "low", "close", "volume"]].to_csv(path, index_label="timestamp")
            try:
                speeds[symbol] = summarize_market_speed(frame, timeframe=args.timeframe, observed_at=observed)
                speeds[symbol]["snapshot_acquired_at"] = observed.isoformat()
                speeds[symbol]["last_round_succeeded"] = symbol not in adapter.failed_symbols
                speeds[symbol]["snapshot_file"] = str(path.resolve())
                speeds[symbol]["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except ValueError as exc:
                speeds[symbol] = {"error": str(exc)}
        return {"venue": venue, "source": "public_ccxt_rest", "timeframe": args.timeframe,
                "workers": args.workers, "rounds": rounds,
                "successful_request_latency": {symbol: distribution(values) for symbol, values in per_symbol.items()},
                "refresh_latency": distribution([row["total_seconds"] for row in rounds]),
                "price_speed": speeds,
                "limits": "short sample; symbol fetch includes pacing, metadata on cold clients and normalization; no exchange order ACK; "
                          "bar age assumes local UTC clock accuracy; REST bars give no event delivery timestamp"}
    finally:
        adapter.close()
        fetcher.close()


def historical_sample(path, timeframe):
    frame = pd.read_csv(path)
    column = next((name for name in ("timestamp", "date", "datetime", "time", "Unnamed: 0") if name in frame), frame.columns[0])
    frame[column] = pd.to_datetime(frame[column], errors="raise", utc=True)
    frame = frame.set_index(column).sort_index().iloc[-200:]
    return {"source": "local_historical_csv", "file": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "statistics": summarize_market_speed(frame, timeframe=timeframe,
                observed_at=datetime.now(timezone.utc))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchanges", nargs="+", default=["binance", "okx"])
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=3)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--csv", type=Path, nargs="*", default=[])
    parser.add_argument("--csv-timeframe", default="1d")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.limit, args.rounds, args.timeout_ms) <= 0:
        parser.error("limit, rounds and timeout must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Provider exceptions can contain local network configuration; the report
    # records structured failure categories instead of raw exception payloads.
    logging.disable(logging.CRITICAL)
    report = {"schema": "market-speed-measurement/v1",
              "measured_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "public_markets": [public_sample(venue, args) for venue in args.exchanges],
              "historical_markets": [historical_sample(path, args.csv_timeframe) for path in args.csv]}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
