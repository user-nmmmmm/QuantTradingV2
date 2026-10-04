"""Isolated real-engine walk-forward research. Never fetches remote data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_timestamp(value: Any) -> str:
    """The daily engine's naive clock is UTC, never browser-local time."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    """Preserve unavailable statistics as null, including NumPy scalar values."""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def run_research(identifier: str, parameters: dict[str, Any], output: Path,
                 data_dir: Path, config_path: Path, config_sha256: str) -> dict[str, Any]:
    from dashboard.backtest_worker import initialize_configuration
    initialize_configuration(config_path, config_sha256)
    # Imports occur after configuration isolation, only in the worker process.
    import numpy as np
    import pandas as pd
    from analysis.research_evidence import ResearchEvidenceRun
    from analysis.walk_forward import WalkForwardConfig, candidate_warmup, run_walk_forward
    from composition.factory import build_strategy_registry
    from config.config import ConfigLoader, config
    from core.data_fetcher import DataFetcher
    from dashboard.market_analysis import validate_data_selection
    from dashboard.robust_research import window_geometry
    from main import _load_local_ohlcv

    np.random.seed(parameters["seed"])
    random.seed(parameters["seed"])
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.snapshot.yaml").write_bytes(config_path.read_bytes())
    geometry = window_geometry(parameters, config._config)
    if parameters["source"] == "local":
        validate_data_selection(data_dir, parameters["symbols"], parameters["start"], parameters["end"])
    data_map = {}
    fetcher = DataFetcher()
    for symbol in parameters["symbols"]:
        frame = (_load_local_ohlcv(symbol, parameters["start"], parameters["end"], str(data_dir))
                 if parameters["source"] == "local" else
                 fetcher.generate_scenario(symbol, parameters["start"], parameters["end"]))
        frame = frame.sort_index()
        required = geometry["required_bars"]
        if len(frame) < required or frame.index.has_duplicates:
            raise ValueError(f"{symbol}: insufficient or duplicate daily observations")
        # A fixed bounded suffix prevents a larger requested date range from
        # silently expanding the candidate/window compute budget.
        data_map[symbol] = frame.iloc[-required:].copy()
    timeline = next(iter(data_map.values())).index
    if any(not frame.index.equals(timeline) for frame in data_map.values()):
        raise ValueError("Research requires an aligned daily timeline across selected symbols")
    if not timeline.equals(pd.date_range(timeline[0], timeline[-1], freq="D")):
        raise ValueError("Research requires contiguous daily observations")

    candidates = {}
    parameter_map = {}
    for entry in parameters["entry_windows"]:
        for exit_window in parameters["exit_windows"]:
            name = f"entry={entry},exit={exit_window}"
            candidate_configuration = ConfigLoader(str(config_path))
            candidate_configuration._config = copy.deepcopy(config._config)
            candidate_configuration._config["research"]["trend_breakout_parameters"] = {
                "entry_window": entry, "exit_window": exit_window}

            def build(configuration=candidate_configuration):
                return build_strategy_registry(configuration)

            build.required_history_bars = geometry["warmup_bars"]
            if candidate_warmup(build, geometry["warmup_bars"]) > geometry["warmup_bars"]:
                raise ValueError("Configured strategy history exceeds the registered research warmup")
            candidates[name] = build
            parameter_map[name] = {"entry_window": entry, "exit_window": exit_window}

    wf_config = WalkForwardConfig(
        train_size=parameters["train_bars"], validation_size=parameters["validation_bars"],
        test_size=parameters["test_bars"], purge_size=parameters["purge_bars"],
        step=geometry["step_bars"], warmup_period=geometry["warmup_bars"],
        initial_capital=parameters["capital"], selection_metric=parameters["selection_metric"],
        bootstrap_samples=500, seed=parameters["seed"],
        engine_kwargs={"slippage": parameters["slippage_bps"] / 10000, "timeframe": "1d", "calculate_benchmarks": False},
    )
    evidence = ResearchEvidenceRun(candidates=list(candidates), parameters={
        **asdict(wf_config), "config_sha256": config_sha256, "dashboard_parameters": parameters},
        data_map=data_map, output=output / "evidence")
    data_output = output / "data"
    data_output.mkdir()
    for symbol, frame in data_map.items():
        frame.to_csv(data_output / (symbol.replace("/", "_") + ".csv"), index_label="timestamp")
    print(f"Registered {len(candidates)} candidates, at most {parameters['windows']} evaluated windows, {len(timeline)} daily bars", flush=True)
    report = run_walk_forward(data_map, candidates, wf_config, evidence_run=evidence)
    if len(report["windows"]) > parameters["windows"]:
        raise RuntimeError("Research exceeded its registered window budget")
    # The engine adapter supplies per-window OOS returns. Plot only those
    # boundaries; do not invent an unobserved daily equity path.
    curve = []
    value = 100.0
    for window in report["windows"]:
        if window["test_return"] is None:
            continue
        if not curve:
            first = pd.Timestamp(window["test_start"]) - pd.Timedelta(days=1)
            curve.append({"timestamp": utc_timestamp(first), "value": value})
        value *= 1 + window["test_return"]
        curve.append({"timestamp": utc_timestamp(window["test_end"]), "value": value})
    result = {"id": identifier, "parameters": parameters, "config_sha256": config_sha256,
        "methodology": {
            "selection": "Each window selects only on validation scores; test returns never select or rank candidates. Training scores diagnose stability.",
            "strategy": "TrendBreakout entry/exit grid; current snapshot risk, fees, health and OBV confirmation; other market states hold cash.",
            "window_policy": "Use the last required daily observations inside the requested dates. Initial split is skipped to supply full warmup; step=max(warmup,test). Test windows never overlap; gaps have no simulated return.",
            "capital": "Each window starts with the same capital and forces exits at its end. Procedure returns compound selected OOS windows only; plotted points are window boundaries.",
            "heatmap": "Pooled OOS total return for every registered candidate, shown in parameter order for diagnosis, never used to select a winner.",
            "evidence": "Retrospective walk-forward evidence, not an independent untouched holdout or production admission.",
        },
        "data": {"source": parameters["source"], "symbols": parameters["symbols"],
                 "requested_start": parameters["start"], "requested_end": parameters["end"],
                 "effective_start": utc_timestamp(timeline[0]), "effective_end": utc_timestamp(timeline[-1]),
                 "bars": len(timeline), **geometry,
                 "data_hashes": evidence.registration["data_hashes"]},
        "heatmap": [{**parameter_map[name], **row} for name, row in report["candidates"].items()],
        "curve": curve,
        "windows": [{**window, **{key: utc_timestamp(window[key]) for key in
                    ("train_start", "validation_start", "test_start", "test_end")}} for window in report["windows"]],
        **{key: report[key] for key in ("selection_metric", "skipped_windows", "procedure",
                                       "candidates", "multiple_testing", "identity_check", "admission_eligible")}}
    full = _json_value(report)
    (output / "walk_forward.json").write_text(json.dumps(full, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    result = _json_value(result)
    temporary = output / "result.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    temporary.replace(output / "result.json")
    print(f"Research complete: {len(report['windows'])} evaluated windows; OOS total return={report['procedure']['total_return']}", flush=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline bounded dashboard walk-forward worker")
    parser.add_argument("--id", required=True)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    args = parser.parse_args(argv)
    if hashlib.sha256(args.config.read_bytes()).hexdigest() != args.config_sha256:
        raise ValueError("Research configuration hash mismatch")
    run_research(args.id, json.loads(args.parameters), args.output_dir, args.data_dir, args.config, args.config_sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
