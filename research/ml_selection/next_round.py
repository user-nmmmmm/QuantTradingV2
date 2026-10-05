"""Evidence-aware execution of the six-stage post-V1 research roadmap."""
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path
import time

import pandas as pd

from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.protocol import save_json
from research.ml_selection.selector import utc


def event_concentration(positions):
    """Merge overlapping holding intervals into conservative exposure events.

    Separate intervals still share market regimes; independence remains
    unproven. Neither raw trades nor different RNG seeds are independent data.
    """
    required = {"entry_time", "exit_time", "net_pnl"}
    if positions.empty or not required <= set(positions):
        return {"status": "insufficient", "reason": "closed_position_intervals_missing",
                "independent_events_proven": False}
    rows = positions.copy()
    for name in ("entry_time", "exit_time"):
        rows[name] = pd.to_datetime(rows[name], utc=True, errors="coerce")
    rows["net_pnl"] = pd.to_numeric(rows.net_pnl, errors="coerce")
    if rows[list(required)].isna().any().any() or (rows.exit_time < rows.entry_time).any():
        return {"status": "insufficient", "reason": "invalid_closed_position_intervals",
                "independent_events_proven": False}
    import numpy as np
    if not np.isfinite(rows.net_pnl).all():
        return {"status": "insufficient", "reason": "invalid_position_pnl", "independent_events_proven": False}
    groups, end = [], None
    for row in rows.sort_values("entry_time").itertuples():
        if end is None or row.entry_time > end:
            groups.append({"start": row.entry_time.isoformat(), "end": row.exit_time.isoformat(),
                           "positions": 1, "net_pnl": float(row.net_pnl)})
            end = row.exit_time
        else:
            end = max(end, row.exit_time)
            groups[-1]["end"] = end.isoformat()
            groups[-1]["positions"] += 1
            groups[-1]["net_pnl"] += float(row.net_pnl)
    total = sum(row["net_pnl"] for row in groups)
    winners = sorted((row["net_pnl"] for row in groups if row["net_pnl"] > 0), reverse=True)
    return {"status": "ok" if len(groups) > 5 else "insufficient", "events": groups,
            "event_count": len(groups), "net_pnl": total,
            "remaining_after_top_5": total - sum(winners[:5]),
            "remaining_after_top_10": total - sum(winners[:10]),
            "group_basis": "connected_overlapping_actual_holding_intervals",
            "independent_events_proven": False}


def complete_walk_forward(folder, protocol, frames, dataset):
    """Report all frozen score windows and the registered additional RL window."""
    from research.ml_selection.pipeline import (supervised_stage, train_policies, freeze_candidate,
                                               evaluate, progress, artifact_manifest)
    settings, results = protocol["settings"], []
    rl_windows = settings["next_research"].get("rl_windows", [])
    for number, window in enumerate(settings.get("walk_forward", []), 1):
        current = deepcopy(protocol)
        current["settings"]["splits"] = {key: value for key, value in window.items() if key != "test_end"}
        current["settings"]["end"] = (utc(window["test_end"]) - pd.Timedelta(days=1)).date().isoformat()
        current["settings"]["rl"]["enabled"] = number in rl_windows
        child = Path(folder) / "walk_forward" / f"window_{number:02d}"
        child.mkdir(parents=True, exist_ok=True)
        save_json(child / "window_protocol.json", {"parent_protocol_id": protocol["protocol_id"],
            "window": window, "settings": current["settings"], "independent_holdout": False,
            "account_initialization": "separate_fresh_account_no_equity_stitching"})
        progress(folder, "next-5", "正在完成预登记滚动窗口", window=number, rl=number in rl_windows)
        models, validation = supervised_stage(child, current, frames, dataset)
        policy = train_policies(child, current, frames, dataset, models) if number in rl_windows else None
        candidate = freeze_candidate(child, current, models, validation, policy)
        verdict = evaluate(child, current, frames, dataset, models, policy)
        comparisons = json.loads((child / "test_comparison.json").read_text(encoding="utf-8"))
        for name, summary in comparisons.items():
            path = child / summary["artifact_directory"] / "closed_positions.csv"
            comparisons[name]["event_concentration"] = event_concentration(pd.read_csv(path))
        row = {"window_number": number, "window": window, "candidate": candidate,
            "results": comparisons, "adjudication": verdict,
            "rl_trained": policy is not None, "independent_holdout": False}
        if number in rl_windows:
            row["rl_seed_comparison"] = json.loads((child / "rl_seed_comparison.json").read_text(encoding="utf-8"))
        results.append(row)
        save_json(Path(folder) / "walk_forward.json", results)
        artifact_manifest(child)
    return results


