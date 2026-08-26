"""Phase 5 research governance and out-of-sample validation primitives.

The module deliberately separates *research* data from the final holdout.  It
contains no strategy-specific optimization: callers may rank candidates only
on train/validation windows and may open the holdout once for final admission.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from core.metrics import calculate_cost_sensitivity, calculate_drawdown, calculate_profit_factor


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HoldoutProtocol:
    """Immutable chronological train/validation/final-holdout boundaries."""

    protocol_id: str
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    holdout_start: str
    holdout_end: str
    timezone_name: str = "UTC"
    version: int = 1

    def __post_init__(self) -> None:
        values = [
            pd.Timestamp(self.train_start), pd.Timestamp(self.train_end),
            pd.Timestamp(self.validation_start), pd.Timestamp(self.validation_end),
            pd.Timestamp(self.holdout_start), pd.Timestamp(self.holdout_end),
        ]
        if not (values[0] <= values[1] < values[2] <= values[3] < values[4] <= values[5]):
            raise ValueError("partitions must be chronological, disjoint, and non-empty")
        if self.version < 1 or not self.protocol_id.strip():
            raise ValueError("protocol_id and positive version are required")

    @property
    def fingerprint(self) -> str:
        return _sha256(asdict(self))

    def partition(self, frame: pd.DataFrame | pd.Series) -> dict[str, Any]:
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise TypeError("holdout partition input must use a DatetimeIndex")
        clean = frame.sort_index()
        bounds = {
            "train": (self.train_start, self.train_end),
            "validation": (self.validation_start, self.validation_end),
            "holdout": (self.holdout_start, self.holdout_end),
        }
        result = {name: clean.loc[start:end].copy() for name, (start, end) in bounds.items()}
        if any(value.empty for value in result.values()):
            raise ValueError("each protocol partition must contain observations")
        return result


class HoldoutVault:
    """Persist and enforce a frozen protocol plus a single final-evaluation open."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def freeze(self, protocol: HoldoutProtocol, *, data_hash: str) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "protocol": asdict(protocol),
            "protocol_hash": protocol.fingerprint,
            "data_hash": str(data_hash),
            "status": "frozen",
            "opened_at": None,
        }
        if self.path.exists():
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing != payload:
                raise RuntimeError("holdout is already frozen; create a new protocol version")
            return existing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_canonical(payload) + "\n", encoding="utf-8")
        return payload

    def open_final(self, *, protocol_hash: str, purpose: str) -> dict[str, Any]:
        if any(word in purpose.lower() for word in ("tun", "optim", "select", "train")):
            raise PermissionError("holdout cannot be opened for training, selection, or tuning")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload["protocol_hash"] != protocol_hash:
            raise PermissionError("protocol fingerprint mismatch")
        if payload["status"] != "frozen":
            raise RuntimeError("final holdout has already been opened")
        payload["status"] = "opened_for_final_evaluation"
        payload["opened_at"] = datetime.now(timezone.utc).isoformat()
        payload["purpose"] = purpose
        self.path.write_text(_canonical(payload) + "\n", encoding="utf-8")
        return payload


def walk_forward_splits(
    n_observations: int,
    *,
    train_size: int,
    validation_size: int,
    test_size: int,
    purge_size: int = 0,
    embargo_size: int = 0,
    step: int | None = None,
    expanding: bool = False,
) -> list[dict[str, int]]:
    """Chronological rolling splits with gaps before and after each test window."""
    sizes = (train_size, validation_size, test_size)
    if any(size < 1 for size in sizes) or purge_size < 0 or embargo_size < 0:
        raise ValueError("window sizes must be positive and purge/embargo non-negative")
    step = test_size + embargo_size if step is None else step
    if step < 1:
        raise ValueError("step must be positive")
    splits: list[dict[str, int]] = []
    origin = 0
    while True:
        train_start = 0 if expanding else origin
        train_end = origin + train_size
        validation_start = train_end
        validation_end = validation_start + validation_size
        test_start = validation_end + purge_size
        test_end = test_start + test_size
        if test_end > n_observations:
            break
        splits.append({
            "train_start": train_start,
            "train_end": train_end,
            "validation_start": validation_start,
            "validation_end": validation_end,
            "purge_start": validation_end,
            "purge_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
            "embargo_end": min(n_observations, test_end + embargo_size),
        })
        origin += step
    return splits


