"""Reproducible out-of-sample and parameter-stability validation workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd

from core.metrics import (
    benjamini_hochberg,
    bootstrap_return_distribution,
    monte_carlo_trade_sequence,
    train_test_split_returns,
    walk_forward_windows,
)


@dataclass(frozen=True)
class ValidationConfig:
    train_fraction: float = 0.7
    walk_forward_train: int = 180
    walk_forward_test: int = 60
    bootstrap_samples: int = 1000
    monte_carlo_samples: int = 1000
    seed: int = 42
    fdr: float = 0.05


def validate_parameter_candidates(
    returns_by_candidate: Mapping[str, pd.Series],
    *,
    p_values: Iterable[float],
    config: ValidationConfig = ValidationConfig(),
) -> dict[str, Any]:
    """Rank on training data and report untouched OOS plus uncertainty evidence."""
    if not returns_by_candidate:
        raise ValueError("at least one parameter candidate is required")
    splits = {
        name: train_test_split_returns(series, config.train_fraction)
        for name, series in returns_by_candidate.items()
    }
    training_scores = {
        name: float(parts["train"].mean()) if not parts["train"].empty else float("-inf")
        for name, parts in splits.items()
    }
    selected = max(training_scores, key=lambda name: training_scores[name])
    oos = splits[selected]["test"]
    windows = walk_forward_windows(
        len(returns_by_candidate[selected]),
        train_size=config.walk_forward_train,
        test_size=config.walk_forward_test,
    )
    return {
        "selected_on": "train_only",
        "selected_candidate": selected,
        "training_scores": training_scores,
        "oos_mean_return": None if oos.empty else float(oos.mean()),
        "oos_sample_size": int(len(oos)),
        "walk_forward_windows": windows,
        "bootstrap": bootstrap_return_distribution(
            oos, n_samples=config.bootstrap_samples, seed=config.seed
        ),
        "monte_carlo": monte_carlo_trade_sequence(
            oos, n_simulations=config.monte_carlo_samples, seed=config.seed
        ),
        "multiple_testing": benjamini_hochberg(p_values, fdr=config.fdr),
    }


def neighborhood_stability(
    scores: Mapping[tuple[float, ...], float],
    selected: tuple[float, ...],
    tolerance: float = 0.10,
) -> dict[str, Any]:
    """Require the selected point to be supported by nearby parameter values."""
    if selected not in scores:
        raise KeyError("selected parameter tuple is absent")
    center = float(scores[selected])
    neighbors = [
        float(score) for point, score in scores.items()
        if point != selected and sum(a != b for a, b in zip(point, selected)) == 1
    ]
    stable = bool(neighbors) and all(score >= center * (1 - tolerance) for score in neighbors)
    return {"selected_score": center, "neighbor_scores": neighbors, "stable": stable}
