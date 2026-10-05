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


def evaluation_contract(settings):
    """Describe data roles without claiming that development data is a holdout."""
    split = settings.get("splits", {})
    calibration_end = split.get("calibration_end")
    options = settings.get("evaluation_protocol", {})
    final = deepcopy(options.get("final_sample", settings.get("next_research", {}).get("final_sample", {})))
    def boundary(value):
        return _utc(value).isoformat() if value is not None else None
    for name in ("start", "end"):
        if name in final:
            final[name] = boundary(final[name])
    return {
        "schema": "ml-selection-evaluation-contract/v1",
        "boundary_status": "declared" if {"train_end", "validation_end"} <= set(split) else "unknown_legacy",
        "train": {"start": boundary(split.get("train_start", settings.get("start"))), "end": boundary(split.get("train_end"))},
        "early_stopping_validation": {"start": boundary(split.get("train_end")), "end": boundary(split.get("validation_end"))},
        "threshold_calibration": {"start": boundary(split.get("validation_end") if calibration_end else split.get("train_end")),
                                  "end": boundary(calibration_end or split.get("validation_end"))},
        "development_test": {"start": boundary(calibration_end or split.get("validation_end")),
                             "end": (_utc(settings["end"]) + pd.Timedelta(days=1)).isoformat() if "end" in settings else None,
                             "independent_final_sample": False},
        "validation_reused_for_thresholds": calibration_end is None,
        "calibration_disjoint_from_early_stopping": calibration_end is not None,
        "calibration_is_final_evidence": False,
        "final_sample": deepcopy(final), "independent_holdout": False,
        "account_contract": {
            "training_account_mode": settings.get("account_mode"),
            "deployment_account_modes": list(options.get("deployment_account_modes", [])),
            "compatibility_verified": bool(options.get("deployment_account_modes")),
            "cross_account_transfer_verified": False,
        },
        "trial_budget": {"scope": "rl_checkpoint_and_threshold_account_evaluations_not_independent_samples",
                         **{key: options.get(key) for key in
                            ("maximum_validation_trials", "maximum_calibration_trials")}},
    }


def evaluation_range(settings, segment):
    split = settings["splits"]
    if segment == "validation":
        return split["train_end"], split["validation_end"]
    if segment == "calibration" and "calibration_end" in split:
        return split["validation_end"], split["calibration_end"]
    if segment == "test":
        return split.get("calibration_end", split["validation_end"]), _utc(settings["end"]) + pd.Timedelta(days=1)
    raise ValueError("only registered validation/calibration/development-test segments supported")


def candidate_training_evidence(settings):
    """Bind the external candidate rows and their original ledger receipts."""
    from research.ml_selection.candidate_dataset import load_candidate_training_rows
    declared = settings["training_data"]
    directory = resolve_path(declared["candidate_dataset_directory"])
    _, receipt = load_candidate_training_rows(directory, target_type=declared["target_type"],
        account_mode=settings["account_mode"], data_identity=declared["data_identity"])
    return {"candidate_directory": str(directory), "receipt": receipt,
            "contract_sha256": sha256_file(directory / "contract.json")}