def purged_cv_indices(
    event_starts: Sequence[Any],
    event_ends: Sequence[Any],
    *,
    n_splits: int = 5,
    embargo_fraction: float = 0.01,
) -> list[dict[str, list[int]]]:
    """Return folds excluding train events that overlap the test or embargo interval."""
    starts, ends = pd.to_datetime(event_starts), pd.to_datetime(event_ends)
    if len(starts) != len(ends) or len(starts) < n_splits or n_splits < 2:
        raise ValueError("aligned events and at least n_splits observations are required")
    if np.any(ends < starts) or not 0 <= embargo_fraction < 1:
        raise ValueError("invalid event intervals or embargo_fraction")
    ordered = np.argsort(starts)
    blocks = np.array_split(ordered, n_splits)
    embargo_count = int(math.ceil(len(starts) * embargo_fraction))
    folds = []
    all_indices = set(range(len(starts)))
    for block in blocks:
        test = sorted(int(i) for i in block)
        test_start = starts[test].min()
        test_end = ends[test].max()
        after = [int(i) for i in ordered if starts[i] > test_end][:embargo_count]
        excluded = set(test) | set(after)
        train = []
        for index in sorted(all_indices - excluded):
            overlaps = starts[index] <= test_end and ends[index] >= test_start
            if not overlaps:
                train.append(index)
        folds.append({"train": train, "test": test, "embargoed": sorted(after)})
    return folds


def parameter_plateau(
    scores: Mapping[tuple[float, float], float],
    *,
    tolerance: float = 0.10,
) -> dict[str, Any]:
    """Describe the near-optimal 2-D parameter platform rather than a lone maximum."""
    if not scores or not 0 <= tolerance < 1:
        raise ValueError("scores and a tolerance in [0, 1) are required")
    best_point = max(scores, key=lambda point: float(scores[point]))
    best_score = float(scores[best_point])
    threshold = best_score - abs(best_score) * tolerance
    plateau = sorted(point for point, score in scores.items() if float(score) >= threshold)
    x_values = sorted({point[0] for point in scores})
    y_values = sorted({point[1] for point in scores})
    best_x = x_values.index(best_point[0])
    best_y = y_values.index(best_point[1])
    immediate = {
        (x_values[i], y_values[j])
        for i, j in (
            (best_x - 1, best_y), (best_x + 1, best_y),
            (best_x, best_y - 1), (best_x, best_y + 1),
        )
        if 0 <= i < len(x_values) and 0 <= j < len(y_values)
    }
    neighbor_points = sorted(point for point in immediate if point in scores)
    return {
        "best_point": list(best_point),
        "best_score": best_score,
        "threshold": threshold,
        "plateau_points": [list(point) for point in plateau],
        "plateau_fraction": len(plateau) / len(scores),
        "neighbor_points": [list(point) for point in neighbor_points],
        "stable": bool(neighbor_points) and all(point in plateau for point in neighbor_points),
    }


def factor_ablation(
    baseline_scores: Sequence[float],
    scores_without_factor: Mapping[str, Sequence[float]],
    *,
    minimum_stable_gain: float = 0.0,
) -> dict[str, Any]:
    """Keep a factor only when its incremental OOS gain is positive in every window."""
    baseline = np.asarray(baseline_scores, dtype=float)
    if baseline.size == 0 or not np.all(np.isfinite(baseline)):
        raise ValueError("finite baseline window scores are required")
    factors = {}
    for name, raw in scores_without_factor.items():
        ablated = np.asarray(raw, dtype=float)
        if ablated.shape != baseline.shape:
            raise ValueError(f"ablation shape mismatch for {name}")
        gains = baseline - ablated
        factors[name] = {
            "window_gains": gains.tolist(),
            "mean_gain": float(gains.mean()),
            "stable_gain": bool(np.all(gains > minimum_stable_gain)),
            "decision": "retain" if np.all(gains > minimum_stable_gain) else "remove_or_research",
        }
    return {"window_count": int(baseline.size), "factors": factors}


