"""SR1-HEALTH-007: statistical power of the strategy health lifecycle, by simulation.

Protocol: docs/research/health_power_study_20261110.md. Draws cohort R values
from a train60 run only (demeaned, then shifted to each target expectancy) and
drives the real StrategyHealthMachine day by day. Changes no configuration.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
import pandas as pd

from config.config import config
from core.analytics.strategy_health import HealthStatus, StrategyHealthMachine, StrategyHealthPolicy
from core.foundation.reproducibility import sha256_file
from scripts.run_revalidation60 import save, source_hashes

ROOT = Path(__file__).resolve().parents[1]

EXPERIMENT_ID = "SR1-HEALTH-007"
PROTOCOL = ROOT / "docs/research/health_power_study_20261110.md"
SOURCE_RUN = ROOT / "reports/time_exit_rebuy_study_20261110/runs/baseline_train60"
MUS = (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)
ARMS = {"current": {}, "h3": {"probation_require_positive_without_best": False}}
PATHS, YEARS, SEED = 1000, 6, 20261110
START = datetime(2020, 1, 1, tzinfo=timezone.utc)
RULE = {"min_multiplier_at_plus_0_2": 0.60, "max_multiplier_at_minus_0_3": 0.30,
        "max_first_year_trigger_at_plus_0_2": 0.25, "h3_min_gain_at_plus_0_2": 0.10}


def empirical_inputs(run: Path) -> Dict[str, Any]:
    """Counted cohort R values, symbols and the daily cohort probability of one run."""
    cohorts = pd.read_csv(run / "strategy_health_cohorts.csv")
    counted = cohorts[cohorts["counts_toward_health"].astype(str).str.lower() == "true"]
    transitions = pd.read_csv(run / "strategy_health_transitions.csv")
    equity = pd.read_csv(run / "equity_requested_period.csv", index_col="timestamp", parse_dates=True)
    first, last = equity.index.min(), equity.index.max()
    days = (last - first).days + 1
    cooldown_days = cooldown_day_count(transitions, first, last)
    symbols = sorted({s for cell in counted["symbols"] for s in parse_symbols(cell)})
    return {"r": counted["r"].astype(float).tolist(), "symbols": symbols,
            "days": days, "cooldown_days": cooldown_days,
            "p": len(counted) / max(days - cooldown_days, 1)}


def parse_symbols(cell: Any) -> List[str]:
    value = ast.literal_eval(cell) if isinstance(cell, str) else cell
    return [str(symbol) for symbol in (value or [])]


def cooldown_day_count(transitions: pd.DataFrame, first: Any, last: Any) -> int:
    """Calendar days spent in COOLDOWN between ``first`` and ``last``."""
    if transitions.empty:
        return 0
    rows = transitions.assign(at=pd.to_datetime(transitions["at"]).dt.tz_localize(None)).sort_values("at")
    total, entered = 0, None
    for _, row in rows.iterrows():
        if row["to"] == "cooldown" and entered is None:
            entered = row["at"]
        elif row["to"] != "cooldown" and entered is not None:
            total += (row["at"] - entered).days
            entered = None
    if entered is not None:
        total += (pd.Timestamp(last).tz_localize(None) - entered).days
    return int(total)


def simulate_path(policy: StrategyHealthPolicy, r_values: Sequence[float], symbols: Sequence[str],
                  p: float, days: int, rng: np.random.Generator) -> Dict[str, Any]:
    machine = StrategyHealthMachine("Sim", policy)
    multipliers = np.zeros(days)
    active = cooldown = 0
    cooldowns, first_cooldown = 0, None
    previous = machine.status
    for day in range(days):
        now = START + timedelta(days=day)
        status = machine.status
        # Only these states can change on a day without a new cohort.
        if status is HealthStatus.PROBATION or (
                status is HealthStatus.COOLDOWN and machine.cooldown_until is not None
                and now >= machine.cooldown_until):
            machine.evaluate(now)
        status = machine.status
        if status is HealthStatus.COOLDOWN and previous is not HealthStatus.COOLDOWN:
            cooldowns += 1
            first_cooldown = day if first_cooldown is None else first_cooldown
        previous = status
        multipliers[day] = machine.risk_multiplier
        active += status is HealthStatus.ACTIVE
        cooldown += status is HealthStatus.COOLDOWN
        if status in (HealthStatus.ACTIVE, HealthStatus.PROBATION) and rng.random() < p:
            machine.ingest_close(close_event_id=f"c{day}", symbol=str(rng.choice(symbols)),
                                 realized_pnl=float(rng.choice(r_values)), initial_risk=1.0,
                                 exit_reason="signal", timestamp=now)
            machine.evaluate(now)
            if machine.status is HealthStatus.COOLDOWN and previous is not HealthStatus.COOLDOWN:
                cooldowns += 1
                first_cooldown = day if first_cooldown is None else first_cooldown
            previous = machine.status
    return {"mean_multiplier": float(multipliers.mean()), "active_share": active / days,
            "cooldown_share": cooldown / days, "cooldowns": cooldowns,
            "first_year_trigger": first_cooldown is not None and first_cooldown < 365}


def simulate(arm: str, mu: float, inputs: Dict[str, Any], paths: int = PATHS,
             years: int = YEARS, seed: int = SEED) -> Dict[str, Any]:
    logging.disable(logging.CRITICAL)
    policy = StrategyHealthPolicy.from_mapping({**(config.get("strategy_health") or {}), **ARMS[arm]})
    base = np.asarray(inputs["r"], dtype=float)
    shifted = (base - base.mean() + mu).tolist()
    rng = np.random.default_rng([seed, MUS.index(mu) if mu in MUS else 99, list(ARMS).index(arm)])
    days = years * 365
    rows = [simulate_path(policy, shifted, inputs["symbols"], inputs["p"], days, rng) for _ in range(paths)]
    frame = pd.DataFrame(rows)
    return {"arm": arm, "mu": mu, "mean_multiplier": float(frame.mean_multiplier.mean()),
            "active_share": float(frame.active_share.mean()),
            "cooldown_share": float(frame.cooldown_share.mean()),
            "first_year_trigger_rate": float(frame.first_year_trigger.mean()),
            "cooldowns_per_year": float(frame.cooldowns.mean() / years)}


def verdict(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Protocol section 3."""
    table = {(row["arm"], round(row["mu"], 3)): row for row in rows}
    current_up, current_down = table[("current", 0.2)], table[("current", -0.3)]
    checks = {"multiplier_at_plus_0_2": current_up["mean_multiplier"] >= RULE["min_multiplier_at_plus_0_2"],
              "multiplier_at_minus_0_3": current_down["mean_multiplier"] <= RULE["max_multiplier_at_minus_0_3"],
              "first_year_trigger_at_plus_0_2":
                  current_up["first_year_trigger_rate"] <= RULE["max_first_year_trigger_at_plus_0_2"]}
    adequate = all(checks.values())
    h3_candidate = False
    if not adequate:
        h3_up, h3_down = table[("h3", 0.2)], table[("h3", -0.3)]
        h3_candidate = (h3_up["mean_multiplier"] - current_up["mean_multiplier"] >= RULE["h3_min_gain_at_plus_0_2"]
                        and h3_down["mean_multiplier"] <= RULE["max_multiplier_at_minus_0_3"])
    conclusion = ("current_discrimination_adequate" if adequate
                  else "h3_is_stage_C1_health_candidate" if h3_candidate
                  else "discrimination_weak_no_health_change_in_stage_C")
    return {"checks": checks, "adequate": adequate, "h3_candidate": h3_candidate, "conclusion": conclusion}


