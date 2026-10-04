"""Frozen retrospective Engine comparison; no network, live policy or orders."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
import logging
import math
from pathlib import Path
import platform
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from analysis.paper_study import WINDOWS, arm_parameters, freeze_registration, load_verified_inputs, write_json
from core.reproducibility import sha256_file, sha256_frame
from scripts.run_paper_applications import run_job
from strategies.trend_portfolio_v2 import TrendPortfolioV2Strategy


ARMS = ("baseline", "regime_all", "prior_momentum_no_obv", "no_obv_hybrid", "no_obv_wide_trail",
        "no_obv_medium_trend", "no_obv_equal_ensemble")
CONTROLS = {"baseline": "baseline", "regime_all": "regime_all", "prior_momentum_no_obv": "momentum_no_obv"}
STUDY_WINDOWS = (*WINDOWS, ("continuous", "2021-01-01", "2026-09-19"))
OBSERVERS = ("signal_observation", "signal_meta_layer", "signal_adaptive", "signal_meta_replay")


@contextmanager
def _quiet_logging():
    """Silence study output without changing the caller's later diagnostics."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def source_identity():
    """Freeze engine dependencies without unrelated concurrent research scripts."""
    paths = []
    for directory in ("core", "backtest", "strategies", "router", "composition", "config"):
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.suffix in {".py", ".yaml", ".json"}
                     and "__pycache__" not in p.parts)
    paths.extend(ROOT / name for name in (
        "analysis/__init__.py", "analysis/paper_study.py", "analysis/paper_data_audit.py",
        "analysis/paper_labels.py", "analysis/paper_validation.py", "analysis/research_validation.py",
        "scripts/run_paper_applications.py", "scripts/run_revalidation60.py",
        "scripts/run_return_followup_engine.py", "pyproject.toml", "requirements.lock.txt") if (ROOT / name).is_file())
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(set(paths))}


def followup_parameters(base, arm, multiplier):
    if arm not in ARMS:
        raise ValueError("unregistered candidate")
    original = CONTROLS.get(arm, "momentum_no_obv")
    parameters = arm_parameters(base, original, multiplier=multiplier)
    options = parameters.get("research", {}).get("trend_portfolio_v2")
    if arm == "no_obv_hybrid":
        options["exit_mode"] = "hybrid"
    elif arm == "no_obv_wide_trail":
        options["trailing_atr_multiple"] = 3.5
    elif arm == "no_obv_medium_trend":
        options.update(horizons=[60, 120], weights=[.5, .5])
    elif arm == "no_obv_equal_ensemble":
        options.update(horizons=[20, 60, 120], weights=[1 / 3, 1 / 3, 1 / 3])
    if options is not None:
        default = inspect.signature(TrendPortfolioV2Strategy).parameters["target_annual_volatility"].default
        if options.get("target_annual_volatility", default) != .10:
            raise ValueError("entry volatility target must remain 10 percent")
    if any(parameters[name]["enabled"] for name in OBSERVERS):
        raise ValueError("research observers must be disabled")
    return parameters


def build_jobs(prior_registration):
    prior_jobs = prior_registration["jobs"]
    base = deepcopy(next(j["parameters"] for j in prior_jobs
                         if j["arm"] == "baseline" and j["cost_multiplier"] == 1.))
    jobs = []
    for window, start, end in STUDY_WINDOWS:
        for multiplier in (1., 1.5):
            for arm in ARMS:
                parameters = followup_parameters(base, arm, multiplier)
                if arm in CONTROLS and window != "continuous":
                    previous = next(j for j in prior_jobs if j["arm"] == CONTROLS[arm]
                        and j["window"] == window and j["cost_multiplier"] == multiplier)
                    if parameters != previous["parameters"]:
                        raise ValueError("old control parameters changed: " + previous["name"])
                jobs.append({"name": f"{arm}_{window}_cost{multiplier:g}", "arm": arm,
                    "window": window, "start": start, "end": end,
                    "cost_multiplier": multiplier, "parameters": parameters})
    return jobs