def concentration_stress(pnls: Iterable[float], removals: Sequence[int] = (1, 3, 5, 10)) -> dict[str, Any]:
    values = np.asarray([float(value) for value in pnls if np.isfinite(value)], dtype=float)
    if values.size == 0:
        return {"status": "insufficient", "sample_size": 0, "scenarios": []}
    descending = np.sort(values)[::-1]
    scenarios = []
    for count in removals:
        removed = max(0, min(int(count), len(values)))
        remaining = descending[removed:]
        scenarios.append({
            "removed_top_winners": int(count),
            "effective_removed": removed,
            "remaining_net_pnl": float(remaining.sum()),
            "positive": bool(remaining.sum() > 0),
        })
    return {"status": "ok", "sample_size": int(values.size), "baseline_net_pnl": float(values.sum()), "scenarios": scenarios}


def deflated_sharpe_ratio(
    returns: Iterable[float], *, trials: int, periods_per_year: float = 1.0,
) -> dict[str, Any]:
    """Probability that observed Sharpe exceeds search-inflated expected maximum."""
    values = np.asarray([float(value) for value in returns if np.isfinite(value)], dtype=float)
    if len(values) < 3 or trials < 1:
        return {"status": "insufficient", "sample_size": int(len(values)), "probability": None}
    std = float(values.std(ddof=1))
    if std == 0:
        return {"status": "undefined", "sample_size": int(len(values)), "probability": None}
    observed = float(values.mean() / std * math.sqrt(periods_per_year))
    euler_gamma = 0.5772156649015329
    normal = NormalDist()
    if trials == 1:
        expected_max = 0.0
    else:
        z1 = normal.inv_cdf(1 - 1 / trials)
        z2 = normal.inv_cdf(1 - 1 / (trials * math.e))
        expected_max = (1 - euler_gamma) * z1 + euler_gamma * z2
    centered = values - values.mean()
    skew = float(np.mean(centered ** 3) / (np.std(values) ** 3))
    kurtosis = float(np.mean(centered ** 4) / (np.var(values) ** 2))
    denominator = math.sqrt(max(1e-12, 1 - skew * observed + ((kurtosis - 1) / 4) * observed ** 2))
    statistic = (observed - expected_max) * math.sqrt(len(values) - 1) / denominator
    return {
        "status": "ok",
        "sample_size": int(len(values)),
        "trials": int(trials),
        "observed_sharpe": observed,
        "expected_max_sharpe": expected_max,
        "probability": float(normal.cdf(statistic)),
    }


