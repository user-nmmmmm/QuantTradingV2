"""Offline paired P0 regression on hash-verified frozen historical inputs.

Run from the repository root; never connects to an exchange. This is a
same-sample engineering experiment, not independent strategy validation.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from config.config import config
from core.reproducibility import (
    canonical_json, code_identity, deterministic_result_digest,
    load_data_snapshots, load_manifest, runtime_identity, sha256_file,
)


def save(path, value):
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False,
                               indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, default=ROOT / "reports" /
                        "20260902_211518_3239d_30Syms_Ret137.0pct" / "run_manifest.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-reference", type=Path,
                        help="Require both result digests to match an earlier paired run")
    parser.add_argument("--isolate-portfolio-breaker", action="store_true",
                        help="Diagnostic only: disable strategy health in BOTH arms, never change params.yaml")
    args = parser.parse_args()
    source = args.source_manifest.resolve()
    manifest = load_manifest(source)
    frames = load_data_snapshots(source.parent / "data_inputs", manifest["data_snapshots"], verify=True)
    execution = manifest["execution"]
    output = args.output or ROOT / "outputs" / "p0_recovery" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = copy.deepcopy(config._config)
    experiment_config = copy.deepcopy(original)
    if args.isolate_portfolio_breaker:
        experiment_config["strategy_health"]["enabled"] = False
    logging.disable(logging.CRITICAL)
    source_hashes = {
        str(path.relative_to(ROOT)): sha256_file(path)
        for directory in ("backtest", "composition", "core", "config", "strategies", "router", "live_trading")
        for path in (ROOT / directory).rglob("*.py")
    }
    source_hashes["scripts/run_p0_recovery_backtest.py"] = sha256_file(__file__)
    identity = {"source_manifest": str(source), "source_manifest_sha256": sha256_file(source),
                "data_snapshots": manifest["data_snapshots"], "code": code_identity(ROOT),
                "source_hashes": source_hashes, "runtime": runtime_identity(),
                "source_execution": execution,
                "isolate_portfolio_breaker": args.isolate_portfolio_breaker,
                "scope": "paired engineering regression; no holdout; only recovery.enabled differs between arms"}
    save(output / "experiment.json", identity)
    summary = {}
    try:
        for name, enabled in (("baseline", False), ("recovery", True)):
            config._config = copy.deepcopy(experiment_config)
            config._config["drawdown"]["recovery"]["enabled"] = enabled
            folder = output / name
            folder.mkdir()
            save(folder / "resolved_config.json", config._config)
            seed = int(execution["seed"])
            np.random.seed(seed)
            random.seed(seed)
            print(f"START {name}: {len(frames)} symbols, seed={seed}", flush=True)
            engine = BacktestEngine(
                initial_capital=float(execution["capital"]), slippage=execution.get("slippage"),
                random_slip=bool(execution.get("random_slip", False)),
                warmup_period=int(execution.get("warmup_period", 30)),
                alignment_mode=execution["alignment_mode"], benchmark_mode=execution["benchmark_mode"],
                benchmark_rebalance_cost_bps=float(execution["benchmark_rebalance_cost_bps"]),
                timeframe=execution["timeframe"], account_mode=execution.get("account_mode"),
                run_id="p0-paired-regression",
            )
            result = engine.run({symbol: frame.copy(deep=True) for symbol, frame in frames.items()},
                                routing_log_enabled=False)
            reporter = ReportGenerator(str(folder))
            metrics = reporter.generate(
                result["trades"], result["equity_curve"], benchmark_curve=result["benchmark"],
                metrics_only=True, close_events=result["close_events"],
                lifecycle=result["lifecycle"], strategy_health=result["strategy_health"],
            )
            result["equity_curve"].to_csv(folder / "equity.csv")
            for key in ("trades", "breaker_audit", "strategy_health_transitions",
                        "strategy_health_cohorts", "stop_order_audit",
                        "risk_budget_reconciliation", "execution_audit", "financing_ledger"):
                pd.DataFrame(result[key]).to_csv(folder / f"{key}.csv", index=False)
            for key in ("breaker_state", "strategy_health", "accounting_check", "lifecycle", "account_cost_contract"):
                save(folder / f"{key}.json", result[key])
            save(folder / "metrics.json", metrics)
            digest = deterministic_result_digest(result)
            save(folder / "result_digest.json", digest)
            if args.verify_reference:
                expected = json.loads((args.verify_reference / name / "result_digest.json").read_text(encoding="utf-8"))
                passed = digest == expected
                save(folder / "replay_verification.json", {"passed": passed, "reference": str(args.verify_reference),
                                                           "expected": expected, "observed": digest})
                if not passed:
                    raise AssertionError(f"{name} deterministic replay mismatch")
            if not result["accounting_check"]["ok"]:
                raise AssertionError(f"{name} accounting reconciliation failed")
            curve = result["equity_curve"]["equity"]
            trades = pd.DataFrame(result["trades"])
            summary[name] = {
                "start": curve.index.min(), "end": curve.index.max(), "bars": len(curve),
                "final_equity": float(curve.iloc[-1]),
                "return_pct": (float(curve.iloc[-1]) / float(execution["capital"]) - 1) * 100,
                "max_drawdown_pct": float((1 - curve / curve.cummax()).max()) * 100,
                "fill_count": len(trades), "metrics": metrics,
                "breaker_state": result["breaker_state"], "accounting_check": result["accounting_check"],
            }
            save(output / "comparison.json", summary)
            print(f"DONE {name}: equity={curve.iloc[-1]:.2f}, fills={len(trades)}, "
                  f"breaker={result['breaker_state']['action']}", flush=True)
    finally:
        config._config = original
        logging.disable(logging.NOTSET)
    print(f"OUTPUT {output}", flush=True)


if __name__ == "__main__":
    main()
