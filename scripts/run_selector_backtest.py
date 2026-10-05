"""Compare the registered 100k smart account with an optional frozen selector.

This entry point preserves the original margin account and inputs. It does not
adapt the account to the separate cash spot ML research experiment.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
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

from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from backtest.reporting.operating_periods import requested_period_curve
from backtest.reporting.serialization import write_metrics_json
from config.config import config
from core.reproducibility import (canonical_json, capital_allocation_digest,
                                 deterministic_result_digest, sha256_file, sha256_frame)

DATA_REGISTRATION = Path("reports/multicoin_100k_20261004/registration.json")
SMART_REGISTRATION = Path("reports/smart_capital_100k_20261004/registration.json")
REGISTRATION_HASHES = {
    "data": "109ff9feaf8239e58f322022e06a0cc8a4e1676477a5fcf5773cd26232c79657",
    "smart": "05276bd3c2893883f456bc04579179e8220f8f3aa6d209aede0bc5d445bfe480",
}
HISTORICAL_HASHES = {
    "equity_requested_period.csv": "589455e0bc17d119b273d3379c4d36bf515a3ebffed7a0733b47ba34c6d72f32",
    "digest.json": "d63dc8e1971112724b09e4b0ff11dd2d09a567d22f94b22f276fd19811fa90e6",
    "summary.json": "ee56c0b2d6eceb5533d001d737be0cef24fc1e7aa1d3c4da8220e928521e158f",
}
EXPECTED_FINAL_EQUITY = 172821.7372157314


def _save(path, value):
    Path(path).write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False,
                                    indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _verified_json(path, expected_hash):
    if sha256_file(path) != expected_hash:
        raise ValueError(f"registered artifact identity mismatch: {path}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_registered_baseline(root=ROOT):
    """Load all original bytes and engine frames without changing parameters."""
    root = Path(root).resolve()
    data_path, smart_path = root / DATA_REGISTRATION, root / SMART_REGISTRATION
    data = _verified_json(data_path, REGISTRATION_HASHES["data"])
    smart = _verified_json(smart_path, REGISTRATION_HASHES["smart"])
    if (data["input_files"] != smart["input_files"]
            or data["engine_frame_hashes"] != smart["engine_frame_hashes"]
            or (data["start"], data["end"]) != (smart["start"], smart["end"])):
        raise ValueError("data and smart registrations describe different inputs")
    symbols = data["symbols"]
    if (not symbols or len(set(symbols)) != len(symbols)
            or set(symbols) != set(data["engine_frame_hashes"])):
        raise ValueError("registered symbol roster is incomplete or duplicated")
    input_hashes = {}
    for relative, expected in data["input_files"].items():
        source = (data_path.parent / relative).resolve()
        if not source.is_relative_to(data_path.parent.resolve()):
            raise ValueError("registered input escaped its original directory")
        actual = sha256_file(source)
        if actual != expected:
            raise ValueError(f"frozen input hash mismatch: {relative}")
        input_hashes[relative] = actual
    frames = {}
    for symbol in symbols:
        relative = "input/engine/" + symbol.replace("/", "_") + ".csv"
        if relative not in input_hashes:
            raise ValueError(f"engine input is unregistered: {symbol}")
        frame = pd.read_csv(data_path.parent / relative, index_col="timestamp", parse_dates=True,
                            float_precision="round_trip")
        frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(None)
        if (frame.empty or frame.index.hasnans or frame.index.has_duplicates
                or not frame.index.is_monotonic_increasing
                or (frame.index != frame.index.normalize()).any()
                or not {"open", "high", "low", "close", "volume"} <= set(frame.columns)):
            raise ValueError(f"invalid registered daily frame: {symbol}")
        if sha256_frame(frame) != data["engine_frame_hashes"][symbol]:
            raise ValueError(f"engine frame identity mismatch: {symbol}")
        frames[symbol] = frame
    arm = smart["arms"]["smart"]
    parameters, options = deepcopy(arm["parameters"]), deepcopy(arm["engine_options"])
    if (options["initial_capital"] != smart["capital"]
            or options["initial_capital"] != data["initial_capital"]):
        raise ValueError("registered initial capital disagrees")
    options["trading_start"] = pd.Timestamp(options["trading_start"]).tz_convert(None)
    historical = smart_path.parent / "smart"
    for name, expected in HISTORICAL_HASHES.items():
        if sha256_file(historical / name) != expected:
            raise ValueError(f"historical baseline identity mismatch: {name}")
    evidence = {"data_registration": str(data_path), "smart_registration": str(smart_path),
                "registration_sha256": dict(REGISTRATION_HASHES), "input_file_sha256": input_hashes,
                "engine_frame_sha256": data["engine_frame_hashes"], "symbols": symbols,
                "historical_baseline_sha256": dict(HISTORICAL_HASHES),
                "start": smart["start"], "end": smart["end"],
                "initial_capital": smart["capital"], "account_mode": parameters["account"]["mode"],
                "parameters": parameters, "engine_options": options, "limits": data.get("limits", [])}
    return frames, parameters, options, evidence, historical


def _selector(frames, capital, enabled, selector_bundle=None, *, account_mode=None):
    if not enabled:
        if selector_bundle is not None:
            raise ValueError("selector_bundle requires coin_selector on")
        return None, {"enabled": False, "mode": "off", "model_id": None,
                      "reason": "original registered strategy and smart capital allocation"}
    # Importing/loading ML is confined to the explicitly enabled branch.
    from backtest.coin_selector import create_selector
    selector, identity = create_selector(frames, initial_capital=capital, bundle_path=selector_bundle,
                                         account_mode=account_mode)
    if selector is None or not isinstance(identity, dict) or not identity:
        raise ValueError("enabled coin selector requires a verified frozen model identity")
    if (identity.get("enabled") is not True or identity.get("candidate") != "rl"
            or identity.get("deterministic") is not True
            or not identity.get("source_protocol_id")
            or identity.get("model_id") != getattr(getattr(selector, "policy", None), "model_id", None)
            or identity.get("parent_model_id") != getattr(getattr(selector, "model", None), "model_id", None)
            or identity.get("policy_threshold") != getattr(selector, "policy_threshold", None)
            or identity.get("new_training_updates") != 0
            or identity.get("new_threshold_search") != 0):
        raise ValueError("coin selector identity differs from the fixed loaded RL policy")
    return selector, {**identity, "enabled": True, "mode": "on"}


def _baseline_validation(result, curve, historical):
    expected = pd.read_csv(historical / "equity_requested_period.csv", index_col=0,
                           parse_dates=True, float_precision="round_trip")
    calendar_equal = curve.index.equals(expected.index)
    difference = (float(np.abs(curve.equity - expected.equity).max()) if calendar_equal else None)
    old_summary = json.loads((historical / "summary.json").read_text(encoding="utf-8"))
    old_digest = json.loads((historical / "digest.json").read_text(encoding="utf-8"))
    checks = {"daily_calendar_exact": calendar_equal, "daily_equity_exact": difference == 0.0,
              "final_equity_exact": float(curve.equity.iloc[-1]) == EXPECTED_FINAL_EQUITY,
              "execution_digest_exact": deterministic_result_digest(result) == old_digest,
              "capital_digest_exact": capital_allocation_digest(result) == old_summary["capital_allocation_digest"],
              "days_exact": len(curve) == old_summary["days"],
              "fills_exact": len(result["trades"]) == old_summary["fill_count"],
              "accounting_ok": bool(result["accounting_check"]["ok"])}
    return {"checks": checks, "all_passed": all(checks.values()),
            "daily_equity_max_abs_difference": difference,
            "expected_final_equity": EXPECTED_FINAL_EQUITY,
            "expected_net_profit": EXPECTED_FINAL_EQUITY - old_summary["initial_capital"]}


def run_backtest(output_dir, *, coin_selector="off", selector_bundle=None, root=ROOT):
    total_started = time.monotonic()
    if coin_selector not in {"off", "on"}:
        raise ValueError("coin_selector must be off or on")
    if coin_selector != "on" and selector_bundle is not None:
        raise ValueError("selector_bundle requires coin_selector on")
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    stage_started = time.monotonic()
    frames, parameters, options, evidence, historical = load_registered_baseline(root)
    timings = {"registered_input_load_seconds": time.monotonic() - stage_started}
    stage_started = time.monotonic()
    selector, identity = _selector(frames, options["initial_capital"], coin_selector == "on", selector_bundle,
                                   account_mode=parameters["account"]["mode"])
    timings["selector_setup_seconds"] = time.monotonic() - stage_started
    timings["selector_stages"] = deepcopy(identity.get("timing_seconds", {}))
    stage_started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    identity.update(training_updates=0, threshold_search=False, candidate_search=False,
                    evaluation_kind="retrospective_fixed_policy_comparison", independent_holdout=False)
    _save(output / "coin_selector.json", identity)
    _save(output / "registration.json", {**evidence, "coin_selector": identity,
          "current_runner_sha256": sha256_file(Path(__file__)),
          "current_engine_sha256": sha256_file(ROOT / "backtest/engine.py"),
          "source_equality_with_historical_registration_required": False,
          "purpose": "same registered original account; optional fixed current RL weights",
          "evaluation_limit": "current model is applied retrospectively, including its training period"})
    timings["registration_write_seconds"] = time.monotonic() - stage_started
    prior, logging_state = config._config, logging.root.manager.disable
    py_random, np_random = random.getstate(), np.random.get_state()
    engine_started = time.monotonic()
    try:
        config._config = deepcopy(parameters)
        random.seed(42)
        np.random.seed(42)
        logging.disable(logging.INFO)
        engine = BacktestEngine(**options, candidate_selector=selector)
        result = engine.run(frames, routing_log_enabled=False)
    finally:
        config._config = prior
        logging.disable(logging_state)
        random.setstate(py_random)
        np.random.set_state(np_random)
    timings["engine_seconds"] = time.monotonic() - engine_started
    report_started = time.monotonic()
    capital = options["initial_capital"]
    curve = requested_period_curve(result["equity_curve"], evidence["start"], evidence["end"],
        capital=capital, lifecycle=result["lifecycle"], activity=result.get("strategy_activity", []))
    curve.to_csv(output / "equity.csv", index_label="timestamp")
    curve.to_csv(output / "equity_requested_period.csv", index_label="timestamp")
    result["equity_curve"].to_csv(output / "equity_engine.csv", index_label="timestamp")
    trades = pd.DataFrame(result["trades"])
    if trades.empty:
        trades = pd.DataFrame(columns=["symbol", "side", "qty", "fill_price", "fill_time", "commission"])
    trades.to_csv(output / "trades.csv", index=False)
    report = ReportGenerator(str(output))
    metrics = report.generate(result["trades"], curve, metrics_only=True,
        lifecycle=result["lifecycle"], close_events=result.get("close_events"),
        strategy_health=result.get("strategy_health"), protective_stops=result.get("protective_stops"),
        periods_per_year=365.25)
    write_metrics_json(output / "metrics.json", metrics, {**evidence, "coin_selector": identity})
    closed = report._aggregate_round_trips(report._reconstruct_closed_trades(trades))
    pd.DataFrame(closed, columns=None if closed else
                 ["position_id", "symbol", "strategy_id", "net_pnl", "initial_risk"]).to_csv(
                     output / "closed_trades.csv", index=False)
    for key in ("accounting_check", "lifecycle", "breaker_state", "capital_allocation_audit",
                "allocation_audit", "execution_audit", "financing_ledger", "margin_ledger"):
        _save(output / f"{key}.json", result.get(key))
    if selector is not None:
        from backtest.coin_selector import write_selector_report
        identity = write_selector_report(output, selector, identity)
    final = float(curve.equity.iloc[-1])
    validation = (_baseline_validation(result, curve, historical) if coin_selector == "off"
                  else {"status": "not_applicable", "reason": "selector changes the original decisions"})
    _save(output / "baseline_validation.json", validation)
    timings["reporting_and_validation_seconds"] = time.monotonic() - report_started
    timings["total_seconds"] = time.monotonic() - total_started
    summary = {"coin_selector": coin_selector, "initial_capital": capital, "final_equity": final,
               "net_pnl": final - capital, "return_pct": (final / capital - 1) * 100,
               "max_drawdown_pct": float(((curve.equity.cummax() - curve.equity)
                                             / curve.equity.cummax()).max()) * 100,
               "days": len(curve), "fill_count": len(trades), "start": evidence["start"],
               "end": evidence["end"], "account_mode": parameters["account"]["mode"],
               "symbol_count": len(frames), "accounting_ok": bool(result["accounting_check"]["ok"]),
               "elapsed_seconds": timings["total_seconds"], "timing_seconds": timings,
               "baseline_validation": validation,
               "training_updates": 0, "independent_holdout": False,
               "lifecycle": result["lifecycle"]}
    _save(output / "summary.json", summary)
    _save(output / "timing.json", timings)
    if not summary["accounting_ok"]:
        raise ValueError("backtest account reconciliation failed")
    if coin_selector == "off" and not validation["all_passed"]:
        raise ValueError("registered original strategy reproduction failed; inspect baseline_validation.json")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="复现原10万本金账户，选择是否开启冻结RL选币器")
    parser.add_argument("--coin-selector", choices=("off", "on"), default="off",
                        help="off复现原策略；on加载当前冻结RL模型，不重新训练")
    parser.add_argument("--selector-bundle", default=None,
                        help="冻结选币模型包路径，仅允许与--coin-selector on一起使用")
    parser.add_argument("--output-dir", required=True, help="必须指定尚不存在的新输出目录")
    args = parser.parse_args(argv)
    if args.selector_bundle is not None and args.coin_selector != "on":
        parser.error("--selector-bundle requires --coin-selector on")
    summary = run_backtest(args.output_dir, coin_selector=args.coin_selector,
                           selector_bundle=args.selector_bundle)
    print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
