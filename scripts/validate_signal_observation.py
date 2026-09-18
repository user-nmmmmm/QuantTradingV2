"""Offline P0 acceptance: same inputs/seed with observation off and on.

No network, fitting, threshold selection or strategy admission. Produces a
replayable evidence bundle and exits nonzero if the passive contract fails.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import random
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestEngine
from backtest.reporting.signal_observation import write_signal_observation_report
from core.data import DataHandler
from core.reproducibility import (
    artifact_hashes, build_run_manifest, canonical_json, data_identity,
    deterministic_result_digest, runtime_identity, save_data_snapshots, write_manifest,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT/"data/binance/1d")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT", "BNB/USDT"])
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2020-12-31")
    parser.add_argument("--output-dir", type=Path,
        default=ROOT/"reports"/datetime.now().strftime("p0_signal_observation_%Y%m%d_%H%M%S"))
    parser.add_argument("--seed", type=int, default=20260918)
    args = parser.parse_args(argv)
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end) + pd.Timedelta(days=1)
    if start >= end:
        parser.error("start must not follow end")
    data = {}
    for symbol in args.symbols:
        source = args.data_dir/(symbol.replace("/", "_")+".csv")
        frame = DataHandler.load_csv(str(source))
        frame = frame[(frame.index >= start) & (frame.index < end)]
        if len(frame) <= 60:
            parser.error(f"insufficient local history for {symbol}")
        data[symbol] = frame
    # Never overwrite an earlier immutable acceptance bundle.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    results = {}
    for enabled in (False, True):
        random.seed(args.seed)
        np.random.seed(args.seed)
        engine = BacktestEngine(initial_capital=10000, random_slip=False, timeframe="1d",
            run_id="signal-observation-p0-local", signal_observation={"enabled": enabled})
        results[enabled] = engine.run(data, routing_log_enabled=False)
        print(f"Observation {'on' if enabled else 'off'}: {len(results[enabled]['trades'])} fills", flush=True)
    off, on = results[False], results[True]
    digest_off, digest_on = deterministic_result_digest(off), deterministic_result_digest(on)
    checks = {"official_digests_identical": digest_off == digest_on,
              "strategy_health_identical": off["strategy_health"] == on["strategy_health"],
              "allocation_identical": off["allocation_audit"] == on["allocation_audit"]}
    summary = write_signal_observation_report(on["signal_observation"], args.output_dir)
    checks.update(observation_complete=summary["status"] == "complete",
                  candidate_partition_complete=summary["decision_partition_ok"],
                  candidate_ids_unique=summary["candidate_ids_unique"],
                  actual_fills_linked=not summary["unmatched_actual_fills"])
    snapshots = save_data_snapshots(data, args.output_dir/"data_inputs")
    acceptance = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
        "period": {"start": args.start, "end": args.end}, "symbols": sorted(data),
        "seed": args.seed, "official_off": digest_off, "official_on": digest_on,
        "candidates": summary["candidate_count"], "gates": summary["gates"],
        "outcomes": summary["outcome_statuses"], "ghosts": summary["ghost_statuses"],
        "strategy_coverage": summary["coverage"],
        "research_payload_sha256": summary["research_payload_sha256"],
        "scope": "engineering acceptance on existing history; not a new out-of-sample test or admission decision"}
    (args.output_dir/"acceptance.json").write_text(canonical_json(acceptance)+"\n", encoding="utf-8")
    on["equity_curve"].to_csv(args.output_dir/"official_equity.csv")
    identity = data_identity(data, source="local", exchange="binance", market_type=on["account_mode"],
                             timeframe="1d", timezone_name="UTC", downloaded_at=None)
    identity["cache_read_at"] = datetime.now(timezone.utc)
    execution = {**runtime_identity(), "capital": engine.initial_capital, "seed": args.seed,
        "data_symbol_order": list(data),
        "slippage": engine.slippage, "random_slip": False, "warmup_period": engine.warmup_period,
        "alignment_mode": engine.alignment_mode, "benchmark_mode": engine.benchmark_mode,
        "benchmark_rebalance_cost_bps": engine.benchmark_rebalance_cost_bps,
        "timeframe": "1d", "account_mode": on["account_mode"], "routing_log_enabled": False,
        "result_digest": digest_on, "signal_observation": summary["policy"],
        "signal_observation_digest": summary["research_payload_sha256"]}
    manifest = build_run_manifest(run_id=on["run_id"], repo_root=ROOT,
        config_path=ROOT/"config/params.yaml", requested_period=acceptance["period"],
        effective_period={s: {"start": f.index.min(), "end": f.index.max(), "rows": len(f)}
                          for s, f in data.items()},
        data=identity, snapshots=snapshots, execution=execution,
        artifacts=artifact_hashes(args.output_dir, [*summary["artifacts"], "acceptance.json", "official_equity.csv"]),
        audit={"signal_observation_acceptance": checks})
    write_manifest(args.output_dir/"run_manifest.json", manifest)
    print(canonical_json(acceptance))
    print(f"Evidence: {args.output_dir.resolve()}")
    return 0 if acceptance["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
