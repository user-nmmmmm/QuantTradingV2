"""Explicit retrospective analysis of the old Phase 0 archive.

This entrypoint cannot grant current-strategy or unseen-holdout admission.
Use scripts/run_strategy_review.py with a newly frozen batch for current-code
research. Preserve the old docs/phase5 reports as historical evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.research_validation import AdmissionThresholds, evaluate_holdout_admission, walk_forward_splits
from backtest.reporting import ReportGenerator
from backtest.reporting.serialization import metrics_document
from core.metrics import calculate_cost_sensitivity

PRIMARY = ROOT / "docs/baseline/phase0/archived_reports/20260824_163836_3498d_10Syms_Ret-15.8pct"


def phase5_config(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["phase5"]
    keys = {
        "partition": {"train_fraction", "validation_fraction", "holdout_fraction"},
        "walk_forward": {"train_bars", "validation_bars", "test_bars", "purge_bars", "embargo_bars"},
        "admission": {"minimum_profit_factor", "minimum_profit_factor_ci_lower", "maximum_drawdown",
                      "concentration_remove_top", "cost_multipliers"},
    }
    if set(raw) != set(keys):
        raise ValueError("unknown or missing phase5 configuration sections")
    for section, required in keys.items():
        if set(raw[section]) != required:
            raise ValueError(f"unknown or missing phase5.{section} configuration fields")
    fractions = list(raw["partition"].values())
    if any(not 0 < x < 1 for x in fractions) or abs(sum(fractions) - 1) > 1e-9:
        raise ValueError("phase5 fractions must be positive and sum to one")
    if any(not isinstance(v, int) or v < (0 if k in {"purge_bars", "embargo_bars"} else 1)
           for k, v in raw["walk_forward"].items()):
        raise ValueError("invalid walk-forward configuration")
    costs = raw["admission"]["cost_multipliers"]
    if not costs or any(not isinstance(v, (float, int)) or not 0 < v < float("inf") for v in costs):
        raise ValueError("finite positive cost multipliers required")
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrospective", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "config/params.yaml")
    parser.add_argument("--source", type=Path, default=PRIMARY)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.retrospective or args.output is None:
        parser.error("legacy admission is disabled; use --retrospective --output NEW_DIRECTORY, "
                     "or python scripts/run_strategy_review.py --help for frozen current-code research")
    effective = phase5_config(args.config)
    if args.output.exists():
        raise ValueError("output must be new; historical research must not be overwritten")
    paths = [args.source / name for name in ("equity.csv", "benchmark.csv", "trades.csv")]
    identity = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    curve = pd.read_csv(paths[0], index_col=0, parse_dates=True)["equity"]
    benchmark = pd.read_csv(paths[1], index_col=0, parse_dates=True).iloc[:, 0]
    split = effective["partition"]
    train_end = int(len(curve) * split["train_fraction"])
    evaluation_start = int(len(curve) * (split["train_fraction"] + split["validation_fraction"]))
    if not 0 < train_end < evaluation_start < len(curve):
        raise ValueError("each configured retrospective partition must contain observations")
    wf = effective["walk_forward"]
    windows = walk_forward_splits(evaluation_start, train_size=wf["train_bars"],
        validation_size=wf["validation_bars"], test_size=wf["test_bars"],
        purge_size=wf["purge_bars"], embargo_size=wf["embargo_bars"], expanding=True)
    # Construction has no writes; output is created only once all inputs validate.
    reporter = ReportGenerator.__new__(ReportGenerator)
    closed = reporter._aggregate_round_trips(reporter._reconstruct_closed_trades(pd.read_csv(paths[2])))
    evaluation = [trade for trade in closed if pd.Timestamp(trade["exit_time"]) >= curve.index[evaluation_start]]
    policy = effective["admission"]
    thresholds = AdmissionThresholds(minimum_pf=policy["minimum_profit_factor"],
        minimum_pf_ci_lower=policy["minimum_profit_factor_ci_lower"],
        maximum_drawdown=policy["maximum_drawdown"],
        concentration_removals=tuple(policy["concentration_remove_top"]),
        required_cost_multiplier=max(policy["cost_multipliers"]))
    diagnostic = evaluate_holdout_admission(trades=evaluation,
        equity=curve.iloc[evaluation_start:], benchmark=benchmark, thresholds=thresholds)
    diagnostic["decision"] = "not_eligible_retrospective"
    result = {"schema_version": "phase5-retrospective/v2", "admission_eligible": False,
        "research_status": "insufficient", "source_role": "previously_seen_historical_archive",
        "effective_config": effective, "thresholds": asdict(thresholds), "input_sha256": identity,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "partitions": {"train_end": train_end, "retrospective_evaluation_start": evaluation_start},
        "walk_forward_windows": windows, "diagnostic": diagnostic,
        "cost_sensitivity": calculate_cost_sensitivity(evaluation,
            commission_multipliers=policy["cost_multipliers"], slippage_multipliers=policy["cost_multipliers"]),
        "prospective_protocol": "reports/strategy_review_20260919/prospective_protocol.json",
        "limitation": "No unseen sample was opened. Changed source identity needs its own frozen protocol."}
    # Reuse strict serialization without claiming the historical diagnostic is admission.
    cleaned = metrics_document(result)["metrics"]
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "retrospective_report.json").write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "admission_eligible": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
