"""Preregistered small reward pilots, separated from the formal update budget."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from core.reproducibility import canonical_json, sha256_frame
from research.ml_selection.protocol import save_json


def run_reward_pilot(folder, protocol, frames, dataset, models, *, episodes=2,
                     drawdown_weights=(0., .25, .5), seeds=(42, 43, 44)):
    """Replay only the main training/validation window under registered rewards.

    These pilot steps measure resource use and reward behavior. They never
    count toward any separate formal run's minimum updates. All registered
    cells remain in the report, including empty-candidate and failed cells.
    The caller's frozen protocol and models are retained unchanged.
    """
    from research.ml_selection.pipeline import _rl_budget_options, _rl_process_memory, train_policy

    if type(episodes) is not int or episodes < 1:
        raise ValueError("pilot episodes must be a positive integer")
    if not isinstance(seeds, (tuple, list)) or not seeds or any(type(seed) is not int or seed < 0 for seed in seeds):
        raise ValueError("pilot seeds must be nonnegative integers")
    if len(set(seeds)) != len(seeds):
        raise ValueError("pilot seeds must be distinct")
    if not isinstance(drawdown_weights, (tuple, list)) or not drawdown_weights or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not np.isfinite(value) or value < 0 for value in drawdown_weights):
        raise ValueError("pilot drawdown weights must be finite and nonnegative")
    weights = [float(value) for value in drawdown_weights]
    if len(set(weights)) != len(weights):
        raise ValueError("pilot drawdown weights must be distinct")
    settings = deepcopy(protocol["settings"])
    pilot_options = deepcopy(settings.get("rl", {}))
    pilot_options.update(enabled=True, episodes=episodes, min_updates=0,
                         early_stopping_start_updates=episodes,
                         validation_patience=max(episodes, pilot_options.get("validation_patience", 3)),
                         turnover_penalty=0.)
    _rl_budget_options(pilot_options)
    cells = [{"cell": f"reward_{number:02d}_seed_{seed}", "seed": seed, "drawdown_penalty": weight,
              "turnover_penalty": 0., "episode_limit": episodes}
             for number, weight in enumerate(weights, 1) for seed in seeds]
    registration = {"schema": "ml-selection-reward-pilot/v1",
                    "parent_protocol_id": protocol.get("protocol_id"),
                    "parent_model_id": models["primary"].model_id,
                    "dataset_sha256": sha256_frame(dataset),
                    "training_start": settings["start"], "splits": settings["splits"],
                    "rl_settings": pilot_options, "cells": cells,
                    "selection_segment": "validation", "test_used_for_selection": False,
                    "formal_budget_contribution": 0, "cash_is_legal": True,
                    "short_episode_sampling": False, "runs_are_replayed_historical_samples": True}
    registration["registration_id"] = hashlib.sha256(canonical_json(registration).encode()).hexdigest()
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    frozen_path = target / "pilot_registration.json"
    if frozen_path.exists():
        if json.loads(frozen_path.read_text(encoding="utf-8")) != registration:
            raise ValueError("reward pilot registration changed; create a new pilot directory")
    else:
        save_json(frozen_path, registration)
    results = {cell["cell"]: {**cell, "status": "pending"} for cell in cells}
    started = time.monotonic()

    def save_report():
        reported = list(results.values())
        completed = [row for row in reported if row["status"] == "completed"]
        return_value = {"registration_id": registration["registration_id"], "cells": reported,
                       "registered_cells": len(cells), "completed_cells": len(completed),
                       "failed_cells": sum(row["status"] == "failed" for row in reported),
                       "total_actual_updates": sum(row.get("actual_updates", 0) for row in reported),
                       "maximum_pilot_updates": len(cells) * episodes,
                       "formal_budget_contribution": 0, "test_used_for_selection": False,
                       "invocation_wall_seconds": time.monotonic() - started, **_rl_process_memory()}
        save_json(target / "pilot_results.json", return_value)
        return return_value

    save_report()
    for cell in cells:
        child = target / cell["cell"]
        child.mkdir(parents=True, exist_ok=True)
        current = deepcopy(protocol)
        current["settings"]["rl"] = {**pilot_options, "seed": cell["seed"],
                                     "seeds": [cell["seed"]], "drawdown_penalty": cell["drawdown_penalty"]}
        save_json(child / "pilot_cell_settings.json", {"registration_id": registration["registration_id"],
                   "settings": current["settings"], "formal_budget_contribution": 0})
        cell_started = time.monotonic()
        try:
            policy = train_policy(child, current, frames, dataset, models)
            history = json.loads((child / "rl_training.json").read_text(encoding="utf-8"))
            receipt = json.loads((child / "rl_budget_receipt.json").read_text(encoding="utf-8"))
            resources = json.loads((child / "rl_resources.json").read_text(encoding="utf-8"))
            best = max(history, key=lambda row: row["validation"]["reward_sum"])
            results[cell["cell"]] = {**cell, "status": "completed", "actual_updates": receipt["actual_updates"],
                                      "policy_id": policy.model_id if policy else None,
                                      "selected_evaluation_threshold": policy.metadata.get("evaluation_threshold", .5) if policy else None,
                                      "validation": best["validation"], "budget": receipt, "resources": resources,
                                      "wall_seconds": time.monotonic() - cell_started}
        except Exception as error:
            # A failure may occur after earlier episodes committed. Report the
            # proven completed watermark rather than hiding their update steps.
            from research.ml_selection.models import BernoulliPolicy
            completed_updates, completed_episodes = 0, 0
            try:
                partial = json.loads((child / "rl_training.json").read_text(encoding="utf-8"))
                if partial:
                    saved = BernoulliPolicy.load(child / "models" / f"policy_{partial[-1]['episode']:03d}.json")
                    if partial[-1].get("checkpoint_model_id") == saved.model_id:
                        completed_updates, completed_episodes = saved.update_count, len(partial)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            results[cell["cell"]] = {**cell, "status": "failed", "error_type": type(error).__name__,
                                      "error": str(error), "actual_updates": completed_updates,
                                      "completed_episodes": completed_episodes,
                                      "wall_seconds": time.monotonic() - cell_started}
        save_report()
    return save_report()
