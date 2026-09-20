"""Offline monthly/quarterly diagnostics; automation never opens the final sample.

Inputs are a hash-verified Binance cache or a Phase-2 report/input snapshot.
The date arguments are inclusive UTC calendar dates. A new output directory is
required. Research diagnostics cannot change strategy parameters or admission.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from analysis.optimize import build_optimization_strategies
from analysis.strategy_review import validate_prospective
from analysis.validation import ValidationConfig, validate_parameter_candidates
from analysis.walk_forward import candidate_warmup
from backtest.capacity import run_capacity_curve
from backtest.engine import BacktestEngine
from composition.factory import (
    build_candidate_score_policy, build_protective_stop_policy, build_strategy_health_policy,
)
from config.config import ConfigLoader, config
from core.data_fetcher import DataFetcher
from core.metrics import benjamini_hochberg, one_sided_bootstrap_p_value
from core.reproducibility import canonical_json, save_data_snapshots, sha256_file
from core.timeframes import timeframe_delta
from scripts.roadmap_baseline import source_manifest

TASKS = ("monthly-optimize", "quarterly-robust")
DEFAULT_CANDIDATES = ((20, 10), (30, 10))
RAW_COLUMNS = ("open", "high", "low", "close", "volume", "funding_rate",
               "borrow_rate_annual", "borrow_available_qty", "spread_bps", "volatility",
               "entry_blocked", "scheduled_exit")


class InsufficientResearch(ValueError):
    """Valid local workflow, but the requested research observations are missing."""


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def save(path, value):
    Path(path).write_text(canonical_json(value) + "\n", encoding="utf-8", newline="")


def parse_candidates(value):
    values = json.loads(value) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 16:
        raise ValueError("candidates must contain 1..16 [entry_window, exit_window] pairs")
    pairs = []
    for pair in values:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(n) is not int for n in pair)
                or not 2 <= pair[1] < pair[0] <= 365):
            raise ValueError("candidate windows must be integers with 2 <= exit < entry <= 365")
        pairs.append(tuple(pair))
    if len(set(pairs)) != len(pairs):
        raise ValueError("duplicate candidate pairs")
    return tuple(pairs)


def utc_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("start/end must be inclusive UTC YYYY-MM-DD dates")
    return pd.Timestamp(value, tz="UTC")


def check_protocol(path, start, end):
    path = Path(path).resolve()
    content = path.read_bytes()
    record = validate_prospective(json.loads(content))
    left, right = utc_date(start), utc_date(end) + pd.Timedelta(days=1)
    boundary = pd.Timestamp(record["test_start"])
    if left >= right or right > boundary:
        raise PermissionError("Research dates must be entirely before protocol.test_start")
    if record["status"] != "pending_unseen_evidence" or path.with_suffix(path.suffix + ".opened").exists():
        raise PermissionError("Scheduler requires a closed prospective protocol")
    if sha256_file(ROOT / "config/params.yaml") != record["config_hash"]:
        raise ValueError("Protocol configuration hash differs from the active configuration")
    return record, content, left, right


def _contained_file(directory, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Input manifest paths must be relative")
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("Input manifest path escapes its directory or is missing")
    return path


def _validate_frame(frame, *, symbol, start, end, boundary, timeframe):
    if not all(name in frame for name in RAW_COLUMNS[:5]):
        raise ValueError(f"Missing OHLCV columns for {symbol}")
    frame.index = pd.to_datetime(frame.index, utc=True, errors="raise")
    if frame.empty or frame.index.hasnans or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(f"Empty, duplicate or unordered timestamps for {symbol}")
    delta = pd.Timedelta(timeframe_delta(timeframe))
    if frame.index[-1] + delta > boundary:
        raise PermissionError(f"Input file for {symbol} contains the protected final sample")
    # Cached derived indicators are never trusted as causal input facts.
    frame = frame.loc[(frame.index >= start) & (frame.index < end),
                      [name for name in RAW_COLUMNS if name in frame]].copy()
    expected = pd.date_range(start, end, freq=delta, inclusive="left")
    if not frame.index.equals(expected):
        raise InsufficientResearch(f"Incomplete requested bar coverage for {symbol}")
    prices = frame[list(RAW_COLUMNS[:5])].apply(pd.to_numeric, errors="raise")
    if (not np.isfinite(prices.to_numpy()).all() or (prices.iloc[:, :4] <= 0).any().any()
            or (prices["volume"] < 0).any()
            or (prices["high"] < prices[["open", "low", "close"]].max(axis=1)).any()
            or (prices["low"] > prices[["open", "high", "close"]].min(axis=1)).any()):
        raise ValueError(f"Invalid OHLCV values for {symbol}")
    frame[list(RAW_COLUMNS[:5])] = prices
    frame.index = frame.index.tz_localize(None)
    return frame


def load_local_inputs(directory, symbols, *, start, end, boundary, timeframe):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise InsufficientResearch("Local verified data directory is missing")
    cache_path, report_path = directory / "_manifest.json", directory / "run_manifest.json"
    if cache_path.is_file():
        manifest_path = cache_path
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if (manifest.get("schema_version") != "binance-cache/v2"
                or manifest.get("provider") != "binance" or manifest.get("market_type") != "spot"
                or manifest.get("timeframe") != timeframe or manifest.get("failures")):
            raise ValueError("Cache source/timeframe/refresh evidence is invalid")
        entries, data_root, kind = manifest.get("symbols", {}), directory, "binance-cache/v2"
    else:
        if not report_path.is_file() and directory.name == "data_inputs":
            report_path = directory.parent / "run_manifest.json"
        if not report_path.is_file():
            raise InsufficientResearch("A verified _manifest.json or run_manifest.json is required")
        manifest_path = report_path
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        identity = manifest.get("data", {})
        if (manifest.get("schema_version") != "2.0" or identity.get("timeframe") != timeframe
                or identity.get("source") not in {"local", "ccxt", "yahoo"}
                or not identity.get("exchange") and identity.get("source") != "yahoo"):
            raise ValueError("Snapshot source/timeframe identity is missing or synthetic")
        entries = manifest.get("data_snapshots", {})
        data_root, kind = report_path.parent / "data_inputs", "backtest-snapshot/2.0"
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    frames, records = {}, {}
    for symbol in symbols:
        entry = entries.get(symbol)
        if not isinstance(entry, dict):
            raise InsufficientResearch(f"Missing manifest entry for {symbol}")
        if kind == "binance-cache/v2":
            normalized = symbol.upper().replace("/", "-").replace("_", "-")
            if (entry.get("provider") != "binance" or entry.get("market_type") != "spot"
                    or entry.get("timeframe") != timeframe or entry.get("symbol") != normalized
                    or entry.get("coverage", {}).get("status") != "complete"):
                raise ValueError(f"Unverified cache entry for {symbol}")
            last, first, relative = entry.get("last"), entry.get("first"), entry.get("file")
        else:
            meta = manifest.get("data", {}).get("symbols", {}).get(symbol, {})
            last, first, relative = meta.get("end"), meta.get("start"), entry.get("path")
        if not first or not last:
            raise ValueError(f"Missing input time coverage for {symbol}")
        declared_last = pd.to_datetime(last, utc=True, errors="raise")
        if declared_last + pd.Timedelta(timeframe_delta(timeframe)) > boundary:
            raise PermissionError("Input metadata reaches the final sample; supply a pre-holdout snapshot")
        path = _contained_file(data_root, relative)
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != entry.get("sha256"):
            raise ValueError(f"Input hash mismatch for {symbol}")
        frame = pd.read_csv(io.BytesIO(content), index_col="timestamp", float_precision="round_trip")
        actual_times = pd.to_datetime(frame.index, utc=True, errors="raise")
        if (len(actual_times) == 0 or actual_times[0] != pd.to_datetime(first, utc=True)
                or actual_times[-1] != declared_last):
            raise ValueError(f"Input time coverage disagrees with manifest for {symbol}")
        frames[symbol] = _validate_frame(frame, symbol=symbol, start=start, end=end,
                                         boundary=boundary, timeframe=timeframe)
        records[symbol] = {"path": str(path), "sha256": actual, "manifest_entry": entry}
    if sha256_file(manifest_path) != manifest_hash:
        raise ValueError("Input manifest changed while loading")
    return frames, {"kind": kind, "manifest_path": str(manifest_path),
                    "manifest_sha256": manifest_hash, "manifest": manifest, "files": records,
                    "verification": "manifest_hash_and_coverage_not_independent_source_confirmation"}


def synthetic_inputs(symbols, start, end, timeframe):
    if timeframe != "1d" or (end - start).days > 2000:
        raise ValueError("Synthetic diagnostics support at most 2000 daily bars")
    rng_state = np.random.get_state()
    try:
        np.random.seed(42)
        fetcher = DataFetcher(proxy_url=None, data_timezone="UTC")
        return {symbol: fetcher.generate_scenario(symbol, str(start.date()),
                    str((end - pd.Timedelta(days=1)).date())) for symbol in symbols}
    finally:
        np.random.set_state(rng_state)


def strategies_for(pair):
    strategies = build_optimization_strategies(*pair)
    for strategy in strategies.values():
        for method, policy in (
            ("configure_health_policy", build_strategy_health_policy),
            ("configure_stop_policy", build_protective_stop_policy),
            ("configure_score_policy", build_candidate_score_policy),
        ):
            if callable(getattr(strategy, method, None)):
                getattr(strategy, method)(policy(config))
    return strategies


def run_partition(frames, pair, *, name, trading_start, warmup, timeframe, output):
    engine = BacktestEngine(initial_capital=10000., warmup_period=warmup, timeframe=timeframe,
                            trading_start=trading_start, random_slip=False,
                            run_id=f"research-{pair[0]}-{pair[1]}-{name}")
    result = engine.run({s: f.copy(deep=True) for s, f in frames.items()},
                        strategies=strategies_for(pair), routing_log_enabled=False)
    check = result.get("accounting_check", {})
    if not check.get("ok") or check.get("checks_performed", 0) <= 0:
        raise ValueError("Engine accounting reconciliation did not pass")
    curve = result["equity_curve"]
    returns = curve["equity"].pct_change(fill_method=None).dropna()
    returns = returns.loc[returns.index >= trading_start]
    if not np.isfinite(returns.to_numpy()).all():
        raise ValueError("Engine produced nonfinite returns")
    output.mkdir(parents=True, exist_ok=False)
    returns.rename("return").to_csv(output / "returns.csv", index_label="timestamp")
    curve.to_csv(output / "equity.csv", index_label="timestamp")
    cohorts = result.get("strategy_health_cohorts", [])
    save(output / "facts.json", {"accounting_check": check, "trades": result.get("trades", []),
                                  "strategy_health_cohorts": cohorts})
    return returns, {"return_bars": len(returns), "fills": len(result.get("trades", [])),
                     "counted_health_cohorts": sum(bool(c.get("counts_toward_health"))
                         and bool(c.get("closed_at")) for c in cohorts),
                     "mean_return": None if returns.empty else float(returns.mean())}


def research_diagnostics(frames, candidates, *, task, timeframe, capital_levels, output):
    timeline = next(iter(frames.values())).index
    minimum = max(30, int(config.require("state", "ma_slow")))
    warmup = max(candidate_warmup(lambda pair=p: strategies_for(pair), minimum=minimum)
                 for p in candidates)
    split = int(len(timeline) * .70)
    if split - warmup < 30 or len(timeline) - split < 30:
        raise InsufficientResearch("Need at least 30 training and 30 validation bars after warmup")
    partitions = {
        "train": {s: f.iloc[:split].copy() for s, f in frames.items()},
        "validation": {s: f.iloc[split - warmup:].copy() for s, f in frames.items()},
    }
    records, combined, p_values = {}, {}, []
    train_count = None
    for pair in candidates:
        candidate = f"entry={pair[0]},exit={pair[1]}"
        series, facts = {}, {}
        for phase, begin in (("train", timeline[warmup]), ("validation", timeline[split])):
            series[phase], facts[phase] = run_partition(partitions[phase], pair, name=phase,
                trading_start=begin, warmup=warmup, timeframe=timeframe,
                output=output / f"entry_{pair[0]}_exit_{pair[1]}" / phase)
        if min(len(s) for s in series.values()) < 30:
            raise InsufficientResearch("Engine returns do not cover both research partitions")
        train_count = len(series["train"])
        combined[candidate] = pd.concat([series["train"], series["validation"]])
        evidence = one_sided_bootstrap_p_value(series["validation"], n_samples=500, seed=42)
        p_values.append(evidence.get("p_value", 1.0))
        records[candidate] = {"parameters": pair, **facts, "validation_p_value": evidence}
    # The existing validator splits by return position. The half-position keeps
    # int(n*fraction) exact even when binary floating point rounds n/n downward.
    length = len(next(iter(combined.values())))
    validation_config = ValidationConfig(train_fraction=(train_count + .5) / length,
        walk_forward_train=train_count, walk_forward_test=length - train_count,
        bootstrap_samples=500, monte_carlo_samples=500, seed=42)
    if any(len(series) != length for series in combined.values()):
        raise ValueError("Candidate return timelines differ")
    if task == "monthly-optimize":
        validation = validate_parameter_candidates(combined, p_values=p_values, config=validation_config)
        diagnostics = {"selected_on": "training_mean_return_only",
                       "selected_candidate": validation["selected_candidate"],
                       "training_scores": validation["training_scores"],
                       "validation_mean_return": validation["oos_mean_return"],
                       "validation_sample_size": validation["oos_sample_size"],
                       "bootstrap": validation["bootstrap"],
                       "return_sequence_monte_carlo": validation["monte_carlo"],
                       "multiple_testing": validation["multiple_testing"]}
    else:
        diagnostics = {"selected_on": "none_fixed_candidates", "selected_candidate": None,
                       "fixed_candidate_diagnostics": {},
                       "multiple_testing": benjamini_hochberg(p_values)}
        for candidate, p_value in zip(combined, p_values):
            result = validate_parameter_candidates({candidate: combined[candidate]},
                p_values=[p_value], config=validation_config)
            diagnostics["fixed_candidate_diagnostics"][candidate] = {
                "validation_mean_return": result["oos_mean_return"],
                "bootstrap": result["bootstrap"], "return_sequence_monte_carlo": result["monte_carlo"]}
        # Capacity's public API uses the configured official registry, never a
        # monthly winner or a modified global configuration.
        diagnostics["capacity"] = {"candidate": "configured_official_defaults_not_grid_winner",
            "data_partition": "pre_holdout_validation_with_training_warmup",
            "result": run_capacity_curve(partitions["validation"], capital_levels=capital_levels,
                engine_kwargs={"timeframe": timeframe, "warmup_period": warmup,
                               "trading_start": timeline[split], "random_slip": False})}
    return {"partitions": {"train_start": timeline[warmup], "train_end_inclusive": timeline[split-1],
                            "validation_start": timeline[split], "validation_end_inclusive": timeline[-1],
                            "warmup_bars": warmup, "training_raw_bar_fraction": .70,
                            "validation_starts_with_flat_positions": True,
                            "validator_return_split_fraction": validation_config.train_fraction},
            "candidates": records, "diagnostics": diagnostics,
            "minimum_validation_health_cohorts": min(r["validation"]["counted_health_cohorts"]
                                                     for r in records.values())}


def run_research_automation(*, task, protocol, data_dir, start, end, symbols, output,
                            synthetic=False, candidates=DEFAULT_CANDIDATES,
                            timeframe="1d", capital_levels=(10000., 100000.)):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "research_automation/v1", "task": task, "engineering_status": "fail",
              "research_status": "not_evaluated", "production_evidence": False,
              "synthetic": bool(synthetic), "holdout_opened": False,
              "admission_changed": False, "configuration_changed": False}
    try:
        if task not in TASKS:
            raise ValueError("Unknown research task")
        candidates = parse_candidates(candidates)
        if (not 1 <= len(symbols) <= 30 or len(set(symbols)) != len(symbols)
                or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9/_:\-]{0,39}", s) for s in symbols)):
            raise ValueError("Provide 1..30 unique canonical symbols")
        levels = [float(n) for n in capital_levels]
        if (not 1 <= len(levels) <= 4 or levels != sorted(set(levels))
                or any(not math.isfinite(n) or not 0 < n <= 100_000_000 for n in levels)):
            raise ValueError("Provide 1..4 ascending positive capital levels no greater than 100 million")
        record, protocol_bytes, left, right = check_protocol(protocol, start, end)
        boundary = pd.Timestamp(record["test_start"])
        source = source_manifest(ROOT)
        config_before = deepcopy(config._config)
        if ConfigLoader(str(ROOT / "config/params.yaml"))._config != config_before:
            raise ValueError("Loaded runtime configuration differs from the hashed config file")
        source_hash = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        (output / "protocol_snapshot.json").write_bytes(protocol_bytes)
        save(output / "source_manifest.json", {"files": source, "source_sha256": source_hash})
        save(output / "resolved_config.json", config_before)
        if synthetic:
            frames = synthetic_inputs(symbols, left, right, timeframe)
            frames = {symbol: _validate_frame(frame, symbol=symbol, start=left, end=right,
                        boundary=boundary, timeframe=timeframe) for symbol, frame in frames.items()}
            input_identity = {"kind": "synthetic", "seed": 42,
                              "generator": "core.data_fetcher.DataFetcher.generate_scenario"}
        else:
            frames, input_identity = load_local_inputs(data_dir, symbols, start=left, end=right,
                                                       boundary=boundary, timeframe=timeframe)
        snapshots = save_data_snapshots(frames, output / "data_inputs")
        identity = {"source_sha256": source_hash, "config_sha256": source["config/params.yaml"],
                    "resolved_config_sha256": digest(config_before),
                    "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                    "protocol_hash": record["protocol_hash"], "registered_code_hash": record["code_hash"],
                    "source_matches_registered_candidate": source_hash == record["code_hash"],
                    "inputs": input_identity, "data_snapshots": snapshots,
                    "parameters": {"task": task, "start": start, "end": end, "timeframe": timeframe,
                                   "symbols": symbols, "candidates": candidates, "capital_levels": levels}}
        save(output / "run_manifest.json", {"schema": "research_automation_manifest/v1",
             "identity_sha256": digest(identity), "identity": identity,
             "created_at": datetime.now(timezone.utc).isoformat()})
        try:
            report.update(research_diagnostics(frames, candidates, task=task, timeframe=timeframe,
                                               capital_levels=levels, output=output / "runs"))
            enough = report["minimum_validation_health_cohorts"] >= int(record["minimum_cohorts"])
            report["research_status"] = "diagnostics_only" if enough else "insufficient"
        except InsufficientResearch as exc:
            report.update(research_status="insufficient", reason=str(exc))
        if (Path(protocol).read_bytes() != protocol_bytes or source_manifest(ROOT) != source
                or config._config != config_before
                or Path(protocol).with_suffix(Path(protocol).suffix + ".opened").exists()):
            raise ValueError("Protocol, source or configuration changed during execution")
        for symbol, snapshot in snapshots.items():
            if sha256_file(output / "data_inputs" / snapshot["path"]) != snapshot["sha256"]:
                raise ValueError(f"Frozen effective input changed for {symbol}")
        report["engineering_status"] = "pass"
        if synthetic:
            report["underlying_research_status"] = report["research_status"]
            report["research_status"] = "synthetic_only"
        report["identity_sha256"] = digest(identity)
    except InsufficientResearch as exc:
        report.update(engineering_status="blocked_missing_inputs", research_status="insufficient", reason=str(exc))
    except Exception as exc:
        report.update(engineering_status="fail", research_status="not_evaluated",
                      error_type=type(exc).__name__, reason=str(exc))
    report["limitations"] = ["Historical train/validation diagnostics only; no final sample opened.",
        "Synthetic data is never production or independent research evidence.",
        "Manifest hashes prove input identity, not independent market-source truth or account reconciliation.",
        "Bootstrap is IID; Monte Carlo permutes validation returns, not execution or independent trade scenarios.",
        "Configured end-of-backtest accounting and fees are retained; capacity remains modeled.",
        "A candidate list bound to this run is not a new prospective registration or live approval."]
    report["artifacts"] = {p.relative_to(output).as_posix(): sha256_file(p)
                           for p in sorted(output.rglob("*")) if p.is_file()}
    save(output / "research_report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=TASKS, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--candidates-json", default=json.dumps(DEFAULT_CANDIDATES))
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--capital-levels", nargs="+", type=float, default=[10000., 100000.])
    args = vars(parser.parse_args(argv))
    args["candidates"] = args.pop("candidates_json")
    try:
        report = run_research_automation(**args)
    except OSError as exc:
        print(json.dumps({"engineering_status": "fail", "reason": str(exc)}))
        return 2
    print(canonical_json({name: report[name] for name in (
        "task", "engineering_status", "research_status", "production_evidence", "holdout_opened")}))
    return 0 if report["engineering_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
