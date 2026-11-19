"""Fixed-evaluation, equal-parameter-budget development learning curves."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

import numpy as np
import pandas as pd

from research.ml_selection.models import calibration_metrics, fit_model, ranking_metrics


def run_learning_curve(frame: pd.DataFrame, validation: pd.DataFrame, *, features,
                       kind="ridge", params=None, seed=42, months=(12, 24, 36),
                       mature_as_of=None, portfolio_evaluator=None, model_sink=None,
                       horizon_days=20) -> dict:
    """Fit each length on past mature rows, and evaluate an identical interval.

    ``portfolio_evaluator(model, training_rows, validation_rows, month)`` must
    replay the original engine under the same account and execution contract.
    Without that callback portfolio performance is explicitly unmeasured.
    ``model_sink(model, month)`` may persist each independently fitted artifact.
    """
    options = deepcopy(dict(params or {}))
    target = options.get("target_column", "label_net_return")
    as_of = options.get("as_of_column", "as_of")
    availability_column = options.get("label_available_column", "label_available_at")
    if not months or any(isinstance(m, bool) or not isinstance(m, (int, np.integer)) or m < 1 for m in months) or len(set(months)) != len(months):
        raise ValueError("months must contain distinct positive integers")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, (int, np.integer)) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer")
    required = {as_of, availability_column, target, *features}
    if not required <= set(frame) or not required <= set(validation):
        raise ValueError("learning curve requires decision, availability, target and feature columns")
    training = frame.copy()
    evaluation = validation.copy()
    if "eligible" in training:
        training = training.loc[training.eligible].copy()
    if "eligible" in evaluation:
        evaluation = evaluation.loc[evaluation.eligible].copy()
    if evaluation.empty:
        raise ValueError("learning curve requires a nonempty fixed evaluation interval")
    training[as_of] = pd.to_datetime(training[as_of], utc=True, errors="raise")
    evaluation[as_of] = pd.to_datetime(evaluation[as_of], utc=True, errors="raise")
    if training[as_of].isna().any() or evaluation[as_of].isna().any():
        raise ValueError("learning curve decision timestamps must be finite")
    if mature_as_of is None:
        raise ValueError("learning curve requires an explicit evaluation maturity cutoff")
    cutoff = pd.to_datetime(mature_as_of, utc=True, errors="raise")
    if pd.isna(cutoff):
        raise ValueError("evaluation maturity cutoff must be finite")
    train_end = evaluation[as_of].min()
    if cutoff < evaluation[as_of].max():
        raise ValueError("evaluation maturity cutoff precedes evaluation decisions")
    available = pd.to_datetime(training[availability_column], utc=True, errors="coerce")
    targets = pd.to_numeric(training[target], errors="coerce")
    evaluation_available = pd.to_datetime(evaluation[availability_column], utc=True, errors="coerce")
    evaluation_mature = evaluation_available.notna() & (evaluation_available <= cutoff)
    # Do not inspect a future target, even to decide whether it is numeric.
    evaluation_labels = pd.Series(np.nan, index=evaluation.index)
    evaluation_labels.loc[evaluation_mature] = pd.to_numeric(evaluation.loc[evaluation_mature, target], errors="coerce")
    evaluation_complete = (evaluation_mature & np.isfinite(evaluation_labels)).groupby(evaluation[as_of]).transform("all")
    evaluation_for_fit = evaluation.loc[evaluation_complete].copy()
    budget_hash = hashlib.sha256(json.dumps({"kind": kind, "params": options, "features": list(features),
                                            "seed": int(seed)}, sort_keys=True, allow_nan=False).encode()).hexdigest()
    windows = []
    for month in months:
        requested_start = train_end - pd.DateOffset(months=int(month))
        period = (training[as_of] >= requested_start) & (training[as_of] < train_end)
        individually_mature = period & available.notna() & (available < train_end) & np.isfinite(targets)
        # Keep entire decision groups. A pending peer is evidence that this
        # query is incomplete, even when the remaining peer's loss is known.
        complete_groups = individually_mature.groupby(training[as_of]).transform("all")
        mature = individually_mature & complete_groups
        subset = training.loc[mature].copy()
        if len(subset) and (available.loc[mature] < subset[as_of]).any():
            raise ValueError("training label availability precedes its decision")
        dates = subset[as_of]
        blocks = dates.astype("int64") // int(pd.Timedelta(days=int(horizon_days)).value)
        item = {"training_months": int(month), "parameter_budget_id": budget_hash,
                "requested_train_start": requested_start.isoformat(), "train_end": train_end.isoformat(),
                "raw_period_rows": int(period.sum()), "rows": int(len(subset)),
                "dropped_pending_or_unknown_rows": int(period.sum() - mature.sum()),
                "decision_dates": int(dates.nunique()), "time_blocks": int(blocks.nunique()),
                "actual_train_start": dates.min().isoformat() if len(dates) else None,
                "actual_train_end": dates.max().isoformat() if len(dates) else None,
                "observed_month_coverage": int(training[as_of].min() <= requested_start) if len(training) else 0,
                "dependency": {"overlap_horizon_days": int(horizon_days),
                    "time_block_days": int(horizon_days), "cross_sectional_rows_are_dependent": True,
                    "blocks_are_not_claimed_independent": True, "market_regime_dependence": "unresolved"},
                "evaluation_rows": int(len(evaluation)), "evaluation_min_as_of": evaluation[as_of].min().isoformat(),
                "evaluation_max_as_of": evaluation[as_of].max().isoformat(),
                "evaluation_mature_as_of": cutoff.isoformat(),
                "portfolio": {"status": "unmeasured", "reason": "original_engine_callback_not_supplied"}}
        if subset.empty or (kind != "ridge" and evaluation_for_fit.empty):
            windows.append({**item, "status": "insufficient_mature_training_data"})
            continue
        started = time.monotonic()
        model = fit_model(subset, evaluation_for_fit, features=features, kind=kind, params=deepcopy(options), seed=seed)
        if model_sink is not None:
            model_sink(model, int(month))
        item.update({"status": "evaluated_development_material", "model_id": model.model_id,
                     "metadata": model.metadata,
                     "ranking": ranking_metrics(evaluation, model.predict(evaluation), target_column=target,
                                                 as_of_column=as_of, label_available_column=availability_column,
                                                 mature_as_of=cutoff),
                     "calibration": calibration_metrics(evaluation, model.predict_net_return(evaluation),
                                                 target_column=target, as_of_column=as_of,
                                                 label_available_column=availability_column, mature_as_of=cutoff)})
        if portfolio_evaluator is not None:
            item["portfolio"] = portfolio_evaluator(model, subset.copy(), evaluation.copy(), int(month))
        item["seconds"] = float(time.monotonic() - started)
        windows.append(item)
    return {"schema": "ml-selection-learning-curve/v1", "evaluation_kind": "development_validation",
            "independent_final_sample": False, "kind": kind, "features": list(features),
            "fixed_params": options, "seed": int(seed), "parameter_budget_id": budget_hash,
            "fixed_evaluation_rows": int(len(evaluation)), "windows": windows,
            "coverage_claim": "supplied_membership_and_market_sources_only"}
