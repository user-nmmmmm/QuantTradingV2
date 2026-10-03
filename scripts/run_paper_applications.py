"""Run a preregistered, offline paper application study on verified BTC/ETH bars."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from analysis.paper_data_audit import audit_frame
from analysis.paper_labels import BarrierConfig, LabelCosts, label_candidates
from analysis.paper_study import (
    ARMS, WINDOWS, arm_parameters, chronological_label_baseline, coverage_report,
    equity_returns, factor_attribution, factor_proxies, fixed_benchmark,
    freeze_registration, load_verified_inputs, write_json,
)
from analysis.paper_validation import (
    assemble_cpcv_paths, cpcv_splits, cscv_pbo, deflated_sharpe_evidence,
    white_reality_check,
)
from analysis.research_validation import ExperimentRegistry, evaluate_holdout_admission
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from config.config import config
from core.metrics.execution import calculate_execution_quality
from core.reproducibility import deterministic_result_digest, sha256_file, sha256_frame
from scripts.run_revalidation60 import source_hashes


def run_job(frames, parameters, *, start, end, name, folder, observe=False):
    if folder.exists():
        raise ValueError(f"Job output already exists: {folder}; choose a fresh output")
    folder.mkdir(parents=True)
    timeline = frames["BTC/USDT"].loc[start:end].index
    sliced = {s: f.loc[:end].tail(len(timeline) + 240).copy() for s, f in frames.items()}
    prior = config._config
    config._config = deepcopy(parameters)
    try:
        engine = BacktestEngine(initial_capital=10000, warmup_period=180, timeframe="1d",
            trading_start=pd.Timestamp(start), run_id=name, alignment_mode="intersection",
            benchmark_mode="fixed", terminal_policy="forced_liquidation",
            signal_observation={"enabled": observe}, signal_meta_layer={"enabled": observe},
            signal_adaptive={"enabled": observe}, signal_meta_replay={"enabled": observe})
        result = engine.run(sliced, routing_log_enabled=False)
        if not result["accounting_check"]["ok"]:
            raise ValueError("Per-bar accounting identity failed")
        lifecycle = result["lifecycle"]
        if lifecycle.get("unresolved_risk_positions") or lifecycle.get("final_open_positions", 0):
            raise ValueError("Unresolved book cannot be extended as a cash tail")
        curve, returns = equity_returns(result["equity_curve"], timeline)
        benchmark = fixed_benchmark(frames, start, end)
        reporter = ReportGenerator(str(folder))
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(result["trades"]))
        closed = reporter._aggregate_round_trips(legs)
        close_facts = [asdict(event) for event in engine.execution_adapter.broker.close_events]
        # Transaction costs are in close facts; financing has a separate ledger.
        financing_net = sum(float(e.get("realized_pnl", 0)) for e in close_facts)
        initial = pd.Series([10000.], index=[timeline[0] - pd.Timedelta(days=1)])
        gate = evaluate_holdout_admission(trades=closed,
            equity=pd.concat([initial, curve]), benchmark=pd.concat([initial, benchmark]))
        gate["existing_formula_diagnostic_decision"] = gate["decision"]
        gate["decision"] = "retrospective_diagnostic_only"
        gate["evidence_scope"] = "retrospective diagnostic; this is NOT an unopened holdout"
        gate["independent_holdout_passed"] = False
        pnls = [float(row["net_pnl"]) for row in closed if row.get("net_pnl") is not None]
        summary = {"name": name, "start": start, "end": end,
            "return_pct": 100 * (curve.iloc[-1] / 10000 - 1),
            "benchmark_return_pct": 100 * (benchmark.iloc[-1] / 10000 - 1),
            "max_drawdown_pct": 100 * (1 - curve / curve.cummax().clip(lower=10000)).max(),
            "closed_trades": len(closed), "fills": len(result["trades"]),
            "profit_factor": (sum(p for p in pnls if p > 0) / -sum(p for p in pnls if p < 0))
                if any(p < 0 for p in pnls) else None,
            "net_closed_pnl_before_separate_financing": sum(pnls),
            "closed_fact_realized_pnl": financing_net, "accounting_ok": True,
            "lifecycle": lifecycle, "diagnostic_gates": gate["gates"],
            "retrospective_gate_decision": gate["existing_formula_diagnostic_decision"], "live_admission": False}
        pd.DataFrame({"equity": curve, "returns": returns, "benchmark": benchmark}).to_csv(
            folder / "daily.csv", index_label="timestamp")
        for key in ("trades", "execution_audit", "financing_ledger", "entry_observations",
                    "exit_lifecycle_audit", "allocation_audit"):
            pd.DataFrame(result.get(key, [])).to_csv(folder / f"{key}.csv", index=False)
        pd.DataFrame(closed).to_csv(folder / "closed_trades.csv", index=False)
        for key in ("accounting_check", "lifecycle", "account_cost_contract"):
            write_json(folder / f"{key}.json", result[key])
        write_json(folder / "close_facts.json", close_facts)
        write_json(folder / "resolved_config.json", parameters)
        write_json(folder / "diagnostic_admission.json", gate)
        write_json(folder / "digest.json", deterministic_result_digest(result))
        quality = calculate_execution_quality(result.get("event_log"))
        quality.update(evidence_source="local_simulated_events", real_venue_calibration=False)
        write_json(folder / "execution_quality.json", quality)
        write_json(folder / "summary.json", summary)
        return result, summary, returns, benchmark
    finally:
        config._config = prior


def label_experiment(frames, p0, output, protocol):
    costs = LabelCosts(**protocol["costs"])
    config_label = BarrierConfig(**protocol["barriers"])
    labels = label_candidates(frames, p0["candidates"], config=config_label,
                              costs=costs, as_of=protocol["as_of"])
    rows = labels["outcomes"]
    write_json(output / "triple_barrier.json", labels)
    pd.DataFrame(rows).to_csv(output / "triple_barrier.csv", index=False)
    fixed = {(r["candidate_id"], r["horizon_bars"]): r for r in p0["outcomes"]}
    paired = []
    for row in rows:
        reference = fixed.get((row["candidate_id"], config_label.max_holding_bars), {})
        if row["status"] == "matured" and reference.get("status") == "matured":
            paired.append({"candidate_id": row["candidate_id"], "ambiguous": row["ambiguous"],
                "barrier": row["barrier"], "fixed_net_bps": reference["net_return_bps"],
                "barrier_net_bps": row["net_return_bps"],
                "sign_agrees": np.sign(reference["net_return_bps"]) == np.sign(row["net_return_bps"])})
    pd.DataFrame(paired).to_csv(output / "label_comparison.csv", index=False)
    rolling = chronological_label_baseline(rows)
    write_json(output / "label_chronological_baseline.json", rolling)
    # Same-time asset observations become one time cohort; do not split them
    # across test groups or count same-day assets as independent time samples.
    events = pd.DataFrame([r for r in rows if r["training_eligible"]])
    if events.empty:
        cpcv = {"status": "insufficient", "reason": "no trainable labels"}
    else:
        events["purge_end"] = [max(pd.Timestamp(r["label_end_time"]), pd.Timestamp(r["available_at"]))
                               for r in events.to_dict("records")]
        cohorts = events.groupby("entry_time", sort=True).agg(
            net_return_bps=("net_return_bps", "mean"),
            label_end_time=("purge_end", "max"), members=("candidate_id", "count"))
        plan = cpcv_splits(pd.to_datetime(cohorts.index), pd.to_datetime(cohorts.label_end_time),
                           n_groups=6, n_test_groups=2, embargo_fraction=.01)
        predictions, fits = {}, []
        if plan["status"] == "ok":
            for fold in plan["splits"]:
                train = cohorts.iloc[fold["train"]]
                estimate = float(train.net_return_bps.mean())
                predictions[fold["split_id"]] = pd.Series(estimate, index=fold["test"], dtype=float)
                fits.append({"split_id": fold["split_id"], "training_cohorts": len(train),
                             "mean_net_bps": estimate})
        paths = assemble_cpcv_paths(plan, predictions)
        errors = [abs(o["prediction"] - cohorts.net_return_bps.iloc[o["position"]])
                  for path in paths["paths"] for o in path["observations"]]
        cpcv = {"status": paths["status"], "plan": plan, "fits": fits, "paths": paths,
            "mae_bps": float(np.mean(errors)) if errors else None,
            "unique_time_cohorts": len(cohorts), "not_independent_path_samples": True,
            "interpretation": "split-fitted constant mean net-label predictions; combinatorial generalization, not forward portfolio performance"}
    write_json(output / "label_cpcv.json", cpcv)
    return {"summary": labels["summary"], "paired_matured": len(paired),
            "sign_agreement": float(np.mean([r["sign_agrees"] for r in paired])) if paired else None,
            "chronological_baseline": {k: v for k, v in rolling.items() if k != "predictions"},
            "cpcv_status": cpcv["status"],
            "cost_comparison": "fixed horizon uses P0 bar cost model; barrier uses preregistered fixed scenario, not an economic uplift estimate"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/binance/1d/_manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/paper_applications_20261003_v3")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if (args.output / "results.json").exists():
        raise ValueError("Completed study is immutable; select a new output directory")
    frames, data_identity = load_verified_inputs(args.manifest)
    base = deepcopy(config._config)
    label_protocol = {"barriers": {"timeframe": "1d", "profit_take_bps": 200.0,
        "stop_loss_bps": 100.0, "max_holding_bars": 5},
        "costs": {"commission_bps_per_side": 10.0, "slippage_bps_per_side": 5.0,
                  "spread_bps_per_side": 1.0, "impact_bps_per_side": 5.0, "carry_bps": 0.0},
        "as_of": "2026-09-20T00:00:00Z", "unmodeled": "historical borrow/funding and variable impact; diagnostic labels only"}
    jobs = [{"name": f"{arm}_{window}_cost{multiplier:g}", "arm": arm, "window": window,
             "start": start, "end": end, "cost_multiplier": multiplier,
             "parameters": arm_parameters(base, arm, multiplier=multiplier)}
            for window, start, end in WINDOWS for arm in ARMS for multiplier in (1.0, 1.5)]
    identity = {"schema": "paper-applications-study/v1", "source_hashes": source_hashes(),
        "config_sha256": sha256_file(ROOT / "config/params.yaml"), "data": data_identity,
        "frame_hashes": {s: sha256_frame(f) for s, f in frames.items()}, "jobs": jobs,
        "candidate_family": list(ARMS), "diagnostic_variants_count": len(jobs) + 4,
        "maximum_runs": len(jobs) + 1, "label_protocol": label_protocol,
        "observation": {"P0_P1_P2_P3": "enabled only in independent full-history diagnostic run",
                         "P1_P2_support_thresholds": "unchanged from authoritative config"},
        "statistics": {"pbo_groups": 8, "bootstrap_iterations": 2000,
                       "block_lengths": [5, 20, 60], "seed": 42},
        "hypotheses": {"regime": "compare rule state with unfiltered trend and retrospective EV coverage",
            "factors": "momentum and OBV ablation plus two-asset market/momentum exposure",
            "labels": "bar triple barriers change mature labels under explicit ambiguity and causal cutoffs"},
        "benchmark": "fixed BTC/ETH 50/50 first-open buy-and-hold, before benchmark costs; strategy costs charged",
        "health_reference": "All trend arms use an explicit common health-disabled research policy; production requires three distinct symbols, unavailable in this two-asset subset. Range retains its own cooldown. No threshold is reduced.",
        "interpretation": "all data already seen historically; retrospective engineering study, no independent holdout",
        "historical_global_search_complete": False, "full_universe_pit_verified": False,
        "implementation_repairs": "v1 rejected unreachable two-asset health registration; v2 retained partial runs, superseded by equity-gap/initial-capital/label-query repairs. Parameters were not selected from these outcomes.",
        "production_config_changes": False, "real_venue_orders": False}
    args.output.mkdir(parents=True, exist_ok=True)
    registration_hash = freeze_registration(args.output / "registration.json", identity)
    registry = ExperimentRegistry(args.output / "experiments.jsonl")
    for group, hypothesis in identity["hypotheses"].items():
        registry.register(hypothesis=hypothesis, parameters={"group": group,
            "registration_hash": registration_hash}, data_scope=data_identity, metrics={})
    audit = {s: audit_frame(f, identity={"symbol": s, "venue": "binance", "market_type": "spot"},
        timeframe="1d", as_of=label_protocol["as_of"],
        units={"timestamp": "iso8601", "price": "quote_per_base", "volume": "base"},
        expected_start="2020-01-01", expected_end="2026-09-20") for s, f in frames.items()}
    write_json(args.output / "data_audit.json", audit)
    if any(report["errors"] for report in audit.values()):
        raise ValueError("Data audit failed; no runs permitted")
    summaries, daily, benchmarks = [], {}, {}
    for job in jobs:
        print(f"RUN {job['name']}", flush=True)
        try:
            _, summary, returns, benchmark = run_job(frames, job["parameters"],
                start=job["start"], end=job["end"], name=job["name"],
                folder=args.output / "runs" / job["name"])
        except Exception as exc:
            write_json(args.output / "failed_run.json", {"job": job["name"],
                "type": type(exc).__name__, "message": str(exc), "family_complete": False})
            raise
        summary.update(arm=job["arm"], window=job["window"], cost_multiplier=job["cost_multiplier"])
        summaries.append(summary)
        if job["cost_multiplier"] == 1:
            daily.setdefault(job["arm"], []).append(returns)
            benchmarks[job["window"]] = benchmark
        print(f"DONE {job['name']} return={summary['return_pct']:.4f}% trades={summary['closed_trades']}", flush=True)
    panel = pd.DataFrame({arm: pd.concat(daily[arm]) for arm in ARMS})
    if panel.isna().any().any() or panel.index.has_duplicates:
        raise ValueError("Incomplete common candidate panel")
    panel.to_csv(args.output / "candidate_returns.csv", index_label="timestamp")
    benchmark_parts = []
    for window, start, end in WINDOWS:
        b = benchmarks[window]
        r = b.pct_change()
        r.iloc[0] = b.iloc[0] / 10000 - 1
        benchmark_parts.append(r)
    benchmark_returns = pd.concat(benchmark_parts)
    # Exact equal groups are required; the declared CSCV prefix is logged.
    equal_length = len(panel) - len(panel) % 8
    pbo = cscv_pbo(panel.iloc[:equal_length], n_groups=8, family_complete=True)
    pbo["declared_prefix"] = {"rows": equal_length, "excluded_tail_rows": len(panel) - equal_length}
    statistics = {"pbo": pbo, "reality_check": [white_reality_check(panel, benchmark_returns,
        family_complete=True, iterations=2000, block_length=block, seed=42) for block in (5, 20, 60)],
        "dsr": {arm: deflated_sharpe_evidence(panel, arm, historical_trials_complete=False,
             registered_before_results=True, declared_total_trials=None) for arm in ARMS},
        "scope": "complete cost=1 local family of seven fixed three-window restart procedures; cost=1.5 runs are preregistered stress diagnostics. Previous global searches and development reruns remain incomplete; not an admission test",
        "window_boundaries": "account resets on each declared window; portfolio-return panel is a fixed restart procedure"}
    write_json(args.output / "statistics.json", statistics)
    factors = factor_proxies(frames)
    factors.to_csv(args.output / "factor_proxies.csv", index_label="timestamp")
    attribution = {arm: {str(block): factor_attribution(panel[arm], factors, block_length=block)
                       for block in (5, 20, 60)} for arm in ARMS}
    write_json(args.output / "factor_attribution.json", attribution)
    # Same algorithm/costs, longer observed history: never reclassify as forward.
    print("RUN full-history P0/P1/P2/P3 coverage diagnostic", flush=True)
    observation, observation_summary, _, _ = run_job(frames, arm_parameters(base, "baseline"),
        start="2020-07-01", end="2026-09-19", name="coverage_full_history",
        folder=args.output / "runs/coverage_full_history", observe=True)
    p0 = observation["signal_observation"]
    if p0 is None or p0.get("errors"):
        raise ValueError("Raw candidate observation failed")
    for name in ("signal_observation", "signal_meta_layer", "signal_adaptive", "signal_meta_replay"):
        write_json(args.output / f"{name}.json", observation[name])
    coverage = {"p0": {"status": p0["status"], "candidates": len(p0["candidates"]),
                       "outcome_statuses": dict(Counter(r["status"] for r in p0["outcomes"]))},
                "p1": coverage_report(observation["signal_meta_layer"]),
                "p2": coverage_report(observation["signal_adaptive"]),
                "p3": {k: v for k, v in (observation["signal_meta_replay"] or {}).items()
                       if k not in ("orders", "trades", "equity_curves", "decisions", "daily")}}
    labels = label_experiment(frames, p0, args.output, label_protocol)
    if source_hashes() != identity["source_hashes"] or sha256_file(ROOT / "config/params.yaml") != identity["config_sha256"]:
        raise ValueError("Source/config changed during study; comparison is not frozen")
    stress_lookup = {(s["arm"], s["window"]): s for s in summaries if s["cost_multiplier"] == 1.5}
    decisions = {}
    for arm in ARMS:
        standard = [s for s in summaries if s["arm"] == arm and s["cost_multiplier"] == 1]
        gates = {"positive_net_each_window": all(s["return_pct"] > 0 for s in standard),
                 "positive_excess_each_window": all(s["return_pct"] > s["benchmark_return_pct"] for s in standard),
                 "cost_stress_positive_each_window": all(stress_lookup[arm, s["window"]]["return_pct"] > 0 for s in standard),
                 "existing_diagnostic_gates_each_window": all(all(s["diagnostic_gates"].values()) for s in standard),
                 "historical_membership_verified": False, "independent_holdout_passed": False,
                 "real_execution_calibrated": False}
        decisions[arm] = {"decision": "research_only_no_forward_promotion", "gates": gates,
                          "failed_gates": [g for g, passed in gates.items() if not passed]}
    results = {"registration_sha256": registration_hash, "completed_runs": len(summaries) + 1,
               "common_panel_rows": len(panel), "summaries": summaries,
               "coverage": coverage, "labels": labels, "candidate_decisions": decisions,
               "promoted_candidates": [], "accounting_all_passed": True,
               "real_venue_calibration": {"status": "insufficient", "real_orders": 0},
               "future_observation": {"status": "not_started_no_qualified_new_candidate",
                   "existing_successor_protocol_preserved": True,
                   "existing_successor_earliest_maturity": "2027-05-09"}}
    write_json(args.output / "results.json", results)
    pd.DataFrame(summaries).drop(columns=["lifecycle", "diagnostic_gates"]).to_csv(
        args.output / "comparison.csv", index=False)
    print(json.dumps({"completed_runs": results["completed_runs"], "panel_rows": len(panel),
                      "p1": coverage["p1"]["statuses"], "p2": coverage["p2"]["statuses"],
                      "promoted_candidates": []}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