def _supervised_extensions(folder, protocol, frames, dataset):
    from composition.factory import build_strategy_registry
    from config.config import config
    from research.ml_selection.pipeline import (make_environment, persist_episode, training_partitions, selection, progress)
    from research.ml_selection.label_experiments import ExitLabelContract, run_exit_label_probe
    from research.ml_selection.learning import run_learning_curve
    from research.ml_selection.membership import audit_membership
    settings = protocol["settings"]
    extension = settings["next_research"]
    progress(folder, "next-2", "正在记录原策略训练候选与真实退出干预")
    environment = make_environment(frames, protocol, start=settings["start"], end=settings["splits"]["train_end"])
    episode = environment.run_episode()
    persist_episode(folder, "training_native_diagnostic", episode)
    candidates = []
    for row in episode.result.get("entry_observations", []):
        if row.get("signal") and row.get("native_score") is not None:
            candidates.append({"as_of": utc(row.get("as_of", utc(row["timestamp"]) + pd.Timedelta(days=1))),
                "symbol": row["symbol"], "strategy": row.get("strategy"), "signal": row["signal"],
                "original_candidate": True})
    candidates = sorted(candidates, key=lambda row: (row["as_of"], row["symbol"]))[:extension.get("exit_probe_count", 12)]
    old_parameters = config._config
    try:
        config._config = deepcopy(environment.parameters)
        strategies = build_strategy_registry(config)
    finally:
        config._config = old_parameters
    contract = ExitLabelContract(initial_capital=episode.summary["initial_capital"],
        nominal_notional=1000., risk_budget_fraction=.02, horizon_bars=60)
    save_json(Path(folder) / "label_contract.json", {"proxy": settings["dataset"], "original_exit": asdict(contract),
        "probe_candidates": candidates, "unfilled_target": "missing_execution_label_not_zero_return",
        "signal_history": "same_registered_frame_start_and_warmup_as_training_native_diagnostic",
        "signal_history_start": {symbol: frame.index.min().isoformat() for symbol, frame in environment.frames.items()},
        "portfolio_marginal_value": "requires_paired_original_engine_accounts_not_inferred_from_proxy",
        "probe_scope": "development_diagnostic_not_replacement_training_targets"})
    if candidates:
        probe_rows, probes = run_exit_label_probe(environment.frames, pd.DataFrame(candidates), strategies=strategies,
            contract=contract, parameters=environment.parameters, engine_options=environment.engine_options,
            max_candidates=extension.get("exit_probe_count", 12))
        probe_rows.to_csv(Path(folder) / "exit_label_probe_rows.csv", index=False)
    else:
        probes = {"status": "insufficient", "reason": "no_original_candidates"}
    save_json(Path(folder) / "exit_label_probes.json", probes)
    _, membership = audit_membership(settings.get("dataset", {}).get("membership"),
                                    dataset[["symbol", "as_of"]], missing_policy="downgrade")
    save_json(Path(folder) / "membership_evidence.json", membership)
    partition, _ = training_partitions(dataset, settings)
    curves = {}
    for kind in settings["models"]:
        def portfolio_evaluator(model, train, validation, month):
            selector = selection(dataset, settings, kind, {kind: model})
            episode = make_environment(frames, protocol, start=settings["splits"]["train_end"],
                end=settings["splits"]["validation_end"]).run_episode(selector)
            return persist_episode(folder, f"learning_{kind}_{month}m", episode, selector)
        def model_sink(model, month):
            model.save(Path(folder) / "learning_models" / f"{kind}_{month}m.json")
        progress(folder, "next-3", "正在比较成熟训练期学习曲线", model=kind)
        curves[kind] = run_learning_curve(partition["train"], partition["validation"], features=FEATURE_COLUMNS,
            kind=kind, params=settings.get("model_params", {}).get(kind, {}), seed=settings.get("seed", 42),
            months=extension.get("learning_months", [12, 24, 36]),
            mature_as_of=settings["splits"]["validation_end"], portfolio_evaluator=portfolio_evaluator,
            model_sink=model_sink, horizon_days=settings.get("dataset", {}).get("horizon_bars", 20))
        save_json(Path(folder) / "learning_curves.json", curves)
    return {"exit_probes": probes, "membership": membership, "learning_curves": curves}