def extra_metrics(result, timeline):
    """Cash charges once; price-embedded slippage is displayed separately."""
    trades = result.get("trades", [])
    commission = math.fsum(float(row["commission"]) for row in trades)
    financing = math.fsum(float(row["amount"]) for row in result.get("financing_ledger", []))
    embedded = math.fsum(abs(float(row.get("slip", 0.))) * float(row["qty"]) for row in trades)
    curve = result["equity_curve"]
    exposure = curve.groupby(curve.index.normalize()).last().reindex(timeline)
    # run_job has already rejected any unresolved terminal inventory.
    gross = exposure["gross_exposure_pct_equity"].fillna(0.) if "gross_exposure_pct_equity" in exposure else None
    net = exposure["net_exposure_pct_equity"].fillna(0.) if "net_exposure_pct_equity" in exposure else None
    final = float(curve["equity"].iloc[-1])
    return {"initial_capital": 10000., "final_equity": final, "net_pnl_quote": final - 10000.,
        "commission_cash_quote": commission, "net_financing_cash_quote": financing,
        "net_cash_cost_quote": commission + financing,
        "price_embedded_slippage_quote": embedded,
        "average_gross_exposure_pct": float(100 * gross.mean()) if gross is not None else None,
        "maximum_gross_exposure_pct": float(100 * gross.max()) if gross is not None else None,
        "average_net_exposure_pct": float(100 * net.mean()) if net is not None else None,
        "fraction_days_in_market": float((gross > 0).mean()) if gross is not None else None,
        "exposure_basis": "last equity/exposure observation per UTC day; confirmed flat terminal tail is zero",
        "cash_cost_basis": "commission plus signed financing ledger amount; embedded slippage is not charged twice"}


@_quiet_logging()
def _worker(task):
    registration_path, job_name, registered_hash = task
    registration_path = Path(registration_path)
    protocol = json.loads(registration_path.read_text(encoding="utf-8"))
    if freeze_registration(registration_path, protocol) != registered_hash:
        raise ValueError("registration identity changed")
    job = next(j for j in protocol["jobs"] if j["name"] == job_name)
    output, started = registration_path.parent, time.monotonic()
    try:
        if source_identity() != protocol["source_hashes"]:
            raise ValueError("source identity changed before run")
        frames, data_identity = load_verified_inputs(Path(protocol["manifest"]))
        if data_identity != protocol["data"] or {s: sha256_frame(f) for s, f in frames.items()} != protocol["frame_hashes"]:
            raise ValueError("data identity changed before run")
        result, summary, _, _ = run_job(frames, job["parameters"], start=job["start"], end=job["end"],
            name=job["name"], folder=output / "runs" / job["name"], observe=False)
        if source_identity() != protocol["source_hashes"]:
            raise ValueError("source identity changed during run")
        for name in OBSERVERS:
            observed = result.get(name)
            if observed and isinstance(observed, dict) and observed.get("enabled") is True:
                raise ValueError("observer unexpectedly enabled")
        metrics = extra_metrics(result, frames["BTC/USDT"].loc[job["start"]:job["end"]].index)
        summary.update(metrics, arm=job["arm"], window=job["window"], cost_multiplier=job["cost_multiplier"],
                       status="completed", elapsed_seconds=time.monotonic() - started,
                       source_identity_unchanged=True, registration_sha256=registered_hash)
        result["equity_curve"].to_csv(output / "runs" / job["name"] / "equity_with_exposure.csv", index_label="timestamp")
        write_json(output / "runs" / job["name"] / "followup_summary.json", summary)
        return summary
    except Exception as exc:
        failure = {"name": job["name"], "arm": job["arm"], "window": job["window"],
            "cost_multiplier": job["cost_multiplier"], "status": "failed", "error_category": type(exc).__name__,
            "message": str(exc), "elapsed_seconds": time.monotonic() - started, "live_admission": False}
        write_json(output / "failures" / (job["name"] + ".json"), failure)
        return failure


def _legacy_costs(folder):
    trades = pd.read_csv(folder / "trades.csv")
    try:
        financing = pd.read_csv(folder / "financing_ledger.csv")
    except pd.errors.EmptyDataError:
        financing = pd.DataFrame()
    commission = float(trades["commission"].sum()) if len(trades) else 0.
    finance = float(financing["amount"].sum()) if len(financing) else 0.
    return {"commission_cash_quote": commission, "net_financing_cash_quote": finance,
            "net_cash_cost_quote": commission + finance}


