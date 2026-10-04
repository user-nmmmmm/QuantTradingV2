"""CPU selector models and a small Bernoulli policy with inspectable artifacts.

All normalization is fitted on retained training rows. JSON artifacts contain
feature order, preprocessing, training boundaries and model identity; no pickle
or executable objects are used. Policy rewards and episode accounting belong to
the caller's original backtest, not to these model primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


ARTIFACT_VERSION = 1
DEFAULT_TARGET = "label_net_return"


def _features(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(values)
    if not result or any(not isinstance(v, str) or not v for v in result):
        raise ValueError("features must contain nonempty column names")
    if len(set(result)) != len(result):
        raise ValueError("feature names must be unique")
    return result


def _matrix(frame: pd.DataFrame | np.ndarray, features: Sequence[str]) -> np.ndarray:
    if isinstance(frame, pd.DataFrame):
        if frame.columns.has_duplicates:
            raise ValueError("input contains duplicate column names")
        missing = [name for name in features if name not in frame.columns]
        if missing:
            raise ValueError(f"input feature mismatch: missing {missing}")
        try:
            values = frame.loc[:, list(features)].to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError("input features must be numeric") from exc
    else:
        values = np.asarray(frame, dtype=float)
    if values.ndim != 2 or values.shape[1] != len(features):
        raise ValueError(f"input feature mismatch: expected n x {len(features)} matrix")
    return values


def _finite_array(values: Any, size: int, name: str) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must have {size} finite values")
    return result


@dataclass(frozen=True)
class FeatureScaler:
    features: tuple[str, ...]
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, frame: pd.DataFrame | np.ndarray, features: Sequence[str]) -> "FeatureScaler":
        names = _features(features)
        values = _matrix(frame, names)
        if not len(values):
            raise ValueError("cannot fit normalization on empty training data")
        median = np.asarray([
            np.median(column[np.isfinite(column)]) if np.isfinite(column).any() else 0.0
            for column in values.T
        ], dtype=float)
        filled = np.where(np.isfinite(values), values, median)
        mean = filled.mean(axis=0)
        scale = filled.std(axis=0)
        # Constant columns have unit scale, rather than enormous amplified noise.
        scale = np.where(scale > 1e-12, scale, 1.0)
        if not np.isfinite(mean).all() or not np.isfinite(scale).all():
            raise ValueError("training feature values overflow normalization")
        return cls(names, median, mean, scale)

    def transform(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        values = _matrix(frame, self.features)
        with np.errstate(over="ignore", invalid="ignore"):
            result = (np.where(np.isfinite(values), values, self.median) - self.mean) / self.scale
        if not np.isfinite(result).all():
            raise ValueError("input feature values overflow normalization")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {"features": list(self.features), "median": self.median.tolist(),
                "mean": self.mean.tolist(), "scale": self.scale.tolist()}

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "FeatureScaler":
        names = _features(values["features"])
        median = _finite_array(values["median"], len(names), "median")
        mean = _finite_array(values["mean"], len(names), "mean")
        scale = _finite_array(values["scale"], len(names), "scale")
        if (scale <= 0).any():
            raise ValueError("normalization scales must be positive")
        return cls(names, median, mean, scale)


def _identity(payload: Mapping[str, Any]) -> str:
    # Random sampling position is persisted for resumption, but not policy identity.
    body = {k: v for k, v in payload.items() if k not in {"model_id", "rng_state"}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _save_payload(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["model_id"] = _identity(payload)
    # Atomic replacement prevents a cancelled training run leaving a partial JSON.
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    temporary.replace(target)


def _load_payload(path: str | Path) -> dict[str, Any]:
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    if result.get("artifact_version") != ARTIFACT_VERSION:
        raise ValueError("unsupported model artifact version")
    if result.get("model_id") != _identity(result):
        raise ValueError("model artifact identity does not match its contents")
    return result


class RidgeModel:
    kind = "ridge"

    def __init__(self, scaler: FeatureScaler, coefficients: Any, intercept: float,
                 metadata: Mapping[str, Any]):
        self.scaler = scaler
        self.features = scaler.features
        self.coefficients = _finite_array(coefficients, len(self.features), "coefficients")
        self.intercept = float(intercept)
        if not math.isfinite(self.intercept):
            raise ValueError("intercept must be finite")
        self.metadata = dict(metadata)

    def predict_net_return(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        return self.predict(frame)

    def predict(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        result = self.scaler.transform(frame) @ self.coefficients + self.intercept
        if not np.isfinite(result).all():
            raise ValueError("model predictions overflow")
        return result

    def _payload(self) -> dict[str, Any]:
        return {"artifact_version": ARTIFACT_VERSION, "kind": self.kind,
                "features": list(self.features), "scaler": self.scaler.to_dict(),
                "coefficients": self.coefficients.tolist(), "intercept": self.intercept,
                "metadata": self.metadata}

    @property
    def model_id(self) -> str:
        return _identity(self._payload())

    def save(self, path: str | Path) -> None:
        _save_payload(path, self._payload())


class LightGBMModel:
    kind = "lightgbm"

    def __init__(self, scaler: FeatureScaler, booster: Any, metadata: Mapping[str, Any]):
        self.scaler = scaler
        self.features = scaler.features
        self.booster = booster
        self.metadata = dict(metadata)
        if tuple(booster.feature_name()) != self.features:
            raise ValueError("booster feature order differs from artifact features")

    def predict(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        values = self.scaler.transform(frame)
        if not len(values):
            return np.empty(0, dtype=float)
        threads = self.metadata.get("params", {}).get("num_threads", 2)
        if isinstance(threads, bool) or not isinstance(threads, (int, np.integer)) or not 1 <= threads <= 16:
            raise ValueError("prediction num_threads must be an integer between 1 and 16")
        # Artifacts retain the research thread budget. Pass it explicitly so
        # inference never depends on a loaded Booster's/default OpenMP settings.
        result = np.asarray(self.booster.predict(values, num_threads=int(threads)), dtype=float)
        if not np.isfinite(result).all():
            raise ValueError("model predictions must be finite")
        return result

    def _payload(self) -> dict[str, Any]:
        return {"artifact_version": ARTIFACT_VERSION, "kind": self.kind,
                "features": list(self.features), "scaler": self.scaler.to_dict(),
                "booster_text": self.booster.model_to_string(), "metadata": self.metadata}

    def predict_net_return(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        return self.predict(frame)

    @property
    def model_id(self) -> str:
        return _identity(self._payload())

    def save(self, path: str | Path) -> None:
        _save_payload(path, self._payload())


def _lightgbm():
    try:
        import lightgbm
    except ImportError as exc:
        raise ImportError("LightGBM regression/ranking requires the optional lightgbm package; choose ridge or install it") from exc
    return lightgbm


class LambdaRankModel(LightGBMModel):
    """Within-decision order and an independent, training-only return gate.

    Ranking scores have no return units. The Ridge gate sees raw features and
    retained training returns, never ranker predictions or validation labels.
    """

    kind = "lambdarank"

    def __init__(self, scaler: FeatureScaler, booster: Any, gate: RidgeModel,
                 metadata: Mapping[str, Any]):
        super().__init__(scaler, booster, metadata)
        if gate.features != self.features:
            raise ValueError("ranker and return gate feature order differs")
        self.gate = gate
        self.metadata["score_semantics"] = "within_as_of_ranking_score"
        self.metadata["net_return_semantics"] = "training_only_ridge_expected_net_return"

    def predict_net_return(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        return self.gate.predict(frame)

    def _payload(self) -> dict[str, Any]:
        return {**super()._payload(), "return_gate": self.gate._payload()}


def ranking_relevance(labels: Sequence[float], groups: Sequence[int], bins: int = 5) -> np.ndarray:
    """Frozen relevance: floor((minimum rank - 1)/(group size - 1)*(bins-1)).

    Ties share the same integer relevance. Each contiguous group is exactly
    one decision timestamp; no full-sample return quantiles are fitted.
    """
    if isinstance(bins, bool) or not isinstance(bins, (int, np.integer)) or not 2 <= bins <= 30:
        raise ValueError("relevance_bins must be an integer between 2 and 30")
    values = np.asarray(labels, dtype=float)
    sizes = np.asarray(groups)
    if sizes.ndim != 1 or any(isinstance(v, (bool, np.bool_)) or int(v) != v or v <= 0 for v in sizes):
        raise ValueError("ranking groups must be positive integer sizes")
    if values.ndim != 1 or not np.isfinite(values).all() or sizes.sum() != len(values):
        raise ValueError("ranking groups must cover finite labels exactly")
    result = np.zeros(len(values), dtype=np.int32)
    start = 0
    for size in sizes:
        end = start + int(size)
        if size > 1:
            ranks = pd.Series(values[start:end]).rank(method="min").to_numpy() - 1.0
            result[start:end] = np.floor(ranks / (int(size) - 1) * (int(bins) - 1)).astype(np.int32)
        start = end
    return result


def _ranking_grouped(frame: pd.DataFrame, labels: np.ndarray, column: str):
    times = _times(frame, column, "ranking data")
    if times is None:
        raise ValueError("lambdarank requires as_of decision timestamps")
    order = np.argsort(times.astype("int64").to_numpy(), kind="stable")
    ordered = frame.iloc[order]
    sizes = times.iloc[order].groupby(times.iloc[order], sort=False).size().to_numpy(dtype=int)
    return ordered, labels[order], sizes


def _times(frame: pd.DataFrame, column: str, context: str) -> pd.Series | None:
    if column not in frame:
        return None
    values = pd.to_datetime(frame[column], utc=True, errors="coerce")
    if values.isna().any():
        raise ValueError(f"{context} contains missing or invalid {column}")
    return values


def _bound(values: pd.Series | None, operation: str) -> str | None:
    if values is None or values.empty:
        return None
    return getattr(values, operation)().isoformat()


def fit_model(train: pd.DataFrame, validation: pd.DataFrame, *, features: Sequence[str],
              kind: str = "ridge", seed: int = 42,
              params: Mapping[str, Any] | None = None) -> RidgeModel | LightGBMModel | LambdaRankModel:
    """Fit only finite training labels, enforcing label availability at validation.

    ``target_column``, ``as_of_column`` and ``label_available_column`` may be
    supplied in params. When timestamps are present, all retained labels must
    be available by the first validation decision; training decisions must
    strictly precede it. The caller must supply a chronologically purged split.
    """
    if kind not in {"ridge", "lightgbm", "lambdarank"}:
        raise ValueError(f"unknown model kind: {kind}")
    options = dict(params or {})
    names = _features(features)
    target_column = options.pop("target_column", DEFAULT_TARGET)
    as_of_column = options.pop("as_of_column", "as_of")
    available_column = options.pop("label_available_column", "label_available_at")
    if target_column not in train:
        raise ValueError(f"training data missing target column {target_column}")
    _matrix(train, names)
    _matrix(validation, names)
    labels = pd.to_numeric(train[target_column], errors="coerce").to_numpy(dtype=float)
    retained = np.isfinite(labels)
    if kind == "lambdarank":
        all_times = _times(train, as_of_column, "ranking training data")
        if all_times is None:
            raise ValueError("lambdarank requires as_of decision timestamps")
        # Missing members must not silently improve a query's relative label.
        complete = pd.Series(retained, index=train.index).groupby(all_times).transform("all")
        retained &= complete.to_numpy(dtype=bool)
    training = train.loc[retained]
    y = labels[retained]
    if not len(y):
        raise ValueError("training data has no finite labels")
    training_times = _times(training, as_of_column, "training data")
    availability = _times(training, available_column, "training data")
    validation_times = _times(validation, as_of_column, "validation data")
    if training_times is not None and availability is not None:
        if (availability < training_times).any():
            raise ValueError("training label availability precedes its decision")
    if validation_times is not None and not validation_times.empty:
        boundary = validation_times.min()
        if training_times is not None and training_times.max() >= boundary:
            raise ValueError("training decisions overlap validation earliest as_of")
        if availability is not None and availability.max() > boundary:
            raise ValueError("training labels are not available at validation earliest as_of")
    scaler = FeatureScaler.fit(training, names)
    x = scaler.transform(training)
    metadata = {
        "score_semantics": "net_return",
        "seed": int(seed), "target_column": target_column,
        "as_of_column": as_of_column, "label_available_column": available_column,
        "train_rows": int(len(training)), "train_dropped_invalid_labels": int((~retained).sum()),
        "train_min_as_of": _bound(training_times, "min"),
        "train_max_as_of": _bound(training_times, "max"),
        "train_latest_label_available_at": _bound(availability, "max"),
        "validation_min_as_of": _bound(validation_times, "min"),
        "validation_max_as_of": _bound(validation_times, "max"),
    }
    if kind == "ridge":
        alpha = float(options.pop("alpha", options.pop("ridge_alpha", 1.0)))
        if options:
            raise ValueError(f"unknown ridge parameters: {sorted(options)}")
        if not math.isfinite(alpha) or alpha < 0:
            raise ValueError("ridge alpha must be finite and nonnegative")
        design = np.column_stack((np.ones(len(x)), x))
        # Augmented least squares is stable for singular/constant features; the
        # intercept has no penalty and only coefficients receive ridge shrinkage.
        penalty = np.sqrt(alpha) * np.column_stack((np.zeros(len(names)), np.eye(len(names))))
        fitted, *_ = np.linalg.lstsq(np.vstack((design, penalty)),
                                     np.concatenate((y, np.zeros(len(names)))), rcond=None)
        metadata["params"] = {"alpha": alpha}
        return RidgeModel(scaler, fitted[1:], fitted[0], metadata)
    ranker = kind == "lambdarank"
    relevance_bins = options.pop("relevance_bins", 5) if ranker else None
    gate_alpha = options.pop("gate_alpha", 1.0) if ranker else None
    gate = None
    if ranker:
        gate = fit_model(training, validation, features=names, kind="ridge", seed=seed,
                         params={"alpha": gate_alpha, "target_column": target_column,
                                 "as_of_column": as_of_column,
                                 "label_available_column": available_column})
    lightgbm = _lightgbm()
    if target_column not in validation:
        raise ValueError(f"validation data missing target column {target_column}")
    validation_y = pd.to_numeric(validation[target_column], errors="coerce").to_numpy(dtype=float)
    validation_mask = np.isfinite(validation_y)
    train_groups, valid_groups = None, None
    if ranker:
        all_valid_times = _times(validation, as_of_column, "ranking validation data")
        if all_valid_times is None:
            raise ValueError("lambdarank requires as_of decision timestamps")
        complete = pd.Series(validation_mask, index=validation.index).groupby(all_valid_times).transform("all")
        validation_mask &= complete.to_numpy(dtype=bool)
    if not validation_mask.any():
        raise ValueError("LightGBM early stopping requires finite complete validation labels")
    valid_training = validation.loc[validation_mask]
    valid_labels = validation_y[validation_mask]
    if ranker:
        training, y, train_groups = _ranking_grouped(training, y, as_of_column)
        valid_training, valid_labels, valid_groups = _ranking_grouped(valid_training, valid_labels, as_of_column)
        x = scaler.transform(training)
        y = ranking_relevance(y, train_groups, relevance_bins)
        valid_labels = ranking_relevance(valid_labels, valid_groups, relevance_bins)
        if not any(size > 1 for size in train_groups):
            raise ValueError("lambdarank requires at least one training decision with competing candidates")
    # Fixed CPU backend and bounded threads keep laptop jobs predictable.
    threads = int(options.pop("num_threads", 2))
    if not 1 <= threads <= 16:
        raise ValueError("num_threads must be between 1 and 16")
    rounds = int(options.pop("num_boost_round", options.pop("n_estimators", 300)))
    stopping = int(options.pop("early_stopping_rounds", 30))
    if rounds <= 0 or stopping <= 0:
        raise ValueError("boosting and early stopping rounds must be positive")
    controlled = {"device", "device_type", "objective", "seed", "num_threads", "metric",
                  "num_iterations", "num_iteration", "num_trees", "num_round", "num_rounds",
                  "n_iter", "n_jobs", "num_thread", "deterministic"}
    if controlled.intersection(options):
        raise ValueError(f"reserved LightGBM parameters: {sorted(controlled.intersection(options))}")
    settings = {"objective": "lambdarank" if ranker else "regression",
                "metric": "ndcg" if ranker else "l2", "device_type": "cpu",
                "seed": int(seed), "num_threads": threads, "verbosity": -1,
                "learning_rate": 0.03, "num_leaves": 15, "max_depth": 5,
                "min_data_in_leaf": 20, "deterministic": True, "force_col_wise": True,
                **options}
    booster = lightgbm.train(
        settings,
        lightgbm.Dataset(x, label=y, group=train_groups, feature_name=list(names)),
        num_boost_round=rounds,
        valid_sets=[lightgbm.Dataset(scaler.transform(valid_training),
                                    label=valid_labels, group=valid_groups, feature_name=list(names))],
        callbacks=[lightgbm.early_stopping(stopping, verbose=False)],
    )
    metadata["params"] = {**settings, "num_boost_round": rounds, "early_stopping_rounds": stopping}
    metadata["best_iteration"] = int(booster.best_iteration)
    if ranker:
        metadata.update({"score_semantics": "within_as_of_ranking_score",
                         "ranking_group_column": as_of_column,
                         "train_group_sizes": train_groups.tolist(),
                         "validation_group_sizes": valid_groups.tolist(),
                         "relevance": {"bins": int(relevance_bins),
                             "formula": "floor((minimum_rank-1)/(group_size-1)*(bins-1)); singleton=0",
                             "scope": "within_complete_as_of_group", "ties": "minimum_rank"},
                         "return_gate": {"kind": "ridge", "alpha": float(gate_alpha),
                             "fit_scope": "retained_training_rows_only", "model_id": gate.model_id}})
        return LambdaRankModel(scaler, booster, gate, metadata)
    return LightGBMModel(scaler, booster, metadata)


def load_model(path: str | Path) -> RidgeModel | LightGBMModel | LambdaRankModel | "BernoulliPolicy":
    payload = _load_payload(path)
    scaler = FeatureScaler.from_dict(payload["scaler"])
    if tuple(payload["features"]) != scaler.features:
        raise ValueError("artifact feature order differs from scaler features")
    kind = payload.get("kind")
    if kind == "ridge":
        return RidgeModel(scaler, payload["coefficients"], payload["intercept"], payload["metadata"])
    if kind == "lightgbm":
        return LightGBMModel(scaler, _lightgbm().Booster(model_str=payload["booster_text"]), payload["metadata"])
    if kind == "lambdarank":
        body = payload["return_gate"]
        if body.get("kind") != "ridge" or body.get("artifact_version") != ARTIFACT_VERSION:
            raise ValueError("ranker return gate must be a supported Ridge artifact")
        gate_scaler = FeatureScaler.from_dict(body["scaler"])
        if tuple(body["features"]) != gate_scaler.features:
            raise ValueError("return gate feature order differs from scaler features")
        gate = RidgeModel(gate_scaler, body["coefficients"], body["intercept"], body["metadata"])
        if payload["metadata"].get("return_gate", {}).get("model_id") != gate.model_id:
            raise ValueError("ranker return gate identity mismatch")
        return LambdaRankModel(scaler, _lightgbm().Booster(model_str=payload["booster_text"]),
                               gate, payload["metadata"])
    if kind == "bernoulli_policy":
        result = BernoulliPolicy(payload["features"], scaler=scaler, seed=payload["seed"],
                                 weights=payload["weights"], bias=payload["bias"],
                                 probability_epsilon=payload["probability_epsilon"],
                                 max_grad_norm=payload["max_grad_norm"], metadata=payload["metadata"])
        result.update_count = int(payload["update_count"])
        result.rng.bit_generator.state = payload["rng_state"]
        return result
    raise ValueError(f"unknown artifact model kind: {kind}")


def ranking_metrics(frame: pd.DataFrame, predictions: Sequence[float], *,
                    target_column: str = DEFAULT_TARGET, as_of_column: str = "as_of",
                    label_available_column: str = "label_available_at",
                    mature_as_of: Any = None, quantiles: int = 5) -> dict[str, Any]:
    """Cross-sectional diagnostics, excluding labels unavailable at the cutoff.

    Pending members make a cohort pending rather than improving its reported
    rank by selectively removing not-yet-known returns. Tied prediction scores
    remain together in percentile groups. These are label diagnostics, never
    synthetic portfolio returns.
    """
    if quantiles < 2 or int(quantiles) != quantiles:
        raise ValueError("quantiles must be an integer of at least 2")
    if target_column not in frame or as_of_column not in frame:
        raise ValueError("ranking data requires target and as_of columns")
    predictions = _finite_array(predictions, len(frame), "predictions")
    times = _times(frame, as_of_column, "ranking data")
    if label_available_column in frame:
        if mature_as_of is None:
            raise ValueError("mature_as_of is required when label availability is present")
        cutoff = pd.to_datetime(mature_as_of, utc=True, errors="raise")
        if pd.isna(cutoff):
            raise ValueError("mature_as_of must be a valid timestamp")
        availability = pd.to_datetime(frame[label_available_column], utc=True, errors="coerce")
        mature = (availability.notna() & (availability <= cutoff)).to_numpy()
    else:
        mature = np.ones(len(frame), dtype=bool)
    # Never inspect target contents on pending rows.
    labels = np.full(len(frame), np.nan)
    labels[mature] = pd.to_numeric(frame.loc[mature, target_column], errors="coerce").to_numpy(dtype=float)
    paired = mature & np.isfinite(labels)
    data = pd.DataFrame({"as_of": times.to_numpy(), "prediction": predictions,
                         "label": labels, "mature": mature, "paired": paired})
    cohorts = []
    for as_of, cohort in data.groupby("as_of", sort=True):
        item = {"as_of": pd.Timestamp(as_of).isoformat(), "rows": len(cohort),
                "mature_rows": int(cohort["mature"].sum()),
                "paired_rows": int(cohort["paired"].sum()),
                "spearman": None, "top_minus_bottom": None,
                "quantile_returns": {}, "status": "insufficient"}
        if not cohort["mature"].all():
            item["status"] = "pending"
        elif not cohort["paired"].all():
            item["reason"] = "missing_mature_labels"
        elif len(cohort) < 2 or cohort["prediction"].nunique() < 2:
            item["reason"] = "insufficient_or_constant_predictions"
        else:
            ranks = cohort["prediction"].rank(method="average", pct=True)
            buckets = np.minimum(np.ceil(ranks * quantiles).astype(int), quantiles)
            means = cohort.groupby(buckets)["label"].mean()
            item["quantile_returns"] = {str(int(k)): float(v) for k, v in means.items()}
            if cohort["label"].nunique() > 1:
                item["spearman"] = float(cohort["prediction"].rank().corr(cohort["label"].rank()))
            if 1 in means and quantiles in means:
                item["top_minus_bottom"] = float(means.loc[quantiles] - means.loc[1])
            item["status"] = "ok"
        cohorts.append(item)
    ic = [row["spearman"] for row in cohorts if row["spearman"] is not None]
    spreads = [row["top_minus_bottom"] for row in cohorts if row["top_minus_bottom"] is not None]
    return {"rows": len(frame), "mature_rows": int(mature.sum()), "paired_rows": int(paired.sum()),
            "cohorts": cohorts, "mean_spearman": float(np.mean(ic)) if ic else None,
            "mean_top_minus_bottom": float(np.mean(spreads)) if spreads else None,
            "evaluated_cohorts": len(ic), "metric_type": "label_diagnostic_not_portfolio_return"}


def calibration_metrics(frame: pd.DataFrame, predictions: Sequence[float], *,
                        target_column: str = DEFAULT_TARGET, as_of_column: str = "as_of",
                        label_available_column: str = "label_available_at",
                        mature_as_of: Any = None, quantiles: int = 5,
                        gate_threshold: float = 0.0) -> dict[str, Any]:
    """Report return error and score groups; never fit validation calibration.

    Complete mature decision cohorts alone contribute to the statistics. A
    missing outcome is unknown, including an unfilled or unclosed experiment.
    """
    if isinstance(quantiles, bool) or int(quantiles) != quantiles or quantiles < 2:
        raise ValueError("quantiles must be an integer of at least 2")
    if not math.isfinite(float(gate_threshold)):
        raise ValueError("gate_threshold must be finite")
    if target_column not in frame or as_of_column not in frame:
        raise ValueError("calibration data requires target and as_of columns")
    predicted = _finite_array(predictions, len(frame), "predictions")
    times = _times(frame, as_of_column, "calibration data")
    mature = np.ones(len(frame), dtype=bool)
    if label_available_column in frame:
        if mature_as_of is None:
            raise ValueError("mature_as_of is required when label availability is present")
        cutoff = pd.to_datetime(mature_as_of, utc=True, errors="raise")
        if pd.isna(cutoff):
            raise ValueError("mature_as_of must be a valid timestamp")
        availability = pd.to_datetime(frame[label_available_column], utc=True, errors="coerce")
        mature = (availability.notna() & (availability <= cutoff)).to_numpy()
    labels = np.full(len(frame), np.nan)
    labels[mature] = pd.to_numeric(frame.loc[mature, target_column], errors="coerce").to_numpy(dtype=float)
    usable = mature & np.isfinite(labels)
    complete = pd.Series(usable).groupby(pd.Series(times.to_numpy())).transform("all").to_numpy(dtype=bool)
    kept = usable & complete
    diagnostics = pd.DataFrame({"prediction": predicted[kept], "outcome": labels[kept]})
    groups = []
    if len(diagnostics):
        buckets = np.minimum(np.ceil(diagnostics.prediction.rank(method="average", pct=True) * quantiles), quantiles).astype(int)
        for number, group in diagnostics.groupby(buckets, sort=True):
            groups.append({"group": int(number), "rows": int(len(group)),
                           "mean_prediction": float(group.prediction.mean()),
                           "mean_net_return": float(group.outcome.mean()),
                           "bias": float((group.prediction - group.outcome).mean()),
                           "positive_outcome_fraction": float((group.outcome > 0).mean())})
    error = predicted[kept] - labels[kept]
    admitted = kept & (predicted >= float(gate_threshold))
    return {"metric_type": "net_return_calibration_diagnostic_not_portfolio_return",
            "fitted_on_evaluation": False, "rows": len(frame), "mature_rows": int(mature.sum()),
            "known_rows": int(usable.sum()), "evaluated_rows": int(kept.sum()),
            "pending_or_incomplete_cohort_rows": int((~kept).sum()),
            "evaluated_cohorts": int(times.loc[kept].nunique()), "groups": groups,
            "mae": float(np.abs(error).mean()) if len(error) else None,
            "rmse": float(np.sqrt(np.mean(error ** 2))) if len(error) else None,
            "bias": float(error.mean()) if len(error) else None,
            "gate_threshold": float(gate_threshold), "admitted_rows": int(admitted.sum()),
            "admitted_mean_net_return": float(labels[admitted].mean()) if admitted.any() else None}


class BernoulliPolicy:
    """Shared linear candidate gate, optimized by caller-supplied advantages.

    The caller collects a complete episode before invoking update exactly once
    for that batch. Features passed to methods are raw and transformed using
    training-only preprocessing. No automatic advantage centering is applied,
    so a single-observation episode can still supply a learning signal.
    """

    kind = "bernoulli_policy"

    def __init__(self, features: Sequence[str], *, seed: int = 42,
                 scaler: FeatureScaler | None = None, weights: Any = None,
                 bias: float = 0.0, probability_epsilon: float = 1e-6,
                 max_grad_norm: float = 1.0, metadata: Mapping[str, Any] | None = None):
        self.features = _features(features)
        if scaler is not None and scaler.features != self.features:
            raise ValueError("policy feature order differs from scaler features")
        self.scaler = scaler
        self.weights = (np.zeros(len(self.features)) if weights is None else
                        _finite_array(weights, len(self.features), "policy weights"))
        self.bias = float(bias)
        self.probability_epsilon = float(probability_epsilon)
        self.max_grad_norm = float(max_grad_norm)
        if not math.isfinite(self.bias):
            raise ValueError("policy bias must be finite")
        if not 0 < self.probability_epsilon < 0.5:
            raise ValueError("probability_epsilon must be between zero and 0.5")
        if not math.isfinite(self.max_grad_norm) or self.max_grad_norm <= 0:
            raise ValueError("max_grad_norm must be finite and positive")
        self.seed = int(seed)
        self.rng = np.random.default_rng(self.seed)
        self.metadata = dict(metadata or {})
        self.update_count = 0

    def fit_scaler(self, train: pd.DataFrame | np.ndarray) -> "BernoulliPolicy":
        if self.update_count:
            raise ValueError("cannot replace preprocessing after policy updates")
        self.scaler = FeatureScaler.fit(train, self.features)
        return self

    def _inputs(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        if self.scaler is None:
            raise ValueError("fit a training scaler before policy inference")
        return self.scaler.transform(frame)

    def _probabilities(self, x: np.ndarray) -> np.ndarray:
        with np.errstate(over="ignore", invalid="ignore"):
            logits = x @ self.weights + self.bias
        if np.isnan(logits).any():
            raise ValueError("policy logits are invalid")
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -40.0, 40.0)))
        return np.clip(probabilities, self.probability_epsilon, 1 - self.probability_epsilon)

    def predict(self, frame: pd.DataFrame | np.ndarray) -> np.ndarray:
        return self._probabilities(self._inputs(frame))

    def act(self, frame: pd.DataFrame | np.ndarray, deterministic: bool = False, *,
            threshold: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
        if isinstance(threshold, bool) or not math.isfinite(float(threshold)) or not 0 <= threshold <= 1:
            raise ValueError("policy evaluation threshold must be finite and between zero and one")
        probabilities = self.predict(frame)
        gates = probabilities >= threshold if deterministic else self.rng.random(len(probabilities)) < probabilities
        return gates.astype(bool), probabilities

    def update(self, frame: pd.DataFrame | np.ndarray, actions: Sequence[bool],
               advantages: Sequence[float], *, learning_rate: float = 0.01,
               entropy_coef: float = 0.001) -> dict[str, Any]:
        learning_rate, entropy_coef = float(learning_rate), float(entropy_coef)
        if not math.isfinite(learning_rate) or learning_rate <= 0:
            raise ValueError("learning_rate must be finite and positive")
        if not math.isfinite(entropy_coef) or entropy_coef < 0:
            raise ValueError("entropy_coef must be finite and nonnegative")
        x = self._inputs(frame)
        if not len(x):
            raise ValueError("policy update requires a nonempty completed episode batch")
        choices = _finite_array(actions, len(x), "actions")
        if not np.isin(choices, [0.0, 1.0]).all():
            raise ValueError("Bernoulli actions must be zero or one")
        advantage = _finite_array(advantages, len(x), "advantages")
        p = self._probabilities(x)
        log_p, log_q = np.log(p), np.log1p(-p)
        log_probability = choices * log_p + (1 - choices) * log_q
        entropy = -(p * log_p + (1 - p) * log_q)
        # Gradient ascent on advantage * log pi(a|s) + entropy bonus.
        factor = advantage * (choices - p) + entropy_coef * p * (1 - p) * (log_q - log_p)
        with np.errstate(over="ignore", invalid="ignore"):
            gradient = np.concatenate(((x.T @ factor) / len(x), [factor.mean()]))
        if not np.isfinite(gradient).all():
            raise ValueError("policy gradient overflow; reduce input or reward scale")
        largest = float(np.abs(gradient).max())
        scaled_norm = float(np.linalg.norm(gradient / largest)) if largest else 0.0
        norm = min(np.finfo(float).max, largest * scaled_norm) if largest else 0.0
        # Divide before multiplying so even very large finite gradients clip safely.
        clip_factor = min(1.0, (self.max_grad_norm / largest) / scaled_norm) if largest else 1.0
        applied = gradient * clip_factor
        objective = float(np.mean(advantage * log_probability + entropy_coef * entropy))
        mean_advantage = float(advantage.mean())
        if not math.isfinite(objective) or not math.isfinite(mean_advantage):
            raise ValueError("policy reward diagnostics overflow; reduce reward scale")
        new_weights = self.weights + learning_rate * applied[:-1]
        new_bias = self.bias + learning_rate * applied[-1]
        if not np.isfinite(new_weights).all() or not math.isfinite(new_bias):
            raise ValueError("policy update overflow; reduce learning_rate")
        self.weights, self.bias = new_weights, float(new_bias)
        self.update_count += 1
        return {"rows": len(x), "update_count": self.update_count,
                "mean_advantage": mean_advantage, "mean_probability": float(p.mean()),
                "mean_entropy": float(entropy.mean()),
                "objective_before_update": objective,
                "gradient_norm": norm, "gradient_clipped": norm > self.max_grad_norm}

    def _payload(self) -> dict[str, Any]:
        if self.scaler is None:
            raise ValueError("cannot save policy without training preprocessing")
        return {"artifact_version": ARTIFACT_VERSION, "kind": self.kind,
                "features": list(self.features), "scaler": self.scaler.to_dict(),
                "weights": self.weights.tolist(), "bias": self.bias,
                "seed": self.seed, "probability_epsilon": self.probability_epsilon,
                "max_grad_norm": self.max_grad_norm, "update_count": self.update_count,
                "metadata": self.metadata, "rng_state": self.rng.bit_generator.state}

    @property
    def model_id(self) -> str:
        return _identity(self._payload())

    def save(self, path: str | Path) -> None:
        _save_payload(path, self._payload())

    @classmethod
    def load(cls, path: str | Path) -> "BernoulliPolicy":
        result = load_model(path)
        if not isinstance(result, cls):
            raise ValueError("artifact is not a Bernoulli policy")
        return result