def verify_rl_bridge(folder, protocol, frames, dataset, models, policy):
    from research.ml_selection.pipeline import make_environment, selection, persist_episode
    from research.ml_selection.forward_bridge import RecordingSelector
    if policy is None:
        return {"status": "awaiting_rl_policy", "all_decisions_identical": False}
    settings = protocol["settings"]
    selector = RecordingSelector(selection(dataset, settings, "rl", models, policy=policy), protocol["protocol_id"])
    replay = make_environment(frames, protocol, start=settings["splits"]["train_end"],
        end=settings["splits"]["validation_end"]).run_episode(selector)
    summary = persist_episode(folder, "rl_state_bridge_replay", replay, selector)
    save_json(Path(folder) / "rl_hook_states.json", selector.snapshots)
    bridge = {"status": "fixed_policy_original_engine_replay", "snapshots": len(selector.snapshots),
        "all_decisions_identical": bool(selector.replay_comparisons) and not selector.verification_errors
            and all(row["identical"] for row in selector.replay_comparisons),
        "verification_errors": selector.verification_errors,
        "comparisons": selector.replay_comparisons, "episode": summary, "forward_evidence": False}
    save_json(Path(folder) / "rl_bridge_verification.json", bridge)
    return bridge


def pilot_status(pilot):
    """An attempted matrix is complete only when every registered cell ran."""
    registered = pilot.get("registered_cells", 0)
    completed = pilot.get("completed_cells", 0)
    if registered > 0 and completed == registered and pilot.get("failed_cells", 0) == 0:
        return "pilot_completed"
    return "pilot_completed_with_failures" if completed else "pilot_failed"


def formal_budget_summary(folder, settings):
    """Check the entire registered seed/window matrix, including absent cells."""
    root = Path(folder)
    parents = [root, *[root / "walk_forward" / f"window_{number:02d}"
                      for number in settings["next_research"].get("rl_windows", [])]]
    receipts, expected, missing = [], [], []
    for parent in parents:
        for seed in settings["rl"].get("seeds", [42, 43, 44]):
            path = parent / "rl_seeds" / str(seed) / "rl_budget_receipt.json"
            cell = path.parent.relative_to(root).as_posix()
            expected.append(cell)
            if not path.is_file():
                missing.append(cell)
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            receipts.append({"directory": cell, **row})
    from research.ml_selection.protocol import evaluation_contract
    calibration_counts = 0
    for parent in parents:
        calibration = parent / "threshold_calibration.json"
        if calibration.is_file():
            calibration_counts += json.loads(calibration.read_text(encoding="utf-8"))["trial_counts"]["threshold_calibration_accounts"]
    return {"rl_budgets": receipts, "expected_rl_budget_cells": expected,
            "missing_rl_budget_cells": missing,
            "evaluation_scope": evaluation_contract(settings),
            "trial_counts": {"checkpoint_validation_accounts": sum(
                row.get("trial_counts", {}).get("checkpoint_validation_accounts", 0) for row in receipts),
                "threshold_calibration_accounts": calibration_counts,
                "legacy_counts_missing": any("trial_counts" not in row for row in receipts)},
            "actual_updates": sum(row.get("actual_updates", 0) for row in receipts),
            "all_minimum_budgets_met": bool(expected) and not missing
                and all(row.get("minimum_budget_met") is True for row in receipts)}


