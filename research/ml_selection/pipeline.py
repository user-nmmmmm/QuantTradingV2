"""Reproducible CPU training, original-engine evaluation and shadow logging."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import logging
from pathlib import Path
import time

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, deterministic_result_digest, sha256_file, sha256_frame
from research.ml_selection.dataset import (FEATURE_COLUMNS, build_dataset, chronological_split,
                                          forward_proxy_outcome)
from research.ml_selection.environment import FullEngineEnvironment, discounted_returns
from research.ml_selection.models import BernoulliPolicy, fit_model, load_model, ranking_metrics, calibration_metrics
from research.ml_selection.protocol import (ROOT, freeze_protocol, load_inputs, save_json,
                                            validate_run)
from research.ml_selection.selector import ACCOUNT_FEATURES, ResearchSelector, utc


def progress(folder, stage, message, **details):
    row = {"stage": stage, "message": message, "updated_at": datetime.now(timezone.utc).isoformat(), **details}
    save_json(Path(folder) / "progress.json", row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def load_dataset(folder):
    rows = pd.read_csv(Path(folder) / "dataset.csv", parse_dates=["as_of", "bar_time", "label_available_at"], float_precision="round_trip")
    rows["as_of"] = pd.to_datetime(rows.as_of, utc=True)
    rows["label_available_at"] = pd.to_datetime(rows.label_available_at, utc=True)
    if rows.eligible.dtype != bool:
        raise ValueError("dataset eligibility must be boolean")
    return rows


def splits(dataset, settings):
    return chronological_split(dataset, **settings["splits"],
                               test_end=utc(settings["end"]) + pd.Timedelta(days=1))


def prepare(settings, folder):
    frames, parameters, options, evidence = load_inputs(settings)
    protocol = freeze_protocol(folder, settings, parameters, options, evidence)
    progress(folder, "R1-R2", "正在生成因果特征和成熟标签", symbols=len(frames))
    started = time.monotonic()
    dataset = build_dataset(frames, **settings.get("dataset", {}))
    dataset = dataset.loc[dataset.as_of >= utc(settings["start"])].reset_index(drop=True)
    membership_report = None
    if settings.get("dataset", {}).get("membership") is not None:
        from research.ml_selection.membership import audit_membership
        membership_rows, membership_report = audit_membership(settings["dataset"]["membership"],
                                                              dataset[["symbol", "as_of"]])
        rejected = ~membership_rows.membership_eligible
        dataset.loc[rejected, "eligible"] = False
        dataset.loc[rejected, "exclusion_reason"] = membership_rows.loc[rejected, "membership_reason"].to_numpy()
        dataset["membership_basis"] = membership_rows.membership_basis.to_numpy()
    dataset.to_csv(Path(folder) / "dataset.csv", index=False)
    partition = splits(dataset, settings)
    statistics = {"rows": len(dataset), "eligible_rows": int(dataset.eligible.sum()),
                  "features": list(FEATURE_COLUMNS), "symbols": len(frames),
                  "split_rows": {name: len(rows) for name, rows in partition.items()},
                  "exclusions": dataset.exclusion_reason.fillna("eligible").value_counts().to_dict(),
                  "seconds": time.monotonic() - started,
                  "labels_are_independent_proxy_trades": True,
                  "membership_basis": "provided_source_intervals_partial_coverage" if membership_report else "observed_history_only",
                  "membership_evidence": membership_report,
                  "independent_holdout": False}
    if any(len(partition[name]) == 0 for name in ("train", "validation", "test")):
        raise ValueError("one or more mature chronological partitions are empty")
    save_json(Path(folder) / "dataset_summary.json", statistics)
    progress(folder, "R2", "训练数据准备完成", **statistics)
    return protocol, frames, dataset


def supervised(folder, protocol, dataset):
    settings = protocol["settings"]
    partition = splits(dataset, settings)
    diagnostics, models = {}, {}
    for kind in settings.get("models", ["ridge", "lightgbm"]):
        progress(folder, "R3", "正在训练评分模型", model=kind)
        path = Path(folder) / "models" / f"{kind}.json"
        if path.exists():
            model = load_model(path)
        else:
            model = fit_model(partition["train"], partition["validation"], features=FEATURE_COLUMNS,
                              kind=kind, seed=settings.get("seed", 42),
                              params=settings.get("model_params", {}).get(kind, {}))
            model.save(path)
        models[kind] = model
        diagnostic_rows = dataset.loc[dataset.eligible &
            (dataset.as_of >= utc(settings["splits"]["train_end"])) &
            (dataset.as_of < utc(settings["splits"]["validation_end"]))]
        diagnostics[kind] = {"model_id": model.model_id, "metadata": model.metadata,
                             "validation": ranking_metrics(diagnostic_rows,
                                 model.predict(diagnostic_rows),
                                 mature_as_of=utc(settings["splits"]["validation_end"])),
                             "return_calibration": calibration_metrics(diagnostic_rows,
                                 getattr(model, "predict_net_return", model.predict)(diagnostic_rows),
                                 mature_as_of=utc(settings["splits"]["validation_end"]))}
    save_json(Path(folder) / "supervised_validation.json", diagnostics)
    return models


def slice_frames(frames, start, end, warmup=130):
    start, end = utc(start).tz_convert(None), utc(end).tz_convert(None)
    result = {}
    for symbol, frame in frames.items():
        before = frame.loc[frame.index < start].tail(warmup)
        active = frame.loc[(frame.index >= start) & (frame.index < end)]
        if len(active):
            result[symbol] = pd.concat([before, active]).copy()
    if not result:
        raise ValueError("no market bars in evaluation window")
    return result


def make_environment(frames, protocol, *, start, end, multiplier=1.0, execution_scenario=None):
    options, parameters = deepcopy(protocol["engine_options"]), deepcopy(protocol["parameters"])
    options["trading_start"] = utc(start).tz_convert(None)
    options["run_id"] = "ml-selection-offline"
    parameters.setdefault("research", {})["entry_audit"] = True
    scenario = dict(execution_scenario or {})
    opening_delay = scenario.get("opening_delay_bars", 0)
    if "initial_capital" in scenario:
        options["initial_capital"] = scenario["initial_capital"]
    for key in ("max_participation_rate", "opening_order_ttl_bars"):
        if key in scenario:
            parameters["execution"][key] = scenario[key]
    for name in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps",
                 "volatility_slippage_factor", "impact_coefficient"):
        parameters["execution"][name] *= multiplier
    # Broker may prefer a fee schedule to flat commission fields.
    schedule = parameters["execution"].get("fee_schedule")
    if isinstance(schedule, dict):
        for name in ("maker_rate", "taker_rate", "maker", "taker"):
            if isinstance(schedule.get(name), (int, float)):
                schedule[name] *= multiplier
    reward = protocol["settings"].get("rl", {})
    return FullEngineEnvironment(slice_frames(frames, start, end), engine_options=options,
                                 parameters=parameters,
                                 drawdown_penalty=reward.get("drawdown_penalty", .5),
                                 turnover_penalty=reward.get("turnover_penalty", 0.0),
                                 opening_delay_bars=opening_delay)


def persist_episode(folder, name, episode, selector=None):
    target = Path(folder) / "episodes" / name
    if target.exists():
        attempt = 1
        while target.with_name(name + f"_attempt_{attempt}").exists():
            attempt += 1
        target = target.with_name(name + f"_attempt_{attempt}")
    target.mkdir(parents=True, exist_ok=False)
    episode.result["equity_curve"].to_csv(target / "equity.csv", index_label="bar_time")
    trades = pd.DataFrame(episode.result["trades"])
    if trades.empty:
        trades = pd.DataFrame(columns=["symbol", "side", "qty", "fill_price", "fill_time", "commission"])
    trades.to_csv(target / "trades.csv", index=False)
    from backtest.reporting.trades import TradeReconstructionMixin
    reconstruction = TradeReconstructionMixin()
    positions = reconstruction._aggregate_round_trips(reconstruction._reconstruct_closed_trades(trades))
    pd.DataFrame(positions, columns=None if positions else ["position_id", "entry_time", "net_pnl"]).to_csv(
        target / "closed_positions.csv", index=False)
    episode.rewards.to_csv(target / "rewards.csv", index_label="bar_time")
    pd.DataFrame(episode.result.get("allocation_audit", [])).to_csv(target / "allocation.csv", index=False)
    if selector is not None:
        pd.DataFrame(selector.audit).to_csv(target / "selection.csv", index=False)
    for key in ("entry_observations", "execution_audit", "capital_allocation_audit",
                "strategy_activity", "strategy_health", "strategy_health_transitions",
                "lifecycle", "terminal_valuation", "accounting_check", "breaker_state"):
        save_json(target / f"{key}.json", episode.result.get(key))
    from research.ml_selection.diagnostics import build_episode_diagnostics
    reward_weights = getattr(episode, "reward_weights", None) or {}
    diagnostics = build_episode_diagnostics(episode.result, episode.rewards,
        selection_audit=selector.audit if selector is not None else (),
        initial_capital=episode.summary["initial_capital"],
        drawdown_penalty=reward_weights.get("drawdown_penalty", .5),
        turnover_penalty=reward_weights.get("turnover_penalty", 0.0))
    for key in ("decision_ledger", "fill_ledger"):
        pd.DataFrame(diagnostics[key]).to_csv(target / f"{key}.csv", index=False)
    save_json(target / "diagnostics.json", {key: value for key, value in diagnostics.items()
                                           if key not in {"decision_ledger", "fill_ledger"}})
    summary = {**episode.summary, "reward_sum": float(episode.rewards.reward.sum()),
               "reward_weights": reward_weights,
               "artifact_directory": str(target.relative_to(Path(folder))),
               "decision_count": len(selector.audit) if selector is not None else None,
               "selected_count": sum(bool(row.get("selected")) for row in selector.audit) if selector is not None else None,
               "digest": deterministic_result_digest(episode.result),
               "candidate_funnel": diagnostics["funnel"],
               "reward_reconciliation": diagnostics["reward_reconciliation"]}
    save_json(target / "summary.json", summary)
    return summary


def selection(dataset, settings, kind, models, *, policy=None, seed=42, deterministic=True):
    options = settings.get("selection", {})
    if kind == "native":
        return None
    if kind in {"qualified_native", "momentum", "random"}:
        return ResearchSelector(dataset, mode=kind, seed=seed, **options)
    if kind == "rl":
        return ResearchSelector(dataset, mode="policy", model=models["primary"], policy=policy,
                                deterministic=deterministic, seed=seed, **options)
    return ResearchSelector(dataset, mode="model", model=models[kind], **options)


def matrix(folder, protocol, frames, dataset, models, *, segment, policy=None):
    settings = protocol["settings"]
    if segment == "validation":
        start, end = settings["splits"]["train_end"], settings["splits"]["validation_end"]
    elif segment == "test":
        start, end = settings["splits"]["validation_end"], utc(settings["end"]) + pd.Timedelta(days=1)
    else:
        raise ValueError("only predeclared validation/test segments supported")
    for kind, model in models.items():
        if kind == "primary":
            continue
        fitted_through = utc(model.metadata["train_latest_label_available_at"])
        if fitted_through >= utc(start):
            raise ValueError("evaluation starts before training labels matured")
    arms = [("native", 42), ("qualified_native", 42), ("momentum", 42)]
    arms.extend(("random", seed) for seed in settings.get("random_seeds", [42, 43, 44]))
    arms.extend((kind, 42) for kind in models if kind != "primary")
    if policy is not None:
        arms.append(("rl", 42))
    results = {}
    for kind, seed in arms:
        name = f"{segment}_{kind}" + (f"_{seed}" if kind == "random" else "")
        cached = Path(folder) / "episodes" / name / "summary.json"
        if cached.exists():
            results[kind if kind != "random" else f"random_{seed}"] = json.loads(cached.read_text(encoding="utf-8"))
            continue
        progress(folder, "R4" if segment == "validation" else "R7", "正在运行组合对照", arm=name)
        selector = selection(dataset, settings, kind, models, policy=policy, seed=seed)
        episode = make_environment(frames, protocol, start=start, end=end).run_episode(selector)
        results[kind if kind != "random" else f"random_{seed}"] = persist_episode(folder, name, episode, selector)
    save_json(Path(folder) / f"{segment}_comparison.json", results)
    return results


def _rl_budget_options(options):
    """Parse the completed-update budget, independently of episode attempts."""
    limits = {}
    for name, default in (("episodes", 6), ("min_updates", 0),
                          ("early_stopping_start_updates", 0), ("validation_patience", 3)):
        value = options.get(name, default)
        minimum = 1 if name in {"episodes", "validation_patience"} else 0
        if type(value) is not int or value < minimum:
            raise ValueError(f"rl.{name} must be an integer >= {minimum}")
        limits[name] = value
    if limits["episodes"] < limits["min_updates"]:
        raise ValueError("rl.episodes must be >= rl.min_updates; empty episodes are not updates")
    default = options.get("evaluation_threshold", .5)
    thresholds = options.get("evaluation_thresholds", [default])
    if not isinstance(thresholds, (list, tuple)) or not thresholds:
        raise ValueError("rl.evaluation_thresholds must be a nonempty preregistered list")
    values = [default, *thresholds]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not np.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise ValueError("RL evaluation thresholds must be finite and between zero and one")
    thresholds = [float(value) for value in thresholds]
    if len(set(thresholds)) != len(thresholds) or float(default) not in thresholds:
        raise ValueError("RL evaluation thresholds must be distinct and include evaluation_threshold")
    return {**limits, "evaluation_threshold": float(default), "evaluation_thresholds": thresholds}


def _rl_process_memory():
    """Measured process peak resident memory; no dependency or estimated RAM."""
    import os
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            *[(name, ctypes.c_size_t) for name in
                              ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                               "PagefileUsage", "PeakPagefileUsage")]]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error())
            return {"peak_ram_bytes": int(counters.PeakWorkingSetSize),
                    "current_ram_bytes": int(counters.WorkingSetSize),
                    "ram_measurement": "windows_process_working_set", "ram_peak_scope": "process_lifetime"}
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"peak_ram_bytes": int(peak * (1 if sys.platform == "darwin" else 1024)),
                "current_ram_bytes": None, "ram_measurement": "getrusage_maxrss",
                "ram_peak_scope": "process_lifetime"}
    except (ImportError, OSError, AttributeError):
        return {"peak_ram_bytes": None, "current_ram_bytes": None,
                "ram_measurement": "unavailable", "ram_peak_scope": "process_lifetime"}


def _rl_probability_diagnostics(policy, trajectory, thresholds):
    if not trajectory:
        return {"count": 0, "min": None, "median": None, "max": None, "mean_entropy": None,
                "threshold_counts": {str(value): {"accepted": 0, "rejected": 0} for value in thresholds}}
    values = policy.predict(pd.DataFrame([row["features"] for row in trajectory]))
    return {"count": len(values), "min": float(values.min()), "median": float(np.median(values)),
            "max": float(values.max()), "mean": float(values.mean()),
            "mean_entropy": float(np.mean(-(values * np.log(values) + (1 - values) * np.log1p(-values)))),
            "threshold_counts": {str(value): {"accepted": int((values >= value).sum()),
                                                 "rejected": int((values < value).sum())} for value in thresholds}}


def _rl_budget_receipt(folder, history, policy, limits, stale, *, wall_seconds):
    actual = int(policy.update_count)
    gate = max(limits["min_updates"], limits["early_stopping_start_updates"])
    exhausted = len(history) >= limits["episodes"]
    no_candidates = sum(int(row.get("trajectory_actions", 0)) for row in history) == 0
    stopped = actual >= gate and stale >= limits["validation_patience"]
    reason = ("no_actionable_candidates" if exhausted and no_candidates else
              "episode_limit_reached" if exhausted else "validation_early_stop" if stopped else "in_progress")
    receipt = {"actual_updates": actual, "min_updates": limits["min_updates"],
               "minimum_budget_met": actual >= limits["min_updates"],
               "budget_status": "met" if actual >= limits["min_updates"] else "insufficient_updates",
               "stop_reason": reason, "episodes_completed": len(history), "episode_limit": limits["episodes"],
               "no_action_episode_count": sum(not row.get("trajectory_actions", 0) for row in history),
               "early_stopping_start_updates": limits["early_stopping_start_updates"],
               "early_stopping_effective_start_updates": gate, "early_stopping_eligible": actual >= gate,
               "stale_validation_count": stale,
               "completed_checkpoint": f"models/policy_{len(history):03d}.json" if history else None,
               "completed_checkpoint_update_count": actual,
               "evaluation_thresholds": limits["evaluation_thresholds"],
               "selection_segment": "validation", "test_used_for_selection": False,
               "cash_is_legal": True, "forced_trade_reward": False,
               "updates_are_replayed_parameter_steps_not_independent_market_samples": True,
               "invocation_wall_seconds": wall_seconds, **_rl_process_memory()}
    save_json(Path(folder) / "rl_budget_receipt.json", receipt)
    save_json(Path(folder) / "rl_resources.json", {
        "episodes": [row.get("resources", {"episode": row["episode"], "measurement": "legacy_not_recorded"})
                     for row in history], "invocation_wall_seconds": wall_seconds, **_rl_process_memory()})
    return receipt


def train_policy(folder, protocol, frames, dataset, models):
    settings = protocol["settings"]
    options = settings.get("rl", {})
    if not options.get("enabled", True):
        return None
    started = time.monotonic()
    limits = _rl_budget_options(options)
    episodes = limits["episodes"]
    partition = splits(dataset, settings)
    training = partition["train"].copy()
    defaults = {"cash_fraction": 1., "gross_exposure_fraction": 0., "portfolio_drawdown": 0.,
                "held_count_fraction": 0., "pending_count_fraction": 0.}
    for name, value in defaults.items():
        training[name] = value
    policy = BernoulliPolicy((*FEATURE_COLUMNS, *ACCOUNT_FEATURES), seed=options.get("seed", 42))
    policy.fit_scaler(training)
    policy.metadata.update(algorithm="episodic_bernoulli_REINFORCE", research_only=True,
                           train_end=settings["splits"]["train_end"],
                           validation_end=settings["splits"]["validation_end"],
                           parent_model_id=models["primary"].model_id,
                           account_feature_scaling="fixed_neutral_state_unit_scale",
                           evaluation_threshold=limits["evaluation_threshold"],
                           preregistered_evaluation_thresholds=limits["evaluation_thresholds"],
                           threshold_selected_using="validation_only", cash_is_legal=True)
    best_value, best_path, stale, history, baseline = -np.inf, None, 0, [], 0.0
    history_path = Path(folder) / "rl_training.json"
    latest_path = Path(folder) / "models/policy_latest.json"
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history, list) or not history or len(history) > episodes:
            raise ValueError("completed RL history must be nonempty and within the frozen budget")
        committed = []
        previous_updates = 0
        for number, row in enumerate(history, 1):
            if not isinstance(row, dict) or type(row.get("episode")) is not int or row["episode"] != number:
                raise ValueError("completed RL episode numbers must be contiguous from one")
            checkpoint = Path(folder) / "models" / f"policy_{number:03d}.json"
            if not checkpoint.is_file():
                raise ValueError(f"committed RL checkpoint is missing: {checkpoint.name}")
            saved = BernoulliPolicy.load(checkpoint)
            if saved.metadata.get("parent_model_id") != models["primary"].model_id:
                raise ValueError("policy parent model changed")
            if "checkpoint_model_id" in row and row["checkpoint_model_id"] != saved.model_id:
                raise ValueError(f"committed RL checkpoint identity mismatch: {checkpoint.name}")
            actions = row.get("trajectory_actions", 0)
            if type(actions) is not int or actions < 0:
                raise ValueError("committed RL trajectory count must be a nonnegative integer")
            expected = previous_updates + int(bool(actions))
            if saved.update_count != expected:
                raise ValueError(f"committed RL checkpoint update count mismatch: {checkpoint.name}")
            for field in ("actual_updates", "checkpoint_update_count"):
                if field in row and (type(row[field]) is not int or row[field] != saved.update_count):
                    raise ValueError(f"committed RL history update count mismatch: {checkpoint.name}")
            reported = row.get("update", {}).get("update_count", saved.update_count)
            if type(reported) is not int or reported != saved.update_count:
                raise ValueError(f"committed RL update diagnostics count mismatch: {checkpoint.name}")
            registered = saved.metadata.get("preregistered_evaluation_thresholds")
            if registered is not None and registered != limits["evaluation_thresholds"]:
                raise ValueError("policy evaluation threshold registration changed")
            if registered is None and limits["evaluation_thresholds"] != [.5]:
                raise ValueError("legacy policy only supports its frozen 0.5 evaluation threshold")
            previous_updates = saved.update_count
            if not np.isfinite(float(row["baseline"])) or not np.isfinite(float(row["validation"]["reward_sum"])):
                raise ValueError("committed RL baseline and validation reward must be finite")
            committed.append(saved)
        # History is the completed watermark. Convenience aliases may be ahead
        # after cancellation between their writes and the history commit.
        policy = committed[-1]
        baseline = float(history[-1]["baseline"])
        best_index = max(range(len(history)), key=lambda i: float(history[i]["validation"]["reward_sum"]))
        best_value = float(history[best_index]["validation"]["reward_sum"])
        best_path = Path(folder) / "models/policy_best.json"
        committed[best_index].save(best_path)
        policy.save(latest_path)
        stale = len(history) - 1 - best_index
    for number in range(len(history), episodes):
        if (policy.update_count >= max(limits["min_updates"], limits["early_stopping_start_updates"])
                and stale >= limits["validation_patience"]):
            break
        episode_started = time.monotonic()
        progress(folder, "R6", "正在运行奖励驱动训练", episode=number + 1, maximum=episodes,
                 actual_updates=policy.update_count, min_updates=limits["min_updates"])
        selector = selection(dataset, settings, "rl", models, policy=policy, deterministic=False)
        episode = make_environment(frames, protocol, start=settings["start"],
                                   end=settings["splits"]["train_end"]).run_episode(selector)
        training_summary = persist_episode(folder, f"rl_train_{number+1:03d}", episode, selector)
        trajectory = selector.trajectory
        probability_before = _rl_probability_diagnostics(policy, trajectory, limits["evaluation_thresholds"])
        training_seconds = time.monotonic() - episode_started
        if trajectory:
            values = discounted_returns(episode.rewards.reward.to_numpy(), gamma=options.get("gamma", .995))
            reward_index = pd.DatetimeIndex(episode.rewards.index)
            if reward_index.tz is None:
                reward_index = reward_index.tz_localize("UTC")
            returns = []
            for row in trajectory:
                # A close decision can first change fills on a strictly later bar.
                position = reward_index.searchsorted(utc(row["bar_time"]), side="right")
                returns.append(float(values[position]) if position < len(values) else 0.0)
            advantages = np.asarray(returns) - baseline
            scale = max(float(np.std(advantages)), .01)
            update = policy.update(pd.DataFrame([row["features"] for row in trajectory]),
                                   [row["action"] for row in trajectory], advantages / scale,
                                   learning_rate=options.get("learning_rate", .02),
                                   entropy_coef=options.get("entropy_coef", .001))
            baseline = .8 * baseline + .2 * float(np.mean(returns))
        else:
            update = {"status": "no_actionable_original_strategy_candidates", "update_count": policy.update_count}
        validation_started = time.monotonic()
        validations = {}
        selected_threshold, validation_summary, value = limits["evaluation_threshold"], None, -np.inf
        # Order the default first so reward ties preserve the preregistered default.
        thresholds = [limits["evaluation_threshold"], *[threshold for threshold in limits["evaluation_thresholds"]
                                                       if threshold != limits["evaluation_threshold"]]]
        for threshold in thresholds:
            policy.metadata["evaluation_threshold"] = threshold
            valid_selector = selection(dataset, settings, "rl", models, policy=policy, deterministic=True)
            valid = make_environment(frames, protocol, start=settings["splits"]["train_end"],
                                     end=settings["splits"]["validation_end"]).run_episode(valid_selector)
            suffix = "" if len(thresholds) == 1 else f"_threshold_{str(threshold).replace('.', '_')}"
            summary = persist_episode(folder, f"rl_validation_{number+1:03d}{suffix}", valid, valid_selector)
            summary = {**summary, "evaluation_threshold": threshold}
            validations[str(threshold)] = summary
            reward = float(valid.rewards.reward.sum())
            if not np.isfinite(reward):
                raise ValueError("RL validation reward must be finite")
            if reward > value:
                selected_threshold, validation_summary, value = threshold, summary, reward
        policy.metadata["evaluation_threshold"] = selected_threshold
        policy.save(Path(folder) / "models" / f"policy_{number+1:03d}.json")
        resources = {"episode": number + 1, "training_wall_seconds": training_seconds,
                     "validation_wall_seconds": time.monotonic() - validation_started,
                     "wall_seconds": time.monotonic() - episode_started,
                     "training_candidate_count": len(getattr(selector, "audit", trajectory)),
                     "actionable_candidate_count": len(trajectory),
                     "training_selected_count": sum(bool(row["action"]) for row in trajectory),
                     "validation_threshold_count": len(thresholds), **_rl_process_memory()}
        history.append({"episode": number + 1, "train": training_summary, "validation": validation_summary,
                        "update": update, "trajectory_actions": len(trajectory), "baseline": baseline,
                        "checkpoint_model_id": policy.model_id, "checkpoint_update_count": policy.update_count,
                        "actual_updates": policy.update_count, "evaluation_threshold": selected_threshold,
                        "validation_thresholds": validations, "resources": resources,
                        "probability_before": probability_before,
                        "probability_after": _rl_probability_diagnostics(policy, trajectory, limits["evaluation_thresholds"])})
        if value > best_value:
            best_value, stale = value, 0
            best_path = Path(folder) / "models/policy_best.json"
            policy.save(best_path)
        else:
            stale += 1
        policy.save(Path(folder) / "models/policy_latest.json")
        save_json(Path(folder) / "rl_training.json", history)
        _rl_budget_receipt(folder, history, policy, limits, stale, wall_seconds=time.monotonic() - started)
    _rl_budget_receipt(folder, history, policy, limits, stale, wall_seconds=time.monotonic() - started)
    return BernoulliPolicy.load(best_path) if best_path else None


def train_policies(folder, protocol, frames, dataset, models):
    options = protocol["settings"].get("rl", {})
    if not options.get("enabled", True):
        return None
    policies, comparisons, histories = {}, {}, {}
    for seed in options.get("seeds", [options.get("seed", 42)]):
        current = deepcopy(protocol)
        current["settings"]["rl"]["seed"] = seed
        child = Path(folder) / "rl_seeds" / str(seed)
        child.mkdir(parents=True, exist_ok=True)
        policy = train_policy(child, current, frames, dataset, models)
        if policy is not None:
            history = json.loads((child / "rl_training.json").read_text(encoding="utf-8"))
            best = max(history, key=lambda row: row["validation"]["reward_sum"])
            policies[str(seed)], histories[str(seed)] = policy, history
            comparisons[str(seed)] = {"policy_id": policy.model_id,
                                     "validation": best["validation"], "episodes": len(history),
                                     "trajectory_actions": best["trajectory_actions"],
                                     "selected_evaluation_threshold": policy.metadata.get("evaluation_threshold", .5),
                                     "budget": json.loads((child / "rl_budget_receipt.json").read_text(encoding="utf-8"))}
    save_json(Path(folder) / "rl_seed_comparison.json", comparisons)
    if not policies:
        return None
    winner = max(policies, key=lambda seed: comparisons[seed]["validation"]["reward_sum"])
    policies[winner].save(Path(folder) / "models/policy_best.json")
    save_json(Path(folder) / "rl_training.json", histories[winner])
    save_json(Path(folder) / "rl_budget_receipt.json", {**comparisons[winner]["budget"],
        "selected_seed": int(winner), "all_seed_actual_updates": {
            seed: row["budget"]["actual_updates"] for seed, row in comparisons.items()},
        "all_seed_minimum_budgets_met": all(row["budget"]["minimum_budget_met"] for row in comparisons.values())})
    return policies[winner]


def walk_forward(folder, protocol, frames, dataset):
    """Predeclared historical windows; each fits only its own mature history."""
    if "next_research" in protocol["settings"]:
        from research.ml_selection.next_round import complete_walk_forward
        return complete_walk_forward(folder, protocol, frames, dataset)
    summaries = []
    settings = protocol["settings"]
    for number, window in enumerate(settings.get("walk_forward", []), 1):
        current = deepcopy(protocol)
        current["settings"]["splits"] = {"train_end": window["train_end"], "validation_end": window["validation_end"]}
        current["settings"]["end"] = (utc(window["test_end"]) - pd.Timedelta(days=1)).date().isoformat()
        child = Path(folder) / "walk_forward" / f"window_{number:02d}"
        child.mkdir(parents=True, exist_ok=True)
        partition = splits(dataset, current["settings"])
        model_results, trained = {}, {}
        for kind in settings.get("models", ["ridge", "lightgbm"]):
            path = child / "models" / f"{kind}.json"
            model = load_model(path) if path.exists() else fit_model(partition["train"], partition["validation"],
                features=FEATURE_COLUMNS, kind=kind, params=settings.get("model_params", {}).get(kind, {}))
            if not path.exists():
                model.save(path)
            trained[kind] = model
            selector = selection(dataset, current["settings"], kind, trained)
            episode = make_environment(frames, current, start=window["train_end"], end=window["validation_end"]).run_episode(selector)
            model_results[kind] = persist_episode(child, f"validation_{kind}", episode, selector)
        choice = max(trained, key=lambda kind: model_results[kind]["reward_sum"])
        save_json(child / "candidate.json", {"selected_using": "window_validation_reward", "candidate": choice,
                                             "model_id": trained[choice].model_id, "test_used_for_selection": False})
        arms = [("native", 42), ("momentum", 42), (choice, 42)]
        arms.extend(("random", seed) for seed in settings.get("random_seeds", [42, 43, 44]))
        outcomes = {}
        for kind, seed in arms:
            name = kind if kind != "random" else f"random_{seed}"
            progress(folder, "R7-WF", "正在运行滚动样本外对照", window=number, arm=name)
            selector = selection(dataset, current["settings"], kind, trained, seed=seed)
            episode = make_environment(frames, current, start=window["validation_end"], end=window["test_end"]).run_episode(selector)
            outcomes[name] = persist_episode(child, f"test_{name}", episode, selector)
        summaries.append({"window": window, "candidate": choice, "results": outcomes,
                          "candidate_excess_return": outcomes[choice]["net_return"] - outcomes["native"]["net_return"]})
        save_json(Path(folder) / "walk_forward.json", summaries)
    return summaries


def concentration_check(folder, arm, episode_summary=None, *, artifact_directory=None):
    root = Path(folder).resolve()
    if episode_summary is not None:
        if not isinstance(episode_summary, dict):
            raise ValueError("episode summary must be a mapping")
        if artifact_directory is None:
            artifact_directory = episode_summary.get("artifact_directory")
    directory = ((root / artifact_directory).resolve() if artifact_directory is not None
                 else (root / "episodes" / arm).resolve())
    path = (directory / "closed_positions.csv").resolve()
    if not directory.is_relative_to(root) or not path.is_relative_to(root):
        raise ValueError("episode artifact directory must stay within the research run")
    try:
        positions = pd.read_csv(path)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        positions = pd.DataFrame()
    if "net_pnl" not in positions or positions.empty:
        return {"status": "insufficient", "reason": "no_realized_roundtrip_records"}
    if "entry_time" not in positions:
        return {"status": "insufficient", "reason": "missing_entry_time"}
    entry = pd.to_datetime(positions.entry_time, utc=True, errors="coerce")
    pnl = pd.to_numeric(positions.net_pnl, errors="coerce")
    if entry.isna().any() or not np.isfinite(pnl).all():
        return {"status": "insufficient", "reason": "invalid_position_records"}
    cohorts = pnl.groupby(entry.dt.floor("5D")).sum().to_numpy()
    top = np.sort(cohorts[cohorts > 0])
    return {"status": "ok" if len(cohorts) > 5 else "insufficient",
            "cohort_basis": "positions_grouped_by_fixed_5_day_entry_blocks",
            "cohort_count": len(cohorts), "net_pnl": float(cohorts.sum()),
            "remaining_after_top_5": float(cohorts.sum() - top[-5:].sum()),
            "remaining_after_top_10": float(cohorts.sum() - top[-10:].sum()),
            "independent_cohorts_proven": False}


def freeze_candidate(folder, protocol, models, validation, policy=None):
    gates = protocol["settings"].get("gates", {})
    candidates = {name: validation[name] for name in models if name != "primary"}
    if policy is not None:
        history = json.loads((Path(folder) / "rl_training.json").read_text(encoding="utf-8"))
        best = max(history, key=lambda row: row["validation"]["reward_sum"])
        if best["trajectory_actions"]:
            candidates["rl"] = best["validation"]
    qualified = {name: row for name, row in candidates.items()
        if (not gates.get("require_positive_net_return", True) or row["net_return"] > 0)
        and row["max_drawdown"] <= gates.get("max_drawdown", .15)
        and row.get("accounting_ok", False)}
    pool = qualified or candidates
    name = max(pool, key=lambda key: pool[key]["reward_sum"])
    primary = models["primary"].kind
    result = {"primary_model": primary, "selected_candidate": name,
              "validation_reward": candidates[name]["reward_sum"],
              "validation_qualification_passed": name in qualified,
              "selected_using": "validation_actual_equity_reward_with_preregistered_gates",
              "test_used_for_selection": False,
              "model_id": policy.model_id if name == "rl" else models[name].model_id,
              "parent_model_id": models["primary"].model_id}
    path = Path(folder) / "candidate.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if "next_research" in protocol["settings"]:
        if saved is None:
            result["frozen_at"] = datetime.now(timezone.utc).isoformat()
        elif "frozen_at" in saved:
            # Repeating a freeze must retain the actual original clock. An
            # older missing timestamp cannot be reconstructed retroactively.
            result["frozen_at"] = saved["frozen_at"]
            if saved["frozen_at"] is not None:
                frozen_at = utc(saved["frozen_at"])
                if frozen_at > utc(datetime.now(timezone.utc)):
                    raise ValueError("candidate freeze time cannot be in the future")
    if path.exists():
        if saved != result:
            raise ValueError("candidate already frozen; start a new experiment")
    else:
        save_json(path, result)
    return result


def supervised_stage(folder, protocol, frames, dataset):
    models = supervised(folder, protocol, dataset)
    validation = matrix(folder, protocol, frames, dataset, models, segment="validation")
    primary = max(models, key=lambda name: validation[name]["reward_sum"])
    save_json(Path(folder) / "parent_model.json", {"primary_model": primary,
              "selected_using": "validation_actual_equity_reward", "test_used_for_selection": False})
    models["primary"] = models[primary]
    return models, validation


def _stress_episode(folder, name, environment, selector):
    """Preserve an infeasible terminal-liquidity stress, without completing it."""
    try:
        episode = environment.run_episode(selector)
    except ValueError as error:
        if str(error) != "End-window exit cannot fill within actual liquidity":
            raise
        target = Path(folder) / "episodes" / name
        if target.exists():
            attempt = 1
            while target.with_name(name + f"_attempt_{attempt}").exists():
                attempt += 1
            target = target.with_name(name + f"_attempt_{attempt}")
        target.mkdir(parents=True, exist_ok=False)
        engine = getattr(environment, "active_engine", None)
        processor = getattr(engine, "event_processor", None)
        adapter = getattr(engine, "execution_adapter", None)
        broker = getattr(adapter, "broker", None)
        portfolio = getattr(processor, "portfolio", None)
        if portfolio is None and broker is not None:
            portfolio = broker.portfolio
        available = broker is not None and portfolio is not None
        trades = deepcopy(broker.trades) if available else None
        orders = {}
        if available:
            for order in [*broker.pending_orders, *broker.active_orders]:
                orders[order.id] = {key: getattr(order, key, None) for key in (
                    "id", "symbol", "side", "qty", "filled_qty", "remaining_qty", "timestamp",
                    "price", "strategy_id", "submitted_date", "expire_time", "match_not_before")}
                orders[order.id]["status"] = getattr(order.status, "value", order.status)
        marks = deepcopy(getattr(engine, "_last_mark_times", {}))
        risk = getattr(processor, "risk_manager", None)
        risk_state = {key: getattr(risk, key, None) for key in (
            "high_water_equity", "last_drawdown", "circuit_breaker_triggered", "daily_loss_triggered")}
        action = getattr(risk, "breaker_action", None)
        risk_state["breaker_action"] = getattr(action, "value", action)
        evidence = {"schema": "ml-selection-incomplete-stress-evidence/v1",
            "status": "failed_end_window_liquidity", "execution_completed": False,
            "error_type": type(error).__name__, "error": str(error),
            "partial_evidence_status": "available_original_engine_state" if available else "unavailable",
            "terminal_policy": getattr(engine, "terminal_policy", None),
            "terminal_contract_satisfied": False, "last_market_time": max(marks.values()) if marks else None,
            "last_mark_times": marks, "last_prices": deepcopy(getattr(processor, "last_prices", {})),
            "cash_at_failure": portfolio.cash if available else None,
            "positions_at_failure": deepcopy(portfolio.positions) if available else None,
            "open_lots_at_failure": ({symbol: [vars(lot).copy() for lot in portfolio.open_lots(symbol)]
                                      for symbol in portfolio.positions} if available else None),
            "matching_orders_at_failure": list(orders.values()) if available else None,
            "risk_at_failure": risk_state, "recorded_fill_count": len(trades) if trades is not None else None,
            "last_fill_at": max((row["fill_time"] for row in trades), default=None) if trades is not None else None,
            "fills": trades, "execution_audit": deepcopy(getattr(broker, "execution_audit", None)),
            "selection_audit": deepcopy(getattr(selector, "audit", [])),
            "net_return": None, "reward_sum": None, "max_drawdown": None,
            "accounting_ok": None, "terminated_by_risk": None, "formal_admission": False}
        save_json(target / "partial_evidence.json", evidence)
        if trades is not None:
            pd.DataFrame(trades).to_csv(target / "trades.csv", index=False)
        pd.DataFrame(getattr(selector, "audit", [])).to_csv(target / "selection.csv", index=False)
        summary = {key: evidence[key] for key in ("status", "execution_completed", "error_type", "error",
            "partial_evidence_status", "terminal_policy", "recorded_fill_count", "last_market_time",
            "last_fill_at", "net_return", "reward_sum", "max_drawdown", "accounting_ok", "terminated_by_risk")}
        summary.update(artifact_directory=str(target.relative_to(Path(folder))),
                       partial_evidence_file="partial_evidence.json", formal_admission=False)
        save_json(target / "summary.json", summary)
        return summary
    return {**persist_episode(folder, name, episode, selector),
            "status": "completed", "execution_completed": True}



def evaluate(folder, protocol, frames, dataset, models, policy=None):
    results = matrix(folder, protocol, frames, dataset, models, segment="test", policy=policy)
    settings = protocol["settings"]
    frozen = json.loads((Path(folder) / "candidate.json").read_text(encoding="utf-8"))
    candidate = frozen["selected_candidate"]
    identity = policy.model_id if candidate == "rl" else models[candidate].model_id
    if identity != frozen["model_id"]:
        raise ValueError("evaluation candidate identity mismatch")
    start, end = settings["splits"]["validation_end"], utc(settings["end"]) + pd.Timedelta(days=1)
    stress = {}
    for multiplier in settings.get("stress", {}).get("cost_multipliers", [1.5, 2.0]):
        selector = selection(dataset, settings, candidate, models, policy=policy)
        environment = make_environment(frames, protocol, start=start, end=end, multiplier=multiplier)
        stress[str(multiplier)] = _stress_episode(folder, f"stress_{candidate}_{multiplier}", environment, selector)
    save_json(Path(folder) / "cost_stress.json", stress)
    execution_stress = {}
    for scenario in settings.get("stress", {}).get("execution_scenarios", []):
        selector = selection(dataset, settings, candidate, models, policy=policy)
        environment = make_environment(frames, protocol, start=start, end=end, execution_scenario=scenario)
        execution_stress[scenario["name"]] = {"scenario": scenario,
            **_stress_episode(folder, f"execution_stress_{candidate}_{scenario['name']}", environment, selector)}
    save_json(Path(folder) / "execution_stress.json", execution_stress)
    candidate_summary = results[candidate]
    gates = settings.get("gates", {})
    concentration = concentration_check(folder, f"test_{candidate}", candidate_summary)
    checks = {"positive_net_return": candidate_summary["net_return"] > 0 if gates.get("require_positive_net_return", True) else True,
              "outperform_native": candidate_summary["net_return"] > results["native"]["net_return"] if gates.get("require_outperform_native", True) else True,
              "drawdown_within_limit": candidate_summary["max_drawdown"] <= gates.get("max_drawdown", .15),
              "accounting_ok": bool(candidate_summary.get("accounting_ok")),
              "cost_1_5_positive": (stress["1.5"]["net_return"] > 0
                   if "1.5" in stress and stress["1.5"].get("net_return") is not None else None),
              "cost_stress_completed": all(row["execution_completed"] is True for row in stress.values()),
              "execution_stress_completed": all(row["execution_completed"] is True for row in execution_stress.values()),
              "positive_after_top_5": (concentration.get("remaining_after_top_5", -1) > 0
                    if concentration.get("status") == "ok" else None)
                    if gates.get("require_positive_after_removing_top_5", True) else True}
    adjudication = {"candidate": candidate, "checks": checks,
                    "retrospective_checks_passed": all(value is True for value in checks.values()),
                    "concentration": concentration, "execution_stress": execution_stress,
                    "independent_holdout": False, "formal_admission": False,
                    "status": "retrospective_research_only",
                    "outstanding": ["historical PIT universe evidence", "independent final adjudication",
                                    "cohort-level concentration and multiple-testing evidence",
                                    "future observation and label maturity"]}
    if "next_research" in settings:
        adjudication["separate_verdicts"] = {
            "return": {"positive_net_return": checks["positive_net_return"],
                       "outperform_native": checks["outperform_native"],
                       "cost_stress_positive": {name: row["net_return"] > 0 if row.get("net_return") is not None else None
                                                 for name, row in stress.items()}},
            "risk": {"drawdown_within_limit": checks["drawdown_within_limit"],
                     "no_risk_termination": not candidate_summary["terminated_by_risk"],
                     "stress_risk_terminations": {name: row.get("terminated_by_risk") for name, row in execution_stress.items()}},
            "accounting": {"main": checks["accounting_ok"],
                           "all_stress": all(row.get("accounting_ok") is True for row in [*stress.values(), *execution_stress.values()]),
                           "unknown_stress_accounting": [name for name, row in [*stress.items(), *execution_stress.items()]
                                                         if row.get("accounting_ok") is None]},
            "evidence": {"full_historical_pit": False, "independent_final_opened": False,
                         "independent_event_groups_proven": False, "forward_maturity": "pending"}}
    save_json(Path(folder) / "adjudication.json", adjudication)
    save_json(Path(folder) / "roadmap_status.json", {
        "R0": "frozen_research_protocol", "R1": "implemented_observed_history_universe",
        "R2": "implemented_proxy_labels", "R3": "trained", "R4": "retrospective_evaluated",
        "R5": "full_original_engine_episodic_environment_verified_by_tests",
        "R6": "trained" if policy is not None else "disabled",
        "R7": ("retrospective_stress_done_independent_evidence_pending"
                if checks["cost_stress_completed"] and checks["execution_stress_completed"]
                else "retrospective_stress_failures_recorded_independent_evidence_pending"),
        "R8": "shadow_logger_ready_future_observation_pending",
        "effectiveness": adjudication, "production_enabled": False})
    write_report(folder, protocol, results, adjudication)
    return adjudication


def write_report(folder, protocol, results, adjudication):
    lines = ["# 机器学习选币研究结果", "", "本次是历史开发与对照研究，未取得独立最终验收或前瞻收益证据。", "",
             f"协议：`{protocol['protocol_id']}`", f"币种：{len(protocol['data_evidence']['symbols'])}；日线；现金现货。", "",
             "| 方案 | 净收益 | 最大回撤 | 实际成交 | 奖励总和 |", "|---|---:|---:|---:|---:|"]
    for name, row in results.items():
        lines.append(f"| {name} | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {row['actual_fill_count']} | {row['reward_sum']:.6f} |")
    lines.extend(["", f"冻结候选：{adjudication['candidate']}。", "",
                  "历史检查：" + json.dumps(adjudication["checks"], ensure_ascii=False), "",
                  "静态币池存在选择偏差；影子标签是假设小额完整成交的独立交易代理，组合结果来自原回测引擎。",
                  "强化学习采用 CPU Bernoulli 策略梯度，更新发生在完整训练回合之后；未宣称 Gym/PPO 逐步接口或实盘准入。",
                  "最终独立样本、完整历史成员、分组集中度和前瞻标签成熟仍待外部证据。", ""])
    (Path(folder) / "report.md").write_text("\n".join(lines), encoding="utf-8")


def artifact_manifest(folder):
    folder = Path(folder)
    excluded = {"artifacts.json", "progress.json"}
    files = {p.relative_to(folder).as_posix(): sha256_file(p)
             for p in folder.rglob("*") if p.is_file() and p.name not in excluded
             and "shadow" not in p.relative_to(folder).parts}
    save_json(folder / "artifacts.json", files)


def full_run(settings, folder):
    previous_logging = logging.root.manager.disable
    try:
        logging.disable(logging.INFO)
        protocol, frames, dataset = prepare(settings, folder)
        models, validation = supervised_stage(folder, protocol, frames, dataset)
        policy = train_policies(folder, protocol, frames, dataset, models)
        freeze_candidate(folder, protocol, models, validation, policy)
        outcome = evaluate(folder, protocol, frames, dataset, models, policy)
        walk_forward(folder, protocol, frames, dataset)
        artifact_manifest(folder)
        progress(folder, "完成历史训练与对照", "独立验收和前瞻观察仍待新证据", outcome=outcome)
        return outcome
    finally:
        logging.disable(previous_logging)


def shadow(folder, protocol, *, market_data_dir=None, as_of=None, account_state=None):
    settings = protocol["settings"]
    observed_at = utc(datetime.now(timezone.utc))
    now = utc(as_of or observed_at)
    if now > observed_at:
        raise ValueError("shadow cutoff cannot be in the future")
    frozen = json.loads((Path(folder) / "candidate.json").read_text(encoding="utf-8"))
    primary = frozen["selected_candidate"] if frozen.get("selected_candidate", "rl") != "rl" else frozen["primary_model"]
    model = load_model(Path(folder) / "models" / f"{primary}.json")
    trained_through = model.metadata.get("train_latest_label_available_at")
    if trained_through and now <= utc(trained_through):
        raise ValueError("shadow cutoff precedes model training label maturity")
    membership_evidence = None
    if market_data_dir is None:
        frames, _, _, _ = load_inputs(settings)
        source_kind = "frozen_historical_data"
    elif (Path(market_data_dir) / "collection.json").exists():
        from research.ml_selection.forward_evidence import load_public_forward_collection
        frames, membership_evidence = load_public_forward_collection(market_data_dir,
            protocol["data_evidence"]["symbols"], information_cutoff=now)
        source_kind = "immutable_public_forward_receipt"
    else:
        frames = {}
        for symbol in protocol["data_evidence"]["symbols"]:
            path = Path(market_data_dir) / (symbol.replace("/", "_") + ".csv")
            if path.exists():
                frame = pd.read_csv(path, index_col="timestamp", parse_dates=True)
                frames[symbol] = frame.loc[pd.to_datetime(frame.index, utc=True) + pd.Timedelta(days=1) <= now]
        source_kind = "user_supplied_forward_files_universe_unverified"
    if not frames:
        raise ValueError("no available shadow market inputs")
    dataset = build_dataset(frames, **settings.get("dataset", {}))
    latest = dataset.loc[dataset.as_of <= now].sort_values("as_of").groupby("symbol").tail(1)
    fresh = latest.as_of >= now.normalize()
    eligible = latest.eligible & fresh
    if membership_evidence is not None:
        verified = set(membership_evidence["verified_symbols"])
        eligible &= latest.symbol.isin(verified)
        membership_evidence["all_eligible_symbols_verified"] = bool(eligible.any()) and all(
            symbol in verified for symbol in latest.loc[eligible, "symbol"])
        membership_evidence["eligible_registered_symbol_count"] = int(eligible.sum())
    observations = []
    for _, row in latest.iterrows():
        inputs = pd.DataFrame([{name: row[name] for name in FEATURE_COLUMNS}])
        value = float(model.predict(inputs)[0]) if eligible.loc[row.name] else None
        expected_return = (float(model.predict_net_return(inputs)[0])
            if eligible.loc[row.name] and hasattr(model, "predict_net_return") else value)
        if (value is not None and not np.isfinite(value)
                or expected_return is not None and not np.isfinite(expected_return)):
            raise ValueError("nonfinite shadow model predictions")
        frame = frames[row.symbol]
        bar = frame.loc[pd.to_datetime(frame.index, utc=True) == utc(row.bar_time)].iloc[0]
        reference_close = float(bar.close)
        observations.append({"symbol": row.symbol, "as_of": row.as_of,
            "eligible": bool(eligible.loc[row.name]), "predicted_value": value,
            "ranking_score": value, "expected_net_return": expected_return,
            "score_semantics": ("lambdarank_ranking_score_with_separate_net_return_calibration"
                if getattr(model, "kind", None) == "lambdarank" else "predicted_net_return"),
            "features": {name: row[name] for name in FEATURE_COLUMNS},
            "reference_close": reference_close,
            "decision_atr_absolute": float(row.atr_14_pct * reference_close),
            "entry_not_before": observed_at,
            "selected_by_score_gate": expected_return is not None and expected_return > settings.get("selection", {}).get("min_expected_return", 0.),
            "return_gate_threshold": settings.get("selection", {}).get("min_expected_return", 0.),
            "reason": "scored" if eligible.loc[row.name] else "stale_or_ineligible_input",
            "label_available_at": None, "outcome_status": "pending_future_data"})
    replay = now.normalize() < observed_at.normalize() or now < utc(protocol.get("created_at", observed_at))
    status = "retrospective_scoring" if replay and bool(eligible.any()) else "forward_diagnostic_scores_recorded" if bool(eligible.any()) else "awaiting_fresh_market_data"
    if frozen.get("selected_candidate") == "rl":
        status = "requires_account_and_strategy_state_for_frozen_RL_candidate"
    payload = {"observed_at": observed_at, "information_cutoff": now,
               "model_id": model.model_id, "frozen_candidate_id": frozen.get("model_id"),
               "diagnostic_scoring_only": True, "retrospective_observation": replay,
               "protocol_id": protocol["protocol_id"],
               "source_kind": source_kind, "source_hashes": {s: sha256_frame(f) for s, f in frames.items()},
               "status": status,
               "real_orders": False, "formal_admission": False, "observations": observations}
    if membership_evidence is not None:
        payload["membership_evidence"] = membership_evidence
    if frozen.get("selected_candidate") == "rl" and account_state is not None:
        from research.ml_selection.forward_bridge import bridge_decision
        policy = BernoulliPolicy.load(Path(folder) / "models" / "policy_best.json")
        if policy.model_id != frozen["model_id"] or model.model_id != frozen["parent_model_id"]:
            raise ValueError("shadow frozen RL model identity mismatch")
        state = (json.loads(Path(account_state).read_text(encoding="utf-8"))
                 if isinstance(account_state, (str, Path)) else account_state)
        decision = bridge_decision(state, dataset, model, policy, protocol_id=protocol["protocol_id"],
                                  information_cutoff=now, selection_options=settings.get("selection", {}))
        payload["rl_decision"] = decision
        payload["status"] = "retrospective_rl_decision" if replay else "forward_rl_decision_recorded"
        payload["diagnostic_scoring_only"] = False
        payload["simulated_account_performance_available"] = False
    payload = json.loads(canonical_json(payload))
    payload["observation_id"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    day = observed_at.strftime("%Y%m%dT%H%M%S%f")
    path = Path(folder) / "shadow" / f"observation_{day}.json"
    if path.exists():
        raise ValueError("shadow observation already exists; no historical rewrite allowed")
    save_json(path, payload)
    return payload


def resolve_shadow(folder, protocol, *, market_data_dir, as_of=None):
    """Append matured proxy outcomes without rewriting recorded predictions."""
    observed_at = utc(datetime.now(timezone.utc))
    cutoff = utc(as_of or observed_at)
    if cutoff > observed_at:
        raise ValueError("outcome cutoff cannot be in the future")
    frames = {}
    for symbol in protocol["data_evidence"]["symbols"]:
        source = Path(market_data_dir) / (symbol.replace("/", "_") + ".csv")
        if source.exists():
            frame = pd.read_csv(source, index_col="timestamp", parse_dates=True)
            times = pd.to_datetime(frame.index, utc=True)
            frames[symbol] = frame.loc[times + pd.Timedelta(days=1) <= cutoff]
    if not frames:
        raise ValueError("no closed market data for shadow outcome resolution")
    dataset = build_dataset(frames, **protocol["settings"].get("dataset", {}))
    table = dataset.set_index(["symbol", "as_of"])
    resolved, pending = [], 0
    for source in sorted((Path(folder) / "shadow").glob("observation_*.json")):
        observation = json.loads(source.read_text(encoding="utf-8"))
        identifier = observation.pop("observation_id", None)
        if hashlib.sha256(canonical_json(observation).encode()).hexdigest() != identifier:
            raise ValueError("shadow observation changed after recording")
        if observation["protocol_id"] != protocol["protocol_id"]:
            raise ValueError("shadow observation belongs to another protocol")
        for item in observation["observations"]:
            if not item["eligible"]:
                continue
            key = (item["symbol"], utc(item["as_of"]))
            if key not in table.index:
                pending += 1
                continue
            row = table.loc[key]
            same = all(np.isclose(float(item["features"][name]), float(row[name]),
                                  rtol=1e-10, atol=1e-12, equal_nan=True)
                       if item["features"][name] is not None else pd.isna(row[name])
                       for name in FEATURE_COLUMNS)
            if not same:
                raise ValueError("recorded feature history was revised; outcome cannot silently replace it")
            replay = observation.get("retrospective_observation", observation["status"] == "retrospective_scoring")
            if replay:
                if pd.isna(row.label_available_at) or row.label_available_at > cutoff or not np.isfinite(row.label_net_return):
                    pending += 1
                    continue
                outcome = {"label_net_return": float(row.label_net_return),
                           "label_available_at": row.label_available_at}
            else:
                if "decision_atr_absolute" not in item or "entry_not_before" not in item:
                    pending += 1
                    continue
                frame = frames[item["symbol"]]
                bar = frame.loc[pd.to_datetime(frame.index, utc=True) == utc(row.bar_time)].iloc[0]
                if not np.isclose(float(bar.close), item["reference_close"], rtol=1e-10, atol=1e-12):
                    raise ValueError("recorded reference price history was revised")
                label_options = protocol["settings"].get("dataset", {})
                outcome = forward_proxy_outcome(frame, entry_not_before=item["entry_not_before"],
                    decision_atr_absolute=item["decision_atr_absolute"], mature_as_of=cutoff,
                    horizon_bars=label_options.get("horizon_bars", 20),
                    commission_rate=label_options.get("commission_rate", .001),
                    slippage_bps=label_options.get("slippage_bps", 5),
                    stop_atr_multiple=label_options.get("stop_atr_multiple", 2))
                if outcome["status"] != "resolved":
                    pending += 1
                    continue
            resolved.append({"observation_id": identifier, "symbol": item["symbol"], "as_of": item["as_of"],
                             "predicted_value": item["predicted_value"],
                             **outcome,
                             "label_basis": "independent_shadow_proxy_not_portfolio",
                             "evidence_kind": "retrospective" if replay else "forward_diagnostic"})
    payload = {"observed_at": observed_at, "information_cutoff": cutoff,
               "protocol_id": protocol["protocol_id"], "pending_labels": pending,
               "resolved": resolved, "formal_admission": False,
               "source_frame_hashes": {s: sha256_frame(f) for s, f in frames.items()}}
    path = Path(folder) / "shadow" / ("outcomes_" + observed_at.strftime("%Y%m%dT%H%M%S%f") + ".json")
    save_json(path, payload)
    return payload