def validate_settings(settings):
    """Validate both parsed files and caller/CLI overrides before freezing."""
    allowed = {"schema", "timeframe", "account_mode", "evaluation_kind", "data_registration",
               "baseline_registration", "baseline_arm", "start", "end", "splits", "symbols",
               "max_symbols", "models", "model_params", "seed", "selection", "random_seeds",
               "dataset", "rl", "stress", "gates", "walk_forward", "next_research", "evaluation_protocol", "training_data"}
    if not isinstance(settings, dict) or settings.get("schema") != "ml-selection-research/v1":
        raise ValueError("unsupported ML research configuration")
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"unknown research settings: {sorted(unknown)}")
    if settings.get("timeframe") != "1d" or settings.get("account_mode") not in {"spot", "spot_margin"}:
        raise ValueError("ML research protocol requires daily spot or spot_margin trading")
    if settings.get("account_mode") == "spot_margin" and "evaluation_protocol" not in settings:
        raise ValueError("spot_margin research requires an explicit evaluation/account contract")
    if settings.get("evaluation_kind") != "retrospective":
        raise ValueError("this historical runner cannot certify independent final evidence")
    for name in ("data_registration", "baseline_registration", "start", "end", "splits"):
        if name not in settings:
            raise ValueError(f"missing required research setting: {name}")
    split = _section(settings, "splits", {"train_start", "train_end", "validation_end", "calibration_end"})
    if not {"train_end", "validation_end"} <= set(split):
        raise ValueError("splits requires train_end and validation_end")
    start, train = _utc(settings["start"]), _utc(split["train_end"])
    valid, end = _utc(split["validation_end"]), _utc(settings["end"]) + pd.Timedelta(days=1)
    if not start < train < valid < end:
        raise ValueError("research boundaries must be strictly time ordered")
    if not start <= _utc(split.get("train_start", start)) < train:
        raise ValueError("rolling training start must precede training end within the registered data")
    if "calibration_end" in split and not valid < _utc(split["calibration_end"]) < end:
        raise ValueError("independent threshold calibration must lie between validation and development test")
    if "calibration_end" in split and "evaluation_protocol" not in settings:
        raise ValueError("independent calibration requires an explicit evaluation/account/trial-budget contract")
    windows = settings.get("walk_forward", [])
    if not isinstance(windows, list):
        raise ValueError("walk_forward must be a list")
    for window in windows:
        if (not isinstance(window, dict) or not {"train_end", "validation_end", "test_end"} <= set(window)
                or set(window) - {"train_start", "train_end", "validation_end", "calibration_end", "test_end"}):
            raise ValueError("walk_forward windows require train_end/validation_end/test_end")
        if not start < _utc(window["train_end"]) < _utc(window["validation_end"]) < _utc(window["test_end"]) <= end:
            raise ValueError("walk_forward boundaries must be ordered within the frozen data period")
        if not start <= _utc(window.get("train_start", start)) < _utc(window["train_end"]):
            raise ValueError("walk-forward training start is outside the registered period")
        if "calibration_end" in window and not _utc(window["validation_end"]) < _utc(window["calibration_end"]) < _utc(window["test_end"]):
            raise ValueError("walk-forward calibration boundaries must be strictly ordered")
        if ("calibration_end" in split) != ("calibration_end" in window):
            raise ValueError("every walk-forward window must retain the declared calibration data role")
    if settings.get("max_symbols") is not None:
        _integer(settings["max_symbols"], "max_symbols")
    if settings.get("symbols") is not None:
        symbols = settings["symbols"]
        if not isinstance(symbols, list) or not symbols or any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be a nonempty list of distinct names")
    models = settings.get("models", ["ridge", "lightgbm"])
    if not isinstance(models, list) or not models or len(set(models)) != len(models) or any(m not in {"ridge", "lightgbm", "lambdarank"} for m in models):
        raise ValueError("models must be distinct supported names")
    model_params = _section(settings, "model_params", {"ridge", "lightgbm", "lambdarank"})
    if any(not isinstance(value, dict) for value in model_params.values()):
        raise ValueError("model_params values must be mappings")
    _integer(settings.get("seed", 42), "seed", 0)
    seeds = settings.get("random_seeds", [42, 43, 44])
    if not isinstance(seeds, list) or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("random_seeds must be a nonempty list of distinct integers")
    for seed in seeds:
        _integer(seed, "random seed", 0)
    rl = _section(settings, "rl", {"enabled", "episodes", "seed", "seeds", "learning_rate", "entropy_coef",
                                  "gamma", "drawdown_penalty", "turnover_penalty", "validation_patience",
                                  "min_updates", "early_stopping_start_updates", "evaluation_threshold",
                                  "evaluation_thresholds", "context_features"})
    if "context_features" in rl:
        from research.ml_selection.policy_context import POLICY_CONTEXT_FEATURES
        context_features = rl["context_features"]
        if (not isinstance(context_features, list) or any(not isinstance(name, str) for name in context_features)
                or len(set(context_features)) != len(context_features)
                or set(context_features) - set(POLICY_CONTEXT_FEATURES)):
            raise ValueError("rl.context_features must be a distinct list of supported actual-candidate features")
    policy_seeds = rl.get("seeds", [rl.get("seed", 42)])
    if not isinstance(policy_seeds, list) or not policy_seeds or len(set(policy_seeds)) != len(policy_seeds):
        raise ValueError("rl.seeds must be distinct seed integers")
    for seed in policy_seeds:
        _integer(seed, "RL seed", 0)
    dataset = _section(settings, "dataset", {"horizon_bars", "min_history", "min_quote_volume",
                       "commission_rate", "slippage_bps", "stop_atr_multiple", "membership",
                       "require_exchange_quote_volume", "require_verified_membership"})
    for name in ("require_exchange_quote_volume", "require_verified_membership"):
        if name in dataset and type(dataset[name]) is not bool:
            raise ValueError(f"dataset.{name} must be boolean")
    if "training_data" in settings:
        training_data = _section(settings, "training_data", {"candidate_dataset_directory", "target_type", "data_identity"})
        if set(training_data) != {"candidate_dataset_directory", "target_type", "data_identity"}:
            raise ValueError("candidate training data requires registered directory, target type and data identity")
        if training_data["target_type"] not in {"actual_exit", "proxy"}:
            raise ValueError("candidate training target must be actual_exit or proxy; unmeasured portfolio marginal targets are unsupported")
        if any(not isinstance(training_data[key], str) or not training_data[key].strip()
               for key in ("candidate_dataset_directory", "data_identity")):
            raise ValueError("candidate training directory and data identity must be nonempty strings")
    for name, minimum in (("horizon_bars", 1), ("min_history", 61)):
        if name in dataset:
            _integer(dataset[name], f"dataset.{name}", minimum)
    for name in ("min_quote_volume", "commission_rate", "slippage_bps", "stop_atr_multiple"):
        if name in dataset:
            _number(dataset[name], f"dataset.{name}", minimum=0, strict_minimum=name == "stop_atr_multiple")
    if dataset.get("commission_rate", .001) >= 1 or dataset.get("slippage_bps", 5) >= 10000:
        raise ValueError("dataset cost assumptions must be below 100%")
    selection = _section(settings, "selection", {"min_expected_return", "score_scale", "score_cap", "policy_gate_mode"})
    if selection.get("policy_gate_mode", "policy_only") not in {"policy_only", "policy_and_return"}:
        raise ValueError("selection.policy_gate_mode must be policy_only or policy_and_return")
    for name, value in selection.items():
        if name == "policy_gate_mode":
            continue
        _number(value, f"selection.{name}", minimum=None if name == "min_expected_return" else 0,
                strict_minimum=name != "min_expected_return")
    if "enabled" in rl and type(rl["enabled"]) is not bool:
        raise ValueError("rl.enabled must be boolean")
    for name in ("episodes", "validation_patience", "seed", "min_updates", "early_stopping_start_updates"):
        if name in rl:
            _integer(rl[name], f"rl.{name}", 0 if name in {"seed", "min_updates", "early_stopping_start_updates"} else 1)
    if max(rl.get("min_updates", 0), rl.get("early_stopping_start_updates", 0)) > rl.get("episodes", 6):
        raise ValueError("RL update minimum and early-stop start must fit the episode budget")
    _number(rl.get("evaluation_threshold", .5), "rl.evaluation_threshold", minimum=0, maximum=1)
    thresholds = rl.get("evaluation_thresholds", [rl.get("evaluation_threshold", .5)])
    if not isinstance(thresholds, list) or not thresholds or len(set(thresholds)) != len(thresholds):
        raise ValueError("rl.evaluation_thresholds must be a nonempty distinct list")
    for threshold in thresholds:
        _number(threshold, "RL evaluation threshold", minimum=0, maximum=1)
    for name in ("learning_rate", "entropy_coef", "drawdown_penalty", "turnover_penalty"):
        if name in rl:
            _number(rl[name], f"rl.{name}", minimum=0, strict_minimum=name == "learning_rate")
    if "gamma" in rl:
        _number(rl["gamma"], "rl.gamma", minimum=0, maximum=1)
    stress = _section(settings, "stress", {"cost_multipliers", "execution_scenarios"})
    multipliers = stress.get("cost_multipliers", [1.5, 2.0])
    if not isinstance(multipliers, list) or not multipliers:
        raise ValueError("stress.cost_multipliers must be a nonempty list")
    for value in multipliers:
        _number(value, "stress cost multiplier", minimum=1)
    if len(set(multipliers)) != len(multipliers):
        raise ValueError("stress cost multipliers must be distinct")
    scenarios = stress.get("execution_scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError("stress.execution_scenarios must be a list")
    names = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) - {"name", "opening_delay_bars", "max_participation_rate", "opening_order_ttl_bars", "initial_capital"}:
            raise ValueError("unsupported execution scenario")
        name = scenario.get("name")
        if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in names:
            raise ValueError("execution scenario names must be distinct safe names")
        names.add(name)
        for key in ("opening_delay_bars", "opening_order_ttl_bars"):
            if key in scenario:
                _integer(scenario[key], f"stress.{key}", 0)
        if "max_participation_rate" in scenario:
            _number(scenario["max_participation_rate"], "stress.max_participation_rate", minimum=0, maximum=1, strict_minimum=True)
        if "initial_capital" in scenario:
            _number(scenario["initial_capital"], "stress.initial_capital", minimum=0, strict_minimum=True)
    extension = _section(settings, "next_research", {"learning_months", "exit_probe_count", "rl_windows",
        "reward_pilot_weights", "pilot_episodes", "final_sample", "forward_observation", "experiment_budget"})
    months = extension.get("learning_months", [12, 24, 36])
    if not isinstance(months, list) or not months or len(set(months)) != len(months):
        raise ValueError("learning_months must be distinct positive months")
    for month in months:
        _integer(month, "learning month")
    for name in ("exit_probe_count", "pilot_episodes"):
        if name in extension:
            _integer(extension[name], f"next_research.{name}")
    rl_windows = extension.get("rl_windows", [])
    if not isinstance(rl_windows, list) or len(set(rl_windows)) != len(rl_windows):
        raise ValueError("rl_windows must be distinct walk-forward indices")
    for number in rl_windows:
        _integer(number, "RL window")
        if number > len(windows):
            raise ValueError("RL window outside registered walk-forward windows")
    weights = extension.get("reward_pilot_weights", [])
    if not isinstance(weights, list) or len(set(weights)) != len(weights):
        raise ValueError("reward pilot weights must be distinct")
    for value in weights:
        _number(value, "reward pilot weight", minimum=0)
    for name in ("final_sample", "forward_observation", "experiment_budget"):
        if name in extension and not isinstance(extension[name], dict):
            raise ValueError(f"next_research.{name} must be a mapping")
    final = extension.get("final_sample", {})
    if final:
        if not {"start", "end", "opened", "maximum_frozen_candidates"} <= set(final):
            raise ValueError("final sample requires unopened dates and candidate budget")
        if final["opened"] is not False or not end <= _utc(final["start"]) < _utc(final["end"]):
            raise ValueError("final sample must be unopened and beyond historical development data")
        _integer(final["maximum_frozen_candidates"], "final candidate budget")
    forward = extension.get("forward_observation", {})
    if forward:
        if not {"start", "minimum_decision_dates", "maturity_horizon_bars", "frozen_candidate_count"} <= set(forward):
            raise ValueError("forward observation requires registered dates and maturity budget")
        if _utc(forward["start"]) < end:
            raise ValueError("forward observation must follow historical development data")
        for key in ("minimum_decision_dates", "maturity_horizon_bars", "frozen_candidate_count"):
            _integer(forward[key], f"forward {key}")
    budget = extension.get("experiment_budget", {})
    minimum_total = rl.get("min_updates", 0) * len(policy_seeds) * (1 + len(rl_windows))
    maximum_total = rl.get("episodes", 6) * len(policy_seeds) * (1 + len(rl_windows))
    for key, actual in (("minimum_formal_updates", minimum_total), ("maximum_formal_episodes", maximum_total)):
        if key in budget and budget[key] != actual:
            raise ValueError(f"registered {key} disagrees with seed/window training controls")
    evaluation = _section(settings, "evaluation_protocol", {"deployment_account_modes", "maximum_validation_trials",
        "maximum_calibration_trials", "minimum_event_groups", "final_sample"})
    if "evaluation_protocol" in settings:
        deployment_modes = evaluation.get("deployment_account_modes")
        if (not isinstance(deployment_modes, list) or deployment_modes != [settings["account_mode"]]):
            raise ValueError("deployment_account_modes must explicitly match the training account; cross-account transfer is unverified")
        for name in ("maximum_validation_trials", "maximum_calibration_trials", "minimum_event_groups"):
            if name in evaluation:
                _integer(evaluation[name], f"evaluation_protocol.{name}", 0 if name.startswith("maximum") else 1)
        planned_validation = maximum_total * (1 if "calibration_end" in split else len(thresholds)) if rl.get("enabled", True) else 0
        planned_calibration = len(thresholds) * (1 + len(rl_windows)) if rl.get("enabled", True) and "calibration_end" in split else 0
        for name, planned in (("maximum_validation_trials", planned_validation), ("maximum_calibration_trials", planned_calibration)):
            if planned and (name not in evaluation or evaluation[name] < planned):
                raise ValueError(f"{name} must cover the registered RL seed/checkpoint/window/threshold attempts ({planned})")
        explicit_final = evaluation.get("final_sample", {})
        if explicit_final:
            if (not isinstance(explicit_final, dict) or not {"start", "end", "opened", "maximum_frozen_candidates"} <= set(explicit_final)
                    or explicit_final["opened"] is not False
                    or not end <= _utc(explicit_final["start"]) < _utc(explicit_final["end"])):
                raise ValueError("evaluation final sample must be unopened and beyond all development data")
            _integer(explicit_final["maximum_frozen_candidates"], "final candidate budget")
            if final and explicit_final != final:
                raise ValueError("evaluation and next-research final sample declarations disagree")
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
    # The original registration remains immutable. The research account must
    # match its explicit protocol rather than silently becoming cash spot.
    parameters["account"]["mode"] = settings["account_mode"]
    parameters.setdefault("research", {})["entry_audit"] = True
    execution = parameters["execution"]
    if isinstance(execution.get("fee_schedule"), dict):
        execution["fee_schedule"]["market_type"] = "spot"
    engine_options.update(account_mode=settings["account_mode"], timeframe="1d", calculate_benchmarks=False,
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
             ROOT / "scripts/train_selector.py", ROOT / "scripts/run_ml_selection_next.py", ROOT / "scripts/verify_ml_selector_baseline.py",
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
    if "evaluation_protocol" in settings:
        contract = evaluation_contract(settings)
        final = contract["final_sample"]
        if final and _utc(datetime.now(timezone.utc)) >= _utc(final["start"]):
            raise ValueError("a new final sample must be registered before its real start; choose a new unopened future period")
        for actual in (parameters.get("account", {}).get("mode"), engine_options.get("account_mode")):
            if actual != settings["account_mode"]:
                raise ValueError("research engine account does not match the evaluation/account contract")
    training_evidence = candidate_training_evidence(settings) if "training_data" in settings else None
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=False)
    protocol = {"schema": "ml-selection-frozen/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "settings": settings, "parameters": parameters, "engine_options": engine_options,
                "data_evidence": evidence, "source_hashes": source_identity(),
                "live_orders": False, "formal_admission": False,
                "reward_definition": "net_log_equity - positive_drawdown_change_penalty - optional_actual_turnover_penalty",
                "training_label": "next_open_to_fixed_horizon_or_frozen_ATR_stop_proxy",
                "final_holdout_opened": False}
    if "evaluation_protocol" in settings:
        protocol["evaluation_contract"] = evaluation_contract(settings)
    if "training_data" in settings:
        protocol["training_label"] = "registered_original_candidate_" + settings["training_data"]["target_type"] + "_not_portfolio_marginal"
        protocol["training_data_evidence"] = training_evidence
    protocol["protocol_id"] = hashlib.sha256(canonical_json(protocol).encode()).hexdigest()
    save_json(target / "protocol.json", protocol)
    # Preserve the exact registered sources as well as their identities. A
    # later checkout must not destroy the ability to inspect an old run.
    for relative, digest in protocol["source_hashes"].items():
        source = (ROOT / relative).resolve()
        archived = (target / "frozen_source" / relative).resolve()
        if not source.is_relative_to(ROOT.resolve()) or not archived.is_relative_to((target / "frozen_source").resolve()):
            raise ValueError("registered source path escapes its root")
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(source.read_bytes())
        if sha256_file(archived) != digest:
            raise ValueError(f"source changed during freeze: {relative}")
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
    if "training_data" in protocol["settings"]:
        if protocol.get("training_data_evidence") != candidate_training_evidence(protocol["settings"]):
            raise ValueError("candidate training evidence changed since freeze; start a new run")
    current_sources = source_identity()
    if set(current_sources) != set(protocol["source_hashes"]):
        raise ValueError("source changed since freeze: source file inventory differs; start a new run")
    for relative, digest in protocol["source_hashes"].items():
        if not (ROOT / relative).exists() or sha256_file(ROOT / relative) != digest:
            raise ValueError(f"source changed since freeze: {relative}; start a new run")
    archive = folder / "frozen_source"
    if archive.exists():
        for relative, digest in protocol["source_hashes"].items():
            archived = (archive / relative).resolve()
            if not archived.is_relative_to(archive.resolve()) or not archived.is_file() or sha256_file(archived) != digest:
                raise ValueError(f"frozen source archive changed: {relative}")
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
