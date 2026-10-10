"""Prove default restored behavior survived opt-in research hooks."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.reporting.operating_periods import requested_period_curve
from config.config import config
from core.reproducibility import (capital_allocation_digest, deterministic_result_digest,
                                 sha256_file)
from research.ml_selection.protocol import load_inputs, load_settings, save_json


def verify(folder):
    settings = load_settings(ROOT / "config/ml_selection.yaml")
    settings["max_symbols"] = None
    frames, _, _, _ = load_inputs(settings)
    registration_path = ROOT / "reports/smart_capital_100k_20261004/registration.json"
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    arm = registration["arms"]["smart"]
    prior, logging_state = config._config, logging.root.manager.disable
    try:
        config._config = deepcopy(arm["parameters"])
        options = deepcopy(arm["engine_options"])
        options["trading_start"] = pd.Timestamp(options["trading_start"]).tz_localize(None)
        random.seed(42)
        np.random.seed(42)
        logging.disable(logging.INFO)
        engine = BacktestEngine(**options)
        result = engine.run(frames, routing_log_enabled=False)
    finally:
        config._config = prior
        logging.disable(logging_state)
    historical = ROOT / "reports/smart_capital_100k_20261004/smart"
    expected = pd.read_csv(historical / "equity_requested_period.csv", index_col=0,
                           parse_dates=True, float_precision="round_trip")
    curve = requested_period_curve(result["equity_curve"], registration["start"], registration["end"],
        capital=registration["capital"], lifecycle=result["lifecycle"], activity=result.get("strategy_activity", []))
    if not curve.index.equals(expected.index):
        raise ValueError("restored baseline calendar changed")
    difference = float(np.abs(curve.equity - expected.equity).max())
    digest = json.loads((historical / "digest.json").read_text(encoding="utf-8"))
    summary = json.loads((historical / "summary.json").read_text(encoding="utf-8"))
    checks = {"daily_equity_exact": difference == 0,
              "execution_digest_exact": deterministic_result_digest(result) == digest,
              "capital_digest_exact": capital_allocation_digest(result) == summary["capital_allocation_digest"],
              "accounting_ok": result["accounting_check"]["ok"]}
    evidence = {"checks": checks, "all_passed": all(checks.values()),
                "daily_equity_max_abs_difference": difference, "days": len(curve),
                "fills": len(result["trades"]), "final_equity": float(curve.equity.iloc[-1]),
                "historical_registration_sha256": sha256_file(registration_path),
                "scope": "original spot_margin smart account unchanged; new spot ML experiment separately evaluated"}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    curve.to_csv(folder / "equity.csv", index_label="timestamp")
    save_json(folder / "validation.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    if not evidence["all_passed"]:
        raise ValueError("restored baseline invariance failed")
    return evidence


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    verify(parser.parse_args().output)