def compare_prior(summaries, prior):
    metrics = ("return_pct", "max_drawdown_pct", "closed_trades", "fills", "profit_factor",
               "net_closed_pnl_before_separate_financing", "closed_fact_realized_pnl",
               "commission_cash_quote", "net_financing_cash_quote", "net_cash_cost_quote")
    result = []
    for current in summaries:
        if current["status"] != "completed" or current["arm"] not in CONTROLS or current["window"] == "continuous":
            continue
        previous_name = f"{CONTROLS[current['arm']]}_{current['window']}_cost{current['cost_multiplier']:g}"
        folder = prior / "runs" / previous_name
        previous = {**json.loads((folder / "summary.json").read_text(encoding="utf-8")), **_legacy_costs(folder)}
        for metric in metrics:
            old, new = previous.get(metric), current.get(metric)
            equal = (old is None and new is None) or (old is not None and new is not None
                and math.isclose(float(old), float(new), rel_tol=1e-10, abs_tol=1e-8))
            result.append({"name": current["name"], "prior_name": previous_name, "metric": metric,
                "prior": old, "current": new, "delta": new - old if old is not None and new is not None else None,
                "matches_prior": equal})
    return result


def candidate_comparisons(summaries):
    completed = {(s["arm"], s["window"], s["cost_multiplier"]): s for s in summaries if s["status"] == "completed"}
    result = []
    for row in completed.values():
        for control in ("baseline", "regime_all", "prior_momentum_no_obv"):
            reference = completed.get((control, row["window"], row["cost_multiplier"]))
            if reference is None or control == row["arm"]:
                continue
            result.append({"arm": row["arm"], "window": row["window"], "cost_multiplier": row["cost_multiplier"],
                "control": control, "return_improvement_pp": row["return_pct"] - reference["return_pct"],
                "drawdown_change_pp": row["max_drawdown_pct"] - reference["max_drawdown_pct"],
                "net_cash_cost_change_quote": row["net_cash_cost_quote"] - reference["net_cash_cost_quote"],
                "fills_change": row["fills"] - reference["fills"],
                "retrospective_only": True})
    return result