def run_next(settings, folder, *, previous_run=None, pilot_only=False, forward_data_dir=None):
    from research.ml_selection.pipeline import (prepare, supervised_stage, train_policies, freeze_candidate,
        evaluate, walk_forward, artifact_manifest, progress, make_environment, selection, persist_episode, shadow)
    from research.ml_selection.diagnostics import audit_existing_run
    from research.ml_selection.rl_experiments import run_reward_pilot
    started = time.monotonic()
    previous_logging = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        protocol, frames, dataset = prepare(settings, folder)
        if previous_run:
            audit_existing_run(previous_run, Path(folder) / "v1_diagnostic")
        extensions = _supervised_extensions(folder, protocol, frames, dataset)
        models, validation = supervised_stage(folder, protocol, frames, dataset)
        from research.ml_selection.batch_comparison import BatchAllocationComparisonSelector
        import numpy as np
        native_selector = selection(dataset, settings, "qualified_native", models)
        table = native_selector.table
        def qualified(candidates, context):
            stamp = utc(context["event"].timestamp) + pd.Timedelta(days=1)
            return [candidate for candidate in candidates if (candidate.symbol, stamp) in table.index
                    and bool(table.loc[(candidate.symbol, stamp), "eligible"])]
        def scores(candidates, context):
            stamp = utc(context["event"].timestamp) + pd.Timedelta(days=1)
            rows = pd.DataFrame([table.loc[(candidate.symbol, stamp)] for candidate in candidates])
            def allocation_score(values):
                scaled = np.asarray(values) * native_selector.score_scale
                return np.clip(native_selector.score_cap * .5 * (1 + scaled / (1 + np.abs(scaled))), .01, native_selector.score_cap)
            return {"momentum": allocation_score(rows["return_20d"].to_numpy()),
                    "model": allocation_score(models["primary"].predict(rows))}
        batch_environment = make_environment(frames, protocol, start=settings["splits"]["train_end"],
                                             end=settings["splits"]["validation_end"])
        batch_selector = BatchAllocationComparisonSelector(native_selector, score_provider=scores,
            qualification_provider=qualified, seed=42, max_batches=settings["next_research"].get("exit_probe_count", 12),
            allocator_provider=lambda context: batch_environment.active_engine.event_processor.allocator,
            allocator_state_source="original_engine_current_allocator")
        batch_episode = batch_environment.run_episode(batch_selector)
        persist_episode(folder, "fixed_batch_ranking_validation", batch_episode, batch_selector)
        save_json(Path(folder) / "fixed_batch_allocation_comparison.json", batch_selector.comparison_summary())
        extension = settings["next_research"]
        pilot = run_reward_pilot(Path(folder) / "reward_pilot", protocol, frames, dataset, models,
            episodes=extension.get("pilot_episodes", 2), drawdown_weights=extension.get("reward_pilot_weights", [0, .25, .5]),
            seeds=tuple(settings["rl"].get("seeds", [42, 43, 44])))
        save_json(Path(folder) / "pilot_resource_report.json", pilot)
        if pilot_only:
            from research.ml_selection.models import BernoulliPolicy
            completed = next((row for row in pilot["cells"] if row["status"] == "completed" and row.get("policy_id")), None)
            policy = (BernoulliPolicy.load(Path(folder) / "reward_pilot" / completed["cell"] / "models/policy_best.json")
                      if completed else None)
            bridge = verify_rl_bridge(folder, protocol, frames, dataset, models, policy)
            pilot_receipt = {"status": pilot_status(pilot), "formal_budget_run": False,
                            "resources": pilot, "rl_bridge": bridge, "seconds": time.monotonic() - started}
            save_json(Path(folder) / "pilot_execution_receipt.json", pilot_receipt)
            artifact_manifest(folder)
            return pilot_receipt
        if pilot_status(pilot) != "pilot_completed":
            save_json(Path(folder) / "next_execution_receipt.json", {
                "status": "formal_budget_blocked_by_incomplete_pilot", "protocol_id": protocol["protocol_id"],
                "pilot_status": pilot_status(pilot), "formal_budget_run": False,
                "formal_admission": False, "production_enabled": False})
            artifact_manifest(folder)
            raise ValueError("reward/resource pilot incomplete; formal budget was not started")
        policy = train_policies(folder, protocol, frames, dataset, models)
        candidate = freeze_candidate(folder, protocol, models, validation, policy)
        verdict = evaluate(folder, protocol, frames, dataset, models, policy)
        windows = walk_forward(folder, protocol, frames, dataset)
        bridge = verify_rl_bridge(folder, protocol, frames, dataset, models, policy)
        forward = shadow(folder, protocol, market_data_dir=forward_data_dir)
        from research.ml_selection.forward_evidence import evaluate_forward_store
        forward_report = evaluate_forward_store(folder, protocol)
        save_json(Path(folder) / "forward_evidence.json", forward_report)
        budget = formal_budget_summary(folder, settings)
        final_start = utc(extension["final_sample"]["start"])
        final_preregistered = (utc(protocol["created_at"]) < final_start
            and candidate.get("frozen_at") is not None and utc(candidate["frozen_at"]) < final_start)
        receipt = {"schema": "ml-selection-next-execution/v1", "protocol_id": protocol["protocol_id"],
            "candidate": candidate, "main_adjudication": verdict, **budget,
            "walk_forward_windows": len(windows), "learning_models": list(extensions["learning_curves"]),
            "rl_bridge": bridge, "forward_status": forward["status"],
            "independent_final_status": ("preregistered_future_sample_not_opened" if final_preregistered
                else "pending_candidate_not_frozen_before_start_requires_new_future_protocol"),
            "historical_pit_universe_verified": False, "formal_admission": False, "production_enabled": False,
            "outstanding_external_evidence": ["complete_historical_exchange_membership_with_sources",
                "fresh_point_in_time_market_and_membership", "prospective_elapsed_observations_and_mature_labels",
                "unopened_independent_final_sample"], "seconds": time.monotonic() - started}
        save_json(Path(folder) / "next_execution_receipt.json", receipt)
        artifact_manifest(folder)
        progress(folder, "next-complete", "研究运行与工程验证完成，外部证据保持待判定", actual_updates=receipt["actual_updates"])
        return receipt
    finally:
        logging.disable(previous_logging)
