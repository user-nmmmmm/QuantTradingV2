"""Frozen offline research configuration and verified historical inputs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
from pathlib import Path

import pandas as pd
import yaml

from core.reproducibility import canonical_json, sha256_file, sha256_frame

ROOT = Path(__file__).resolve().parents[2]


def save_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(canonical_json(value))
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def resolve_path(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("research timestamps must be finite")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _number(value, name, *, minimum=None, maximum=None, strict_minimum=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and (value < minimum or (strict_minimum and value == minimum)):
        raise ValueError(f"{name} is below its allowed minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above its allowed maximum")


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _section(settings, name, allowed):
    value = settings.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"unknown {name} settings: {sorted(unknown)}")
    return value


def validate_settings(settings):
    """Validate both parsed files and caller/CLI overrides before freezing."""
    allowed = {"schema", "timeframe", "account_mode", "evaluation_kind", "data_registration",
               "baseline_registration", "baseline_arm", "start", "end", "splits", "symbols",
               "max_symbols", "models", "model_params", "seed", "selection", "random_seeds",
               "dataset", "rl", "stress", "gates", "walk_forward"}
    if not isinstance(settings, dict) or settings.get("schema") != "ml-selection-research/v1":
        raise ValueError("unsupported ML research configuration")
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"unknown research settings: {sorted(unknown)}")
    if settings.get("timeframe") != "1d" or settings.get("account_mode") != "spot":
        raise ValueError("first ML research protocol requires daily spot trading")
    if settings.get("evaluation_kind") != "retrospective":
        raise ValueError("this historical runner cannot certify independent final evidence")
    for name in ("data_registration", "baseline_registration", "start", "end", "splits"):
        if name not in settings:
            raise ValueError(f"missing required research setting: {name}")
    split = _section(settings, "splits", {"train_end", "validation_end"})
    if not {"train_end", "validation_end"} <= set(split):
        raise ValueError("splits requires train_end and validation_end")
    start, train = _utc(settings["start"]), _utc(split["train_end"])
    valid, end = _utc(split["validation_end"]), _utc(settings["end"]) + pd.Timedelta(days=1)
    if not start < train < valid < end:
        raise ValueError("research boundaries must be strictly time ordered")
    windows = settings.get("walk_forward", [])
    if not isinstance(windows, list):
        raise ValueError("walk_forward must be a list")
    for window in windows:
        if not isinstance(window, dict) or set(window) != {"train_end", "validation_end", "test_end"}:
            raise ValueError("walk_forward windows require train_end/validation_end/test_end")
        if not start < _utc(window["train_end"]) < _utc(window["validation_end"]) < _utc(window["test_end"]) <= end:
            raise ValueError("walk_forward boundaries must be ordered within the frozen data period")
    if settings.get("max_symbols") is not None:
        _integer(settings["max_symbols"], "max_symbols")
    if settings.get("symbols") is not None:
        symbols = settings["symbols"]
        if not isinstance(symbols, list) or not symbols or any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be a nonempty list of distinct names")
    models = settings.get("models", ["ridge", "lightgbm"])
    if not isinstance(models, list) or not models or len(set(models)) != len(models) or any(m not in {"ridge", "lightgbm"} for m in models):
        raise ValueError("models must be distinct supported names")
    model_params = _section(settings, "model_params", {"ridge", "lightgbm"})
    if any(not isinstance(value, dict) for value in model_params.values()):
        raise ValueError("model_params values must be mappings")
    _integer(settings.get("seed", 42), "seed", 0)
    seeds = settings.get("random_seeds", [42, 43, 44])
    if not isinstance(seeds, list) or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("random_seeds must be a nonempty list of distinct integers")
    for seed in seeds:
        _integer(seed, "random seed", 0)
    rl = _section(settings, "rl", {"enabled", "episodes", "seed", "seeds", "learning_rate", "entropy_coef",
                                  "gamma", "drawdown_penalty", "turnover_penalty", "validation_patience"})
    policy_seeds = rl.get("seeds", [rl.get("seed", 42)])
    if not isinstance(policy_seeds, list) or not policy_seeds or len(set(policy_seeds)) != len(policy_seeds):
        raise ValueError("rl.seeds must be distinct seed integers")
    for seed in policy_seeds:
        _integer(seed, "RL seed", 0)
    dataset = _section(settings, "dataset", {"horizon_bars", "min_history", "min_quote_volume",
                       "commission_rate", "slippage_bps", "stop_atr_multiple", "membership"})
    for name, minimum in (("horizon_bars", 1), ("min_history", 61)):
        if name in dataset:
            _integer(dataset[name], f"dataset.{name}", minimum)
    for name in ("min_quote_volume", "commission_rate", "slippage_bps", "stop_atr_multiple"):
        if name in dataset:
            _number(dataset[name], f"dataset.{name}", minimum=0, strict_minimum=name == "stop_atr_multiple")
    if dataset.get("commission_rate", .001) >= 1 or dataset.get("slippage_bps", 5) >= 10000:
        raise ValueError("dataset cost assumptions must be below 100%")
    selection = _section(settings, "selection", {"min_expected_return", "score_scale", "score_cap"})
    for name, value in selection.items():
        _number(value, f"selection.{name}", minimum=None if name == "min_expected_return" else 0,
                strict_minimum=name != "min_expected_return")
    if "enabled" in rl and type(rl["enabled"]) is not bool:
        raise ValueError("rl.enabled must be boolean")
    for name in ("episodes", "validation_patience", "seed"):
        if name in rl:
            _integer(rl[name], f"rl.{name}", 0 if name == "seed" else 1)
    for name in ("learning_rate", "entropy_coef", "drawdown_penalty", "turnover_penalty"):
        if name in rl:
            _number(rl[name], f"rl.{name}", minimum=0, strict_minimum=name == "learning_rate")
    if "gamma" in rl:
        _number(rl["gamma"], "rl.gamma", minimum=0, maximum=1)
    stress = _section(settings, "stress", {"cost_multipliers"})
    multipliers = stress.get("cost_multipliers", [1.5, 2.0])
    if not isinstance(multipliers, list) or not multipliers:
        raise ValueError("stress.cost_multipliers must be a nonempty list")
    for value in multipliers:
        _number(value, "stress cost multiplier", minimum=1)
    if len(set(multipliers)) != len(multipliers):
        raise ValueError("stress cost multipliers must be distinct")
    gates = _section(settings, "gates", {"max_drawdown", "require_positive_net_return",
                                       "require_outperform_native", "require_positive_after_removing_top_5"})
    for name, value in gates.items():
        if name == "max_drawdown":
            _number(value, "gates.max_drawdown", minimum=0, maximum=1)
        elif type(value) is not bool:
            raise ValueError(f"gates.{name} must be boolean")
    return settings


def _validate_engine_configuration(parameters, options):
    execution = parameters.get("execution", {})
    for name in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps",
                 "volatility_slippage_factor", "impact_coefficient"):
        if name in execution:
            _number(execution[name], f"execution.{name}", minimum=0)
    for name in ("commission_rate_taker", "commission_rate_maker"):
        if execution.get(name, 0) >= 1:
            raise ValueError(f"execution.{name} must be below 100%")
    if execution.get("slippage_bps", 0) >= 10000:
        raise ValueError("execution.slippage_bps must be below 100%")
    if "initial_capital" in options:
        _number(options["initial_capital"], "initial_capital", minimum=0, strict_minimum=True)
    if "warmup_period" in options:
        _integer(options["warmup_period"], "warmup_period", 0)
    if "slippage" in options:
        _number(options["slippage"], "engine slippage", minimum=0)
    if options.get("portfolio_controller") is not None or parameters.get("portfolio_targets", {}).get("enabled", False):
        raise ValueError("portfolio targets bypass candidate selection")


def load_settings(path):
    settings = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return validate_settings(settings)


def load_inputs(settings):
    validate_settings(settings)
    registration_path = resolve_path(settings["data_registration"])
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    symbols = list(registration["symbols"])
    if not symbols or len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not s for s in symbols):
        raise ValueError("registered symbols must be nonempty and unique")
    if settings.get("symbols"):
        requested = settings["symbols"]
        if len(set(requested)) != len(requested) or not set(requested) <= set(symbols):
            raise ValueError("unknown or duplicate symbols")
        symbols = requested
    if settings.get("max_symbols"):
        symbols = symbols[:settings["max_symbols"]]
    frames, hashes = {}, {}
    for symbol in symbols:
        relative = "input/engine/" + symbol.replace("/", "_") + ".csv"
        source = registration_path.parent / relative
        expected = registration["input_files"][relative]
        actual = sha256_file(source)
        if actual != expected:
            raise ValueError(f"frozen input hash mismatch: {symbol}")
        frame = pd.read_csv(source, index_col="timestamp", parse_dates=True,
                            float_precision="round_trip")
        frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(None)
        if (frame.index.hasnans or frame.index.has_duplicates or not frame.index.is_monotonic_increasing
                or (frame.index != frame.index.normalize()).any()):
            raise ValueError(f"registered daily frame requires unique ordered midnight timestamps: {symbol}")
        if not {"open", "high", "low", "close", "volume"} <= set(frame.columns):
            raise ValueError(f"registered daily frame lacks OHLCV columns: {symbol}")
        if sha256_frame(frame) != registration["engine_frame_hashes"][symbol]:
            raise ValueError(f"engine frame identity mismatch: {symbol}")
        end = _utc(settings["end"]).tz_convert(None)
        frame = frame.loc[:end].copy()
        frames[symbol], hashes[symbol] = frame, actual
    baseline_path = resolve_path(settings["baseline_registration"])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    arm = baseline["arms"][settings.get("baseline_arm", "smart")]
    parameters, engine_options = deepcopy(arm["parameters"]), deepcopy(arm["engine_options"])
    # The new experiment is cash-funded spot; the original smart registration
    # remains immutable and is separately replayed without this adaptation.
    parameters["account"]["mode"] = "spot"
    execution = parameters["execution"]
    if isinstance(execution.get("fee_schedule"), dict):
        execution["fee_schedule"]["market_type"] = "spot"
    engine_options.update(account_mode="spot", timeframe="1d", calculate_benchmarks=False,
                          trading_start=_utc(settings["start"]).tz_convert(None),
                          terminal_policy="forced_liquidation")
    _validate_engine_configuration(parameters, engine_options)
    evidence = {"data_registration": str(registration_path),
                "data_registration_sha256": sha256_file(registration_path),
                "baseline_registration": str(baseline_path),
                "baseline_registration_sha256": sha256_file(baseline_path),
                "input_file_hashes": hashes, "symbols": symbols,
                "membership_basis": "observed_history_only",
                "historical_universe_verified": False,
                "limits": registration.get("limits", []),
                "evaluation_kind": "retrospective", "independent_holdout": False}
    return frames, parameters, engine_options, evidence


def source_identity():
    paths = [ROOT / "backtest/engine.py", ROOT / "core/runtime.py",
             ROOT / "scripts/train_selector.py", ROOT / "scripts/verify_ml_selector_baseline.py",
             ROOT / "config/ml_selection.yaml",
             ROOT / "requirements-ml.txt", ROOT / "requirements.txt", ROOT / "requirements.lock.txt",
             ROOT / "requirements.lock.sha256", ROOT / "pyproject.toml"]
    for folder in ("research/ml_selection", "core", "strategies", "router", "composition", "backtest", "config"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".yaml", ".json"})
    paths.extend(p for p in (ROOT / "data").rglob("*.py"))
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(set(paths)) if p.is_file()}


def freeze_protocol(folder, settings, parameters, engine_options, evidence):
    validate_settings(settings)
    _validate_engine_configuration(parameters, engine_options)
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=False)
    protocol = {"schema": "ml-selection-frozen/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "settings": settings, "parameters": parameters, "engine_options": engine_options,
                "data_evidence": evidence, "source_hashes": source_identity(),
                "live_orders": False, "formal_admission": False,
                "reward_definition": "net_log_equity - positive_drawdown_change_penalty - optional_actual_turnover_penalty",
                "training_label": "next_open_to_fixed_horizon_or_frozen_ATR_stop_proxy",
                "final_holdout_opened": False}
    protocol["protocol_id"] = hashlib.sha256(canonical_json(protocol).encode()).hexdigest()
    save_json(target / "protocol.json", protocol)
    return protocol


def validate_run(folder):
    folder = Path(folder)
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    stored_id = protocol.pop("protocol_id")
    if hashlib.sha256(canonical_json(protocol).encode()).hexdigest() != stored_id:
        raise ValueError("frozen protocol was changed")
    protocol["protocol_id"] = stored_id
    validate_settings(protocol["settings"])
    _validate_engine_configuration(protocol["parameters"], protocol["engine_options"])
    current_sources = source_identity()
    if set(current_sources) != set(protocol["source_hashes"]):
        raise ValueError("source changed since freeze: source file inventory differs; start a new run")
    for relative, digest in protocol["source_hashes"].items():
        if not (ROOT / relative).exists() or sha256_file(ROOT / relative) != digest:
            raise ValueError(f"source changed since freeze: {relative}; start a new run")
    evidence = protocol["data_evidence"]
    declared = {"data_registration", "data_registration_sha256", "baseline_registration",
                "baseline_registration_sha256", "input_file_hashes"}
    # Lightweight test protocols may declare no external data at all. Once any
    # external input is declared, the complete frozen evidence is mandatory.
    if declared.intersection(evidence):
        if not declared <= set(evidence):
            raise ValueError("frozen input evidence is incomplete")
        for name in ("data_registration", "baseline_registration"):
            source = Path(evidence[name])
            if not source.is_file() or sha256_file(source) != evidence[f"{name}_sha256"]:
                raise ValueError(f"frozen registration changed: {name}; start a new run")
        hashes = evidence["input_file_hashes"]
        if set(hashes) != set(evidence["symbols"]):
            raise ValueError("frozen symbol/input inventory differs")
        root = Path(evidence["data_registration"]).parent.resolve()
        for symbol, digest in hashes.items():
            source = (root / "input" / "engine" / (symbol.replace("/", "_") + ".csv")).resolve()
            if not source.is_relative_to(root) or not source.is_file() or sha256_file(source) != digest:
                raise ValueError(f"frozen input hash mismatch: {symbol}; start a new run")
    artifact_path = folder / "artifacts.json"
    if artifact_path.exists():
        manifest = json.loads(artifact_path.read_text(encoding="utf-8"))
        for relative, digest in manifest.items():
            source = (folder / relative).resolve()
            if not source.is_relative_to(folder.resolve()) or not source.is_file() or sha256_file(source) != digest:
                raise ValueError(f"research artifact changed: {relative}")
    return protocol