@_quiet_logging()
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/binance/1d/_manifest.json")
    parser.add_argument("--prior", type=Path, default=ROOT / "reports/paper_applications_20261003_v3")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/paper_return_followup_engine_20261004")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--register-only", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("fresh output directory required; prior attempts are retained")
    prior = args.prior.resolve()
    prior_registration = json.loads((prior / "registration.json").read_text(encoding="utf-8"))
    frames, data = load_verified_inputs(args.manifest)
    jobs = build_jobs(prior_registration)
    sources = source_identity()
    prior_files = [prior / "registration.json"]
    for job in jobs:
        if job["arm"] in CONTROLS and job["window"] != "continuous":
            name = f"{CONTROLS[job['arm']]}_{job['window']}_cost{job['cost_multiplier']:g}"
            prior_files.extend(prior / "runs" / name / filename for filename in
                               ("resolved_config.json", "summary.json", "trades.csv", "financing_ledger.csv"))
    identity = {"schema": "retrospective-return-followup/v1", "registered_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest.resolve()), "data": data, "source_hashes": sources,
        "frame_hashes": {s: sha256_frame(f) for s, f in frames.items()},
        "prior_files": {str(p): sha256_file(p) for p in sorted(set(prior_files))},
        "jobs": jobs, "candidate_family": list(ARMS), "windows": STUDY_WINDOWS, "maximum_runs": len(jobs),
        "workers": args.workers, "initial_capital": 10000., "entry_volatility_target": .10,
        "observers_enabled": False, "production_config_changes": False, "real_venue_orders": False,
        "parameter_origin": "old v3 baseline cost1 resolved parameters; all 18 old control configurations match old registration exactly",
        "costs": "1x and 1.5x original commission, spread, slippage, impact, borrow and liquidation policy; no new historical rate calibration",
        "cash_cost_definition": "commission cash plus signed financing; price-embedded slippage already changes fills, not charged again",
        "scope": "retrospective fixed BTC/ETH comparison; previously seen data, incomplete global search history, no promotion or unopened holdout claim",
        "continuous_scope": "one account from 2021-01-01 through 2026-09-19; existing breaker may terminate and remaining flat days stay cash",
        "source_scope": "all core/backtest/strategy/router/composition/config sources and direct paper runner/analysis dependencies",
        "runtime": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}}
    args.output.mkdir(parents=True, exist_ok=False)
    digest = freeze_registration(args.output / "registration.json", identity)
    # Retain the exact inputs and sources behind the hashes before any engine run.
    for name in sources:
        target = args.output / "frozen_sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    input_folder = args.output / "frozen_inputs"
    input_folder.mkdir()
    shutil.copyfile(args.manifest, input_folder / "_manifest.json")
    for entry in data["symbols"].values():
        shutil.copyfile(entry["path"], input_folder / entry["file"])
    write_json(args.output / "registration_receipt.json", {"registration_sha256": digest,
        "registered_jobs": len(jobs), "results_seen_at_registration": False,
        "historical_results_already_seen": True, "status": "registered"})
    print(f"REGISTERED {len(jobs)} jobs {digest}", flush=True)
    if args.register_only:
        return {"status": "registered", "registration_sha256": digest}
    summaries = []
    tasks = [(str((args.output / "registration.json").resolve()), job["name"], digest) for job in jobs]
    if args.workers == 1:
        for task in tasks:
            summary = _worker(task)
            summaries.append(summary)
            print(f"{summary['status'].upper()} {summary['name']} return={summary.get('return_pct')}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_worker, task): task[1] for task in tasks}
            for future in as_completed(futures):
                try:
                    summary = future.result()
                except Exception as exc:
                    summary = {"name": futures[future], "status": "failed", "error_category": type(exc).__name__}
                summaries.append(summary)
                print(f"{summary['status'].upper()} {summary['name']} return={summary.get('return_pct')}", flush=True)
    order = {job["name"]: i for i, job in enumerate(jobs)}
    summaries.sort(key=lambda s: order[s["name"]])
    drift = compare_prior(summaries, prior)
    comparisons = candidate_comparisons(summaries)
    source_unchanged = source_identity() == sources
    prior_unchanged = all(sha256_file(Path(path)) == hashed for path, hashed in identity["prior_files"].items())
    _, current_data = load_verified_inputs(args.manifest)
    data_unchanged = current_data == data
    complete = len(summaries) == len(jobs) and all(s["status"] == "completed" for s in summaries)
    result = {"schema": "retrospective-return-followup-results/v1", "registration_sha256": digest,
        "status": "complete" if complete and source_unchanged and prior_unchanged and data_unchanged else "incomplete_or_changed",
        "registered_runs": len(jobs), "completed_runs": sum(s["status"] == "completed" for s in summaries),
        "source_identity_unchanged": source_unchanged, "data_identity_unchanged": data_unchanged,
        "prior_artifacts_unchanged": prior_unchanged, "old_control_metric_checks": len(drift),
        "old_control_mismatches": sum(not r["matches_prior"] for r in drift), "summaries": summaries,
        "retrospective_only": True, "live_admission": False, "promoted_candidates": [],
        "production_config_changes": False, "real_venue_orders": False}
    write_json(args.output / "results.json", result)
    write_json(args.output / "prior_control_comparison.json", drift)
    write_json(args.output / "candidate_comparisons.json", comparisons)
    pd.DataFrame(summaries).drop(columns=["lifecycle", "diagnostic_gates"], errors="ignore").to_csv(args.output / "summary.csv", index=False)
    pd.DataFrame(drift).to_csv(args.output / "prior_control_comparison.csv", index=False)
    pd.DataFrame(comparisons).to_csv(args.output / "candidate_comparisons.csv", index=False)
    print(json.dumps({k: v for k, v in result.items() if k != "summaries"}), flush=True)
    return result


if __name__ == "__main__":
    outcome = main()
    raise SystemExit(0 if outcome["status"] in {"complete", "registered"} else 2)
