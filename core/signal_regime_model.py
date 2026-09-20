"""Frozen causal outcome-distribution geometry for research-only regime axes.

This is an engineering extension of the paper's one-dimensional Wasserstein
geometry: a trailing-feature neighbourhood supplies an empirical quantile
function. It is not the paper's histogram implementation or a calibrated
state-probability estimator. No test-period outcome is an inference input.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from numbers import Real

import numpy as np
import pandas as pd

from core.signal_observation_types import canonical, fingerprint
from core.timeframes import as_utc_timestamp, timeframe_delta


AXIS_FEATURES = {"trend": "return_12", "efficiency": "efficiency_ratio_10",
                 "volatility": "volatility_ratio_8_48"}
_SCOPE = "independent_fixed_notional_signal_diagnostic"
_TEMPERATURE_FLOOR = 1e-9


def _number(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _timestamp(value, name):
    if value is None or isinstance(value, (bool, int, float)):
        raise ValueError(f"invalid {name}")
    try:
        result = as_utc_timestamp(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"invalid {name}") from error
    if pd.isna(result):
        raise ValueError(f"invalid {name}")
    return result


def _quantiles(value, name):
    try:
        array = np.asarray(value)
        if array.dtype.kind not in "iuf" or array.ndim != 1 or not array.size:
            raise ValueError(f"{name} must be a finite nonempty quantile vector")
        array = array.astype(float)
    except (TypeError, OverflowError) as error:
        raise ValueError(f"invalid {name}") from error
    if not np.isfinite(array).all() or np.any(array[1:] < array[:-1]):
        raise ValueError(f"{name} must be finite and nondecreasing")
    return array


def wasserstein_squared(left, right):
    """Midpoint-grid approximation to integral (Q_left - Q_right)**2 du."""
    left = _quantiles(left, "left quantiles")
    right = _quantiles(right, "right quantiles")
    if left.shape != right.shape:
        raise ValueError("quantile vectors must have equal lengths")
    try:
        with np.errstate(over="raise", invalid="raise"):
            return float(np.mean(np.square(left - right)))
    except FloatingPointError as error:
        raise ValueError("Wasserstein distance exceeds finite numeric range") from error


def quantile_barycenter(vectors, weights=None):
    """The one-dimensional squared-Wasserstein barycenter on a shared grid."""
    rows = [_quantiles(vector, "barycenter quantiles") for vector in vectors]
    if not rows or len({len(row) for row in rows}) != 1:
        raise ValueError("barycenter requires equal nonempty quantile vectors")
    if weights is None:
        normalized = np.full(len(rows), 1.0 / len(rows))
    else:
        numbers = np.array([_number(value, "barycenter weight") for value in weights])
        if len(numbers) != len(rows) or np.any(numbers < 0) or not np.any(numbers > 0):
            raise ValueError("invalid barycenter weights")
        normalized = numbers / numbers.max()
        normalized /= normalized.sum()
    result = np.sum(np.stack(rows) * normalized[:, None], axis=0)
    if not np.isfinite(result).all():
        raise ValueError("barycenter exceeds finite numeric range")
    return result.tolist()


def soft_memberships(distances, temperature):
    """Stable exp(-W2_squared / temperature); these are uncalibrated weights."""
    distances = np.array([_number(value, "distance") for value in distances])
    temperature = _number(temperature, "temperature")
    if not len(distances) or np.any(distances < 0) or temperature <= 0:
        raise ValueError("distances must be nonnegative and temperature positive")
    with np.errstate(over="ignore", under="ignore"):
        exponent = (distances - distances.min()) / temperature
        weights = np.exp(-exponent)
    return (weights / weights.sum()).tolist()


def _features(candidate):
    context = candidate.get("context")
    if not isinstance(context, dict):
        raise ValueError("candidate requires context")
    source = context.get("features")
    if source is None:
        source = {}
    if not isinstance(source, dict):
        raise ValueError("features must be a mapping")
    values = []
    for axis, feature in AXIS_FEATURES.items():
        raw = source.get(feature)
        value = None if raw is None else _number(raw, feature)
        if value is not None and ((axis == "efficiency" and not 0 <= value <= 1 + 1e-12)
                                  or (axis == "volatility" and value < 0)):
            raise ValueError(f"{feature} outside its valid range")
        values.append(value)
    return tuple(values)


def _book(candidate, horizon):
    context = candidate.get("context")
    if not isinstance(context, dict):
        raise ValueError("candidate requires context")
    names = (candidate.get("strategy"), candidate.get("signal_version"),
             candidate.get("direction"), context.get("timeframe"),
             context.get("snapshot_version"))
    if any(not isinstance(value, str) or not value for value in names):
        raise ValueError("candidate book identifiers must be nonempty strings")
    if names[2] not in {"long", "short"}:
        raise ValueError("invalid candidate direction")
    timeframe_delta(names[3])
    if type(horizon) is not int or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    return (*names, horizon)


@dataclass(frozen=True)
class _Record:
    candidate_id: str
    symbol: str
    entry_at: str
    label_at: str
    value: float
    features: tuple


def _records(observations, cutoff):
    if not isinstance(observations, (list, tuple)):
        raise ValueError("observations must be a list of candidate/outcome mappings")
    records = []
    book = None
    seen = set()
    ignored = {"not_matured": 0, "execution_flags": 0}
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("observations must contain candidate/outcome mappings")
        candidate, outcome = observation.get("candidate"), observation.get("outcome")
        if not isinstance(candidate, dict) or not isinstance(outcome, dict):
            raise ValueError("observations require candidate and outcome mappings")
        identity = candidate.get("candidate_id")
        symbol = candidate.get("symbol")
        if not isinstance(identity, str) or not identity or not isinstance(symbol, str) or not symbol:
            raise ValueError("candidate identity and symbol are required")
        if identity in seen:
            raise ValueError("duplicate training candidate")
        seen.add(identity)
        current_book = _book(candidate, outcome.get("horizon_bars"))
        if book is not None and book != current_book:
            raise ValueError("mixed training books")
        book = current_book
        for key in ("candidate_id", "symbol", "strategy", "direction"):
            if candidate.get(key) != outcome.get(key):
                raise ValueError(f"candidate/outcome {key} mismatch")
        entry = _timestamp(candidate["context"].get("available_at"), "candidate available_at")
        if entry >= cutoff:
            raise ValueError("training entry must be strictly before cutoff")
        if outcome.get("scope") != _SCOPE:
            raise ValueError("unsupported outcome scope")
        if outcome.get("status") != "matured":
            if not str(outcome.get("status", "")).startswith("censored_"):
                raise ValueError("invalid outcome status")
            ignored["not_matured"] += 1
            continue
        label = _timestamp(outcome.get("available_at"), "label available_at")
        if label >= cutoff:
            raise ValueError("training label must be strictly before cutoff")
        if label < entry + timeframe_delta(current_book[3]) * current_book[-1]:
            raise ValueError("label available before horizon maturity")
        value = _number(outcome.get("net_return_bps"), "net_return_bps")
        flags = outcome.get("execution_flags")
        if not isinstance(flags, list):
            raise ValueError("execution_flags must be a list")
        if flags:
            ignored["execution_flags"] += 1
            continue
        records.append(_Record(identity, symbol, entry.isoformat(), label.isoformat(),
                               value, _features(candidate)))
    records.sort(key=lambda record: (record.entry_at, record.candidate_id))
    return tuple(records), book, ignored


def _neighbour_quantiles(features, values, feature, neighbors, grid, exclude=None):
    """Include all exact distance ties at the kth boundary, excluding self."""
    available = np.arange(len(features))
    if exclude is not None:
        available = available[available != exclude]
    if not len(available):
        raise ValueError("neighbourhood requires at least one other observation")
    with np.errstate(over="raise", invalid="raise"):
        try:
            distances = np.abs(features[available] - feature)
        except FloatingPointError as error:
            raise ValueError("feature distances exceed finite numeric range") from error
    boundary = np.partition(distances, min(neighbors, len(available)) - 1)[
        min(neighbors, len(available)) - 1]
    selected = available[distances <= boundary]
    result = np.quantile(values[selected], grid, method="linear")
    if not np.isfinite(result).all():
        raise ValueError("empirical quantiles exceed finite numeric range")
    return result


def _distance_matrix(vectors, centers):
    try:
        with np.errstate(over="raise", invalid="raise"):
            result = np.mean((vectors[:, None, :] - centers[None, :, :]) ** 2, axis=2)
    except FloatingPointError as error:
        raise ValueError("Wasserstein distance exceeds finite numeric range") from error
    return result


def _kmeans(vectors, clusters, n_init, max_iter, seed):
    """Seeded quantile-space K-means++; failed empty-cluster runs are skipped."""
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(n_init):
        centers = [vectors[int(rng.integers(len(vectors)))].copy()]
        while len(centers) < clusters:
            nearest = _distance_matrix(vectors, np.stack(centers)).min(axis=1)
            maximum = nearest.max()
            if maximum <= 0:
                break
            probability = nearest / maximum
            probability /= probability.sum()
            centers.append(vectors[int(rng.choice(len(vectors), p=probability))].copy())
        if len(centers) < clusters:
            continue
        centers = np.stack(centers)
        valid = True
        for _ in range(max_iter):
            distances = _distance_matrix(vectors, centers)
            labels = distances.argmin(axis=1)
            # A collapsed run cannot claim a fitted state with zero support.
            if any(not np.any(labels == label) for label in range(clusters)):
                valid = False
                break
            updated = np.stack([quantile_barycenter(vectors[labels == label])
                                for label in range(clusters)])
            if np.array_equal(updated, centers):
                centers = updated
                break
            centers = updated
        if not valid:
            continue
        centers = np.array(sorted(centers.tolist()))
        distances = _distance_matrix(vectors, centers)
        labels = distances.argmin(axis=1)
        support = tuple(int(np.sum(labels == label)) for label in range(clusters))
        if min(support) == 0 or len(np.unique(centers, axis=0)) < clusters:
            continue
        residuals = distances[np.arange(len(vectors)), labels]
        inertia = float(residuals.mean())
        key = (inertia, tuple(centers.ravel()))
        if best is None or key < best[0]:
            best = (key, centers, support, residuals)
    return None if best is None else best[1:]


@dataclass(frozen=True)
class _Axis:
    name: str
    feature: str
    status: str
    reason: str
    features: tuple = ()
    values: tuple = ()
    centers: tuple = ()
    support: tuple = ()
    temperature: float | None = None
    inertia: float | None = None
    separation: float | None = None


@dataclass(frozen=True)
class RegimeModel:
    """Immutable fitted model. Every returned mapping is a fresh copy."""
    _cutoff: str
    _book_key: tuple | None
    _records: tuple
    _axes: tuple
    _spec_json: str
    _audit_json: str

    @classmethod
    def fit(cls, observations, *, cutoff, clusters=2, neighbors=20, quantiles=21,
            min_samples=40, n_init=3, max_iter=50, seed=20260918):
        parameters = {"clusters": clusters, "neighbors": neighbors, "quantiles": quantiles,
                      "min_samples": min_samples, "n_init": n_init, "max_iter": max_iter,
                      "seed": seed}
        for name, value in parameters.items():
            floor = 0 if name == "seed" else 2 if name in {"clusters", "quantiles", "min_samples"} else 1
            if type(value) is not int or value < floor:
                raise ValueError(f"{name} must be an integer >= {floor}")
        if min_samples < clusters:
            raise ValueError("min_samples must be >= clusters")
        cutoff = _timestamp(cutoff, "training cutoff")
        records, book, ignored = _records(observations, cutoff)
        grid = (np.arange(quantiles) + .5) / quantiles
        axes = []
        for index, (name, feature) in enumerate(AXIS_FEATURES.items()):
            usable = [record for record in records if record.features[index] is not None]
            features = np.array([record.features[index] for record in usable], dtype=float)
            values = np.array([record.value for record in usable], dtype=float)
            base = {"name": name, "feature": feature, "features": tuple(features),
                    "values": tuple(values)}
            if len(usable) < min_samples:
                axes.append(_Axis(**base, status="unavailable", reason="insufficient_feature_samples"))
                continue
            if len(np.unique(features)) < clusters:
                axes.append(_Axis(**base, status="unavailable", reason="insufficient_distinct_features"))
                continue
            vectors = np.stack([_neighbour_quantiles(features, values, value, neighbors,
                                                     grid, exclude=number)
                                for number, value in enumerate(features)])
            if len(np.unique(vectors, axis=0)) < clusters:
                axes.append(_Axis(**base, status="unavailable", reason="insufficient_distinct_distributions"))
                continue
            fitted = _kmeans(vectors, clusters, n_init, max_iter, seed + index)
            if fitted is None:
                axes.append(_Axis(**base, status="unavailable", reason="collapsed_clusters"))
                continue
            centers, support, residuals = fitted
            separation = min(wasserstein_squared(left, right)
                             for number, left in enumerate(centers) for right in centers[number + 1:])
            axes.append(_Axis(**base, status="available", reason="fitted",
                              centers=tuple(tuple(float(value) for value in row) for row in centers),
                              support=support, temperature=max(float(np.median(residuals)), _TEMPERATURE_FLOOR),
                              inertia=float(residuals.mean()), separation=separation))
        spec = {**parameters, "quantile_grid": grid.tolist(), "quantile_method": "linear",
                "temperature_floor_bps2": _TEMPERATURE_FLOOR,
                "training_neighbours": "leave_self_out_all_boundary_ties",
                "inference_neighbours": "frozen_training_samples_all_boundary_ties",
                "distance": "mean_squared_quantile_distance",
                "scope": "feature_conditioned_empirical_distribution_engineering_extension"}
        return cls(cutoff.isoformat(), book, records, tuple(axes), canonical(spec), canonical(ignored))

    def _inference(self, candidate, as_of):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("context"), dict):
            raise ValueError("candidate requires context")
        entry = _timestamp(candidate["context"].get("available_at"), "candidate available_at")
        clock = _timestamp(as_of, "inference as_of")
        if _timestamp(self._cutoff, "training cutoff") > entry:
            raise ValueError("fitted cutoff must not be later than candidate entry")
        if clock < entry:
            raise ValueError("inference as_of must not precede candidate entry")
        if self._book_key is not None and _book(candidate, self._book_key[-1]) != self._book_key:
            raise ValueError("candidate does not match fitted book")
        values = _features(candidate)
        spec = json.loads(self._spec_json)
        memberships, diagnostics = {}, {}
        for index, axis in enumerate(self._axes):
            if axis.status != "available" or values[index] is None:
                memberships[axis.name] = {"unknown": 1.0}
                diagnostics[axis.name] = {"status": "unavailable", "entropy": None,
                    "reason": axis.reason if axis.status != "available" else "missing_feature"}
                continue
            quantile = _neighbour_quantiles(np.array(axis.features), np.array(axis.values),
                values[index], spec["neighbors"], np.array(spec["quantile_grid"]))
            distances = [wasserstein_squared(quantile, center) for center in axis.centers]
            probability = soft_memberships(distances, axis.temperature)
            memberships[axis.name] = {f"state_{number}": value for number, value in enumerate(probability)}
            entropy = -sum(value * math.log(value) for value in probability if value > 0)
            diagnostics[axis.name] = {"status": "available", "entropy": entropy,
                                     "distances_bps2": distances,
                                     "empirical_quantiles_bps": quantile.tolist()}
        return memberships, diagnostics

    def memberships(self, candidate, *, as_of):
        return self._inference(candidate, as_of)[0]

    def describe(self, candidate, *, as_of):
        memberships, axes = self._inference(candidate, as_of)
        return {"memberships": memberships, "axes": axes,
                "probability_interpretation": "uncalibrated_soft_memberships"}

    def manifest(self):
        training = [{"candidate_id": record.candidate_id, "symbol": record.symbol,
                     "entry_at": record.entry_at, "label_available_at": record.label_at,
                     "net_return_bps": record.value, "features": list(record.features)}
                    for record in self._records]
        axes = {axis.name: {"feature": axis.feature, "status": axis.status, "reason": axis.reason,
                "sample_count": len(axis.features), "distinct_features": len(set(axis.features)),
                "centroids": [list(center) for center in axis.centers],
                "state_ids": [f"state_{number}" for number in range(len(axis.centers))],
                "support": list(axis.support), "temperature_bps2": axis.temperature,
                "inertia_bps2": axis.inertia, "separation_bps2": axis.separation}
                for axis in self._axes}
        payload = {"schema": "signal_regime_model/v1", "training_cutoff": self._cutoff,
                   "book": list(self._book_key) if self._book_key is not None else None,
                   "fit_spec": json.loads(self._spec_json), "axes": axes,
                   "training_sample_count": len(training), "training_data_digest": fingerprint(training),
                   "training_latest_label_at": max((record.label_at for record in self._records), default=None),
                   "ignored": json.loads(self._audit_json),
                   "probability_interpretation": "uncalibrated_soft_memberships"}
        return {**payload, "model_id": fingerprint(payload)}
