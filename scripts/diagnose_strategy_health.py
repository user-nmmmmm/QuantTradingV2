"""Observe the frozen current-config backtest without changing trading decisions."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from config.config import config
from core.reproducibility import canonical_json, deterministic_result_digest, load_data_snapshots, load_manifest, sha256_file
from core.strategy_health import StrategyHealthMachine


def save(path, value):
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=ROOT / "outputs/p0_recovery/final_current_config/recovery")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reference = args.reference.resolve()
    experiment = json.loads((reference.parent / "experiment.json").read_text(encoding="utf-8"))
    source = Path(experiment["source_manifest"])
    manifest = load_manifest(source)
    frames = load_data_snapshots(source.parent / "data_inputs", manifest["data_snapshots"], verify=True)
    execution = manifest["execution"]
    output = args.output or ROOT / "outputs/health_diagnosis" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    saved_config = copy.deepcopy(config._config)
    previous_logging = logging.root.manager.disable
    transitions, closes = [], []
    original_transition = StrategyHealthMachine._transition
    original_close = StrategyHealthMachine.ingest_close
    original_failure = StrategyHealthMachine._fail_probation
    pending_failures = {}

    def probation_snapshot(machine):
        return {"probation_started_at": machine.probation_started_at,
                "probation_cohorts": [row.to_dict() for row in machine._probation_cohorts()],
                "probation_total_r": machine.probation_total_r}

    def observe_failure(machine, moment, *, reason):
        # _enter_cooldown clears probation BEFORE calling _transition.
        pending_failures[id(machine)] = probation_snapshot(machine)
        try:
            return original_failure(machine, moment, reason=reason)
        finally:
            pending_failures.pop(id(machine), None)

    def observe_transition(machine, target, moment, *, reason):
        # Capture BEFORE transition resets probation state, never evaluate twice.
        transitions.append({
            "strategy": machine.strategy_name, "at": moment,
            "from": machine.status.value, "to": target.value, "reason": reason,
            **pending_failures.get(id(machine), probation_snapshot(machine)),
            "failed_cycles_at_transition": machine.failed_probation_cycles,
            "counted_tail": [row.to_dict() for row in machine.counted_cohorts()[-5:]],
        })
        return original_transition(machine, target, moment, reason=reason)

    def observe_close(machine, **kwargs):
        cohort = original_close(machine, **kwargs)
        if cohort is not None:
            closes.append({"strategy": machine.strategy_name, **kwargs,
                           "cohort_id": cohort.cohort_id, "controller": cohort.exit_controller,
                           "counts_toward_health": cohort.counts_toward_health})
        return cohort

    try:
        config._config = json.loads((reference / "resolved_config.json").read_text(encoding="utf-8"))
        logging.disable(logging.CRITICAL)
        random.seed(execution["seed"])
        np.random.seed(execution["seed"])
        print("START observational replay; no policy overrides", flush=True)
        with (patch.object(StrategyHealthMachine, "_transition", observe_transition),
              patch.object(StrategyHealthMachine, "ingest_close", observe_close),
              patch.object(StrategyHealthMachine, "_fail_probation", observe_failure)):
            result = BacktestEngine(
                initial_capital=execution["capital"], slippage=execution["slippage"],
                random_slip=execution["random_slip"], warmup_period=execution["warmup_period"],
                alignment_mode=execution["alignment_mode"], benchmark_mode=execution["benchmark_mode"],
                benchmark_rebalance_cost_bps=execution["benchmark_rebalance_cost_bps"],
                timeframe=execution["timeframe"], account_mode=execution["account_mode"],
                run_id="p0-paired-regression",
            ).run(frames, routing_log_enabled=False)
        observed = deterministic_result_digest(result)
        expected = json.loads((reference / "result_digest.json").read_text(encoding="utf-8"))
        verification = {"passed": observed == expected, "reference": str(reference),
                        "expected": expected, "observed": observed, "script_sha256": sha256_file(__file__)}
        save(output / "verification.json", verification)
        if observed != expected:
            raise AssertionError("Observation changed replay output")
        save(output / "transition_evidence.json", transitions)
        save(output / "close_events.json", closes)
        pd.DataFrame(closes).to_csv(output / "close_events.csv", index=False)
        pd.DataFrame(result["strategy_health_cohorts"]).to_csv(output / "cohorts.csv", index=False)
        reporter = ReportGenerator(str(output))
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(result["trades"]))
        save(output / "closed_legs.json", legs)
        verdicts = [row for row in transitions if row["from"] == "probation"]
        for row in verdicts:
            cohorts = row["probation_cohorts"]
            print(f"{row['at']} -> {row['to']}: {len(cohorts)} cohorts; R={row['probation_total_r']:.6f}; "
                  f"PnL={sum(c['net_pnl'] for c in cohorts):.6f}", flush=True)
        print(f"VERIFIED {output.resolve()}", flush=True)
    finally:
        config._config = saved_config
        logging.disable(previous_logging)


if __name__ == "__main__":
    main()