class ExperimentRegistry:
    """Append-only, content-addressed JSONL registry for every research attempt."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def register(self, *, hypothesis: str, parameters: Mapping[str, Any], data_scope: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict[str, Any]:
        if not hypothesis.strip():
            raise ValueError("hypothesis is required")
        identity = {"hypothesis": hypothesis, "parameters": dict(parameters), "data_scope": dict(data_scope)}
        experiment_id = _sha256(identity)[:16]
        existing = self.records()
        for record in existing:
            if record["experiment_id"] == experiment_id:
                return record
        record = {
            "sequence": len(existing) + 1,
            "experiment_id": experiment_id,
            **identity,
            "metrics": dict(metrics),
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(record) + "\n")
        return record


@dataclass(frozen=True)
class AdmissionThresholds:
    minimum_pf: float = 1.15
    minimum_pf_ci_lower: float = 1.0
    maximum_drawdown: float = 0.20
    required_cost_multiplier: float = 1.5
    concentration_removals: tuple[int, ...] = (5, 10)


def evaluate_holdout_admission(
    *,
    trades: Sequence[Mapping[str, Any]],
    equity: pd.Series,
    benchmark: pd.Series,
    thresholds: AdmissionThresholds = AdmissionThresholds(),
) -> dict[str, Any]:
    """Evaluate the Phase 5 G12-G15 gates without changing any parameters."""
    pnls = [float(trade.get("net_pnl", 0.0)) for trade in trades]
    pf = calculate_profit_factor(pnls, minimum_samples=30, confidence=0.95)
    overlap = pd.concat([pd.Series(equity, name="strategy"), pd.Series(benchmark, name="benchmark")], axis=1).dropna()
    strategy_return = benchmark_return = excess_return = None
    if len(overlap) >= 2 and overlap.iloc[0].ne(0).all():
        strategy_return = float(overlap["strategy"].iloc[-1] / overlap["strategy"].iloc[0] - 1)
        benchmark_return = float(overlap["benchmark"].iloc[-1] / overlap["benchmark"].iloc[0] - 1)
        excess_return = strategy_return - benchmark_return
    drawdown = calculate_drawdown(equity)
    concentration = concentration_stress(pnls, thresholds.concentration_removals)
    costs = calculate_cost_sensitivity(
        trades,
        commission_multipliers=(1.0, thresholds.required_cost_multiplier, 2.0, 3.0),
        slippage_multipliers=(1.0, thresholds.required_cost_multiplier, 2.0, 3.0),
    )
    cost_lookup = {
        (row["commission_multiplier"], row["slippage_multiplier"]): row["net_pnl"]
        for row in costs.get("grid", [])
    }
    stressed_net = cost_lookup.get((thresholds.required_cost_multiplier, thresholds.required_cost_multiplier))
    concentration_pass = all(row["positive"] for row in concentration.get("scenarios", []))
    gates = {
        "G12_positive_oos_edge": bool(
            strategy_return is not None
            and excess_return is not None
            and strategy_return > 0
            and excess_return > 0
        ),
        "G13_pf_significance": bool(pf["value"] is not None and pf["value"] > thresholds.minimum_pf and pf["lower"] is not None and pf["lower"] > thresholds.minimum_pf_ci_lower),
        "G14_not_concentrated": concentration_pass,
        "G15_cost_stress": bool(stressed_net is not None and stressed_net > 0),
        "drawdown_limit": bool(drawdown["max_pct"] is not None and abs(drawdown["max_pct"]) <= thresholds.maximum_drawdown),
    }
    by_strategy = {}
    for name in sorted({str(trade.get("strategy", "UNKNOWN")) for trade in trades}):
        values = [float(trade.get("net_pnl", 0.0)) for trade in trades if str(trade.get("strategy", "UNKNOWN")) == name]
        by_strategy[name] = calculate_profit_factor(values, minimum_samples=30, confidence=0.95)
    return {
        "decision": "admit" if all(gates.values()) else "reject",
        "gates": gates,
        "returns": {"strategy": strategy_return, "benchmark": benchmark_return, "excess": excess_return},
        "profit_factor": pf,
        "profit_factor_by_strategy": by_strategy,
        "drawdown": drawdown,
        "concentration": concentration,
        "cost_stress": costs,
        "thresholds": asdict(thresholds),
    }


def cross_market_validation(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Require evidence across at least two exchanges, timeframes, and regimes."""
    exchanges = {str(item["exchange"]) for item in scenarios}
    timeframes = {str(item["timeframe"]) for item in scenarios}
    regimes = {str(item["regime"]) for item in scenarios}
    positive = [bool(float(item["net_return"]) > 0) for item in scenarios]
    coverage = len(exchanges) >= 2 and len(timeframes) >= 2 and len(regimes) >= 2
    return {
        "scenario_count": len(scenarios),
        "exchanges": sorted(exchanges),
        "timeframes": sorted(timeframes),
        "regimes": sorted(regimes),
        "coverage_complete": coverage,
        "positive_fraction": float(np.mean(positive)) if positive else None,
        "stable_positive_edge": bool(coverage and positive and all(positive)),
    }
