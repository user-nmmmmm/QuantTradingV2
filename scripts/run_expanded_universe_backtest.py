"""Fresh Binance-only 30-vs-60 research backtest; no private API or policy changes.

2016 is a requested boundary, not invented Binance history. Selection is a
predeclared static research basket, NOT a survivorship-free historical index.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import requests
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from config.config import config
from core.data import DataHandler
from core.reproducibility import canonical_json, code_identity, deterministic_result_digest, sha256_file
from scripts.fetch_binance_data import DEFAULT_SYMBOLS

EXTRA_BASES = "NEO EOS IOTA DASH ZEC XMR XTZ ALGO VET THETA FTM RUNE EGLD SAND MANA AXS GALA ENJ CHZ CRV SNX COMP MKR SUSHI YFI KAVA ZIL BAT 1INCH DYDX".split()
ENDPOINT = "https://data-api.binance.vision/api/v3/klines"


def save(path, value):
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_symbol(symbol, folder, start, end):
    cursor = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    boundary = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    rows, pages = [], []
    with requests.Session() as session:
        while cursor < boundary:
            params = {"symbol": symbol.replace("/", ""), "interval": "1d", "startTime": cursor,
                      "endTime": boundary - 1, "limit": 1000}
            for attempt in range(3):
                try:
                    response = session.get(ENDPOINT, params=params, timeout=25)
                    response.raise_for_status()
                    batch = response.json()
                    if not isinstance(batch, list):
                        raise ValueError(f"Unexpected response: {batch}")
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
            pages.append({"url": response.url, "rows": len(batch)})
            if not batch:
                break
            rows.extend(batch)
            following = int(batch[-1][0]) + 86_400_000
            if following <= cursor:
                raise ValueError("Kline pagination did not advance")
            cursor = following
            if len(batch) < 1000:
                break
            time.sleep(0.15)
    if not rows:
        raise ValueError("No historical klines returned")
    frame = pd.DataFrame([row[:6] for row in rows], columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    frame = frame.set_index("timestamp").astype(float).sort_index()
    if frame.index.has_duplicates or not np.isfinite(frame.to_numpy()).all():
        raise ValueError("Duplicate timestamps or non-finite OHLCV")
    frame = frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    if len(frame) < 31:
        raise ValueError("Fewer than 31 historical bars")
    filename = symbol.replace("/", "_") + ".csv"
    path = folder / filename
    frame.to_csv(path, float_format="%.17g")
    gaps = int(((frame.index.to_series().diff().dt.total_seconds() / 86400).fillna(1) - 1).clip(lower=0).sum())
    return {"symbol": symbol.replace("/", "-"), "file": filename, "rows": len(frame),
            "first": frame.index.min(), "last": frame.index.max(), "missing_internal_days": gaps,
            "ends_before_requested_end": frame.index.max() < pd.Timestamp(end),
            "sha256": sha256_file(path), "pages": pages}


def run_arm(name, symbols, root, inventory, capital, start, end):
    folder = root / name
    folder.mkdir(exist_ok=False)
    frames = {}
    for symbol in symbols:
        entry = inventory["symbols"][symbol]
        path = root / "data_inputs" / entry["file"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError("Input hash mismatch")
        frames[symbol] = DataHandler.annotate_quality(pd.read_csv(path, index_col="timestamp", parse_dates=True, float_precision="round_trip"))
    random.seed(42)
    np.random.seed(42)
    print(f"BACKTEST {name}: {len(frames)} symbols", flush=True)
    engine = BacktestEngine(initial_capital=capital, timeframe="1d", alignment_mode="union",
                            benchmark_mode="fixed", run_id="expanded-universe-2016-20260831")
    result = engine.run(frames, routing_log_enabled=False)
    reporter = ReportGenerator(str(folder))
    metrics = reporter.generate(result["trades"], result["equity_curve"], metrics_only=True,
                                benchmark_curve=result["benchmark"], close_events=result["close_events"],
                                lifecycle=result["lifecycle"], strategy_health=result["strategy_health"],
                                protective_stops=result["protective_stop_summary"])
    result["equity_curve"].to_csv(folder / "equity.csv")
    for key in ("trades", "breaker_audit", "strategy_health_transitions", "strategy_health_cohorts",
                "stop_order_audit", "risk_budget_reconciliation", "financing_ledger", "execution_audit"):
        pd.DataFrame(result[key]).to_csv(folder / f"{key}.csv", index=False)
    for key in ("strategy_health", "breaker_state", "accounting_check", "lifecycle", "account_cost_contract"):
        save(folder / f"{key}.json", result[key])
    save(folder / "metrics.json", metrics)
    save(folder / "result_digest.json", deterministic_result_digest(result))
    save(folder / "resolved_config.json", config._config)
    metadata = {"RequestedStart": start, "RequestedEnd": end, "Capital": capital,
                "Symbols": symbols, "ActualStart": str(result["equity_curve"].index.min()),
                "ActualEnd": str(result["equity_curve"].index.max()),
                "Warning": "Static retrospective universe; no pre-Binance bars; independent second-source audit not performed."}
    reporter._save_report_text(metrics, metadata, metrics.get("ExtendedAnalytics", {}))
    reporter._plot_equity(result["equity_curve"], result["benchmark"])
    curve = result["equity_curve"]["equity"]
    if not result["accounting_check"]["ok"]:
        raise ValueError("Accounting reconciliation failed")
    summary = {"symbols": symbols, "symbol_count": len(symbols), "requested_start": start,
               "requested_end": end, "actual_start": curve.index.min(), "actual_end": curve.index.max(),
               "initial_capital": capital, "final_equity": float(curve.iloc[-1]),
               "return_pct": (float(curve.iloc[-1]) / capital - 1) * 100,
               "max_drawdown_pct": float((1 - curve / curve.cummax()).max()) * 100,
               "total_trades": metrics.get("TotalTrades"), "profit_factor": metrics.get("ProfitFactor"),
               "fills": len(result["trades"]), "strategy_health": result["strategy_health"],
               "breaker_state": result["breaker_state"], "accounting_check": result["accounting_check"]}
    save(folder / "summary.json", summary)
    print(f"DONE {name}: equity={curve.iloc[-1]:.2f}, trades={metrics.get('TotalTrades')}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--reuse-download", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    requested = DEFAULT_SYMBOLS + [f"{symbol}/USDT" for symbol in EXTRA_BASES]
    assert len(requested) == len(set(requested)) == 60
    logging.disable(logging.CRITICAL)
    if not args.reuse_download:
        root.mkdir(parents=True, exist_ok=False)
        (root / "data_inputs").mkdir()
        inventory = {"requested_start": args.start, "requested_end_inclusive": args.end,
                     "requested_symbols": requested, "timeframe": "1d", "endpoint": ENDPOINT,
                     "downloaded_at": datetime.now(timezone.utc), "code": code_identity(ROOT),
                     "script_sha256": sha256_file(__file__), "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                     "selection": "predeclared static 60-symbol research basket; no performance ranking",
                     "survivorship_bias_controlled": False, "second_source_verified": False,
                     "symbols": {}, "failures": {}}
        save(root / "download_manifest.json", inventory)
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(fetch_symbol, symbol, root / "data_inputs", args.start, args.end): symbol for symbol in requested}
            for future in as_completed(jobs):
                symbol = jobs[future]
                try:
                    entry = future.result()
                    inventory["symbols"][entry["symbol"]] = entry
                    print(f"DATA {symbol}: {entry['rows']} bars, {entry['first']} .. {entry['last']}", flush=True)
                except Exception as exc:
                    inventory["failures"][symbol] = str(exc)
                    print(f"FAILED {symbol}: {exc}", flush=True)
                save(root / "download_manifest.json", inventory)
    else:
        inventory = json.loads((root / "download_manifest.json").read_text(encoding="utf-8"))
        if inventory["requested_start"] != args.start or inventory["requested_end_inclusive"] != args.end:
            raise ValueError("Requested range differs from downloaded manifest")
    base = [symbol.replace("/", "-") for symbol in DEFAULT_SYMBOLS]
    if any(symbol not in inventory["symbols"] for symbol in base):
        raise ValueError("Original basket incomplete; inspect failures before comparing")
    expanded = [symbol.replace("/", "-") for symbol in requested if symbol.replace("/", "-") in inventory["symbols"]]
    if len(expanded) <= len(base):
        raise ValueError("No additional usable symbols")
    comparisons = {}
    for name, symbols in (("original_30", base), ("expanded", expanded)):
        comparisons[name] = run_arm(name, symbols, root, inventory, args.capital, args.start, args.end)
        save(root / "comparison.json", comparisons)
    print(f"OUTPUT {root}", flush=True)


if __name__ == "__main__":
    main()