def _job(args):
    return simulate(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/health_power_study_20261110")
    parser.add_argument("--source-run", type=Path, default=SOURCE_RUN)
    parser.add_argument("--workers", type=int, default=7)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    inputs = empirical_inputs(args.source_run)
    identity = {"schema": "health-power-study/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "source_run_files": {name: sha256_file(args.source_run / name) for name in (
                    "strategy_health_cohorts.csv", "strategy_health_transitions.csv",
                    "equity_requested_period.csv")},
                "empirical": {k: v for k, v in inputs.items() if k != "r"}
                             | {"cohorts": len(inputs["r"]), "mean_r": float(np.mean(inputs["r"]))},
                "mus": list(MUS), "arms": ARMS, "paths": PATHS, "years": YEARS, "seed": SEED, "rule": RULE}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/input identity changed; use a new study output directory")
    else:
        save(manifest, identity)
    if (args.output / "result.json").exists():
        raise ValueError("The registered simulation is single-use and already recorded")
    jobs = [(arm, mu, inputs) for arm in ARMS for mu in MUS]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(_job, jobs))
    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    table = pd.DataFrame(rows)
    table.to_csv(args.output / "power_table.csv", index=False)
    result = {"empirical": identity["empirical"], "rows": rows, "verdict": verdict(rows)}
    save(args.output / "result.json", result)
    print(table.round(3).to_string(index=False), flush=True)
    print(json.dumps(result["verdict"], indent=2), flush=True)


if __name__ == "__main__":
    main()
