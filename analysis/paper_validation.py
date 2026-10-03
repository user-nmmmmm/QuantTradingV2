"""Selection and event-validation diagnostics for the paper research workstream.

The caller supplies research-only data and retains responsibility for its
provenance, cost basis and holdout boundaries. Nothing here opens a holdout,
fits a strategy, changes admission thresholds or authorizes trading.
"""
from __future__ import annotations

from itertools import combinations
import math
from statistics import NormalDist
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from analysis.research_validation import purged_cv_indices


def _panel(frame: pd.DataFrame) -> tuple[np.ndarray | None, list[str]]:
    errors: list[str] = []
    if not isinstance(frame, pd.DataFrame):
        return None, ["candidate_returns must be a DataFrame"]
    if not isinstance(frame.index, pd.DatetimeIndex):
        errors.append("a DatetimeIndex is required for the common observation axis")
    elif frame.index.hasnans or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        errors.append("timestamps must be present, unique and increasing")
    if not frame.columns.is_unique or any(not isinstance(c, str) or not c for c in frame.columns):
        errors.append("candidate IDs must be unique nonempty strings")
    try:
        values = frame.to_numpy(dtype=float)
    except (TypeError, ValueError):
        return None, errors + ["candidate returns must be numeric"]
    if not np.isfinite(values).all():
        errors.append("missing/nonfinite observations cannot be dropped or filled implicitly")
    return (None if errors else values), errors


def _base(schema: str, **evidence: Any) -> dict[str, Any]:
    return {"schema": schema, "status": "insufficient", "admission_eligible": False,
            "reason": None, **evidence}


def _score(values: np.ndarray, metric: str) -> np.ndarray:
    if metric == "mean":
        return values.mean(axis=0)
    deviations = values.std(axis=0, ddof=1)
    # An undefined Sharpe must never become an infinite winning candidate.
    return np.divide(values.mean(axis=0), deviations,
                     out=np.full(values.shape[1], np.nan), where=deviations > 0)


def cscv_pbo(
    candidate_returns: pd.DataFrame, *, n_groups: int = 8, score: str = "sharpe",
    family_complete: bool = False, minimum_rows_per_group: int = 2,
) -> dict[str, Any]:
    """Enumerate all equal-block half/half CSCV selections and OOS ranks.

    The selected IS maximum uses the first candidate in the supplied column
    order for ties. OOS ties receive their average ascending rank. Logit <= 0
    counts as overfit, so median ties are explicitly conservative. Incomplete
    candidate families may produce a local diagnostic, never admission evidence.
    Rows must be divisible by n_groups: no hidden tail truncation is performed.
    """
    if (isinstance(n_groups, bool) or not isinstance(n_groups, int)
            or n_groups < 2 or n_groups % 2 or minimum_rows_per_group < 2
            or score not in {"sharpe", "mean"}):
        raise ValueError("even n_groups >= 2, minimum_rows_per_group >= 2 and supported score required")
    result = _base("cscv_pbo/v1", probability=None, diagnostic_probability=None,
                   family_complete=family_complete is True, n_groups=n_groups, score=score,
                   tie_policy="first IS column; average OOS rank; logit <= 0 is overfit",
                   scope="declared_local_candidate_family", outcomes=[], invalid_splits=[])
    values, errors = _panel(candidate_returns)
    if errors:
        result.update(status="invalid", reason="; ".join(errors))
        return result
    n, m = values.shape
    result.update(observations=n, candidate_ids=list(candidate_returns.columns), candidates=m,
                  expected_splits=math.comb(n_groups, n_groups // 2))
    if m < 2 or n < n_groups * minimum_rows_per_group:
        result["reason"] = "at least two candidates and enough observations per time group required"
        return result
    if n % n_groups:
        result["reason"] = "CSCV requires equal-sized groups; observations are not divisible by n_groups"
        return result
    blocks = np.split(np.arange(n), n_groups)
    result["group_size"] = len(blocks[0])
    for split_id, train_groups in enumerate(combinations(range(n_groups), n_groups // 2)):
        test_groups = tuple(group for group in range(n_groups) if group not in train_groups)
        train = np.concatenate([blocks[group] for group in train_groups])
        test = np.concatenate([blocks[group] for group in test_groups])
        train_scores, test_scores = _score(values[train], score), _score(values[test], score)
        if not (np.isfinite(train_scores).all() and np.isfinite(test_scores).all()):
            result["invalid_splits"].append({"split_id": split_id,
                                            "reason": "undefined score (e.g. zero-variance Sharpe)"})
            continue
        selected = int(np.argmax(train_scores))
        rank = float(pd.Series(test_scores).rank(method="average", ascending=True).iloc[selected])
        relative_rank = rank / (m + 1)
        logit = math.log(relative_rank / (1 - relative_rank))
        result["outcomes"].append({"split_id": split_id, "train_groups": list(train_groups),
                                   "test_groups": list(test_groups),
                                   "selected_candidate": candidate_returns.columns[selected],
                                   "train_score": float(train_scores[selected]),
                                   "test_score": float(test_scores[selected]),
                                   "oos_rank": rank, "relative_oos_rank": relative_rank,
                                   "logit": logit, "overfit": bool(logit <= 0),
                                   "is_maximum_ties": int(np.sum(train_scores == train_scores[selected])),
                                   "oos_selected_ties": int(np.sum(test_scores == test_scores[selected]))})
    result["computed_splits"] = len(result["outcomes"])
    if result["invalid_splits"]:
        result["reason"] = "one or more CSCV splits have undefined scores; the family was not reduced"
        return result
    probability = float(np.mean([row["overfit"] for row in result["outcomes"]]))
    complete = family_complete is True
    result.update(status="ok" if complete else "diagnostic", diagnostic_probability=probability,
                  probability=probability if complete else None,
                  reason=None if complete else "candidate-family completeness was not attested")
    return result


def cpcv_splits(
    event_starts: Sequence[Any], event_ends: Sequence[Any], *, n_groups: int = 6,
    n_test_groups: int = 2, embargo_fraction: float = .01,
) -> dict[str, Any]:
    """Build C(N,k) multi-group purged folds and C(N-1,k-1) path maps.

    Purging reuses the existing research primitive for each contiguous test
    group, then intersects its permissible training sets. This excludes overlap
    with each test envelope without purging the gap between disjoint test groups.
    Paths consume every (split,test-group) cell exactly once. CPCV is a research
    resampling design and can train on later groups; it is not a causal live replay.
    """
    if (not isinstance(n_groups, int) or isinstance(n_groups, bool) or n_groups < 3
            or not isinstance(n_test_groups, int) or isinstance(n_test_groups, bool)
            or not 2 <= n_test_groups < n_groups or not 0 <= embargo_fraction < 1):
        raise ValueError("n_groups >= 3, 2 <= n_test_groups < n_groups and valid embargo required")
    result = _base("cpcv_plan/v1", n_groups=n_groups, n_test_groups=n_test_groups,
                   embargo_fraction=embargo_fraction, splits=[], paths=[], groups=[],
                   observation_positions=[], event_starts=[], event_ends=[],
                   prediction_contract="split-fitted OOS event predictions; not prefit candidate returns",
                   independence="paths reuse the research history and are not independent observations")
    try:
        starts = pd.DatetimeIndex(pd.to_datetime(event_starts, utc=True))
        ends = pd.DatetimeIndex(pd.to_datetime(event_ends, utc=True))
    except (TypeError, ValueError) as exc:
        result.update(status="invalid", reason=f"invalid timestamps: {exc}")
        return result
    if (len(starts) != len(ends) or starts.hasnans or ends.hasnans
            or np.any(ends < starts) or not starts.is_monotonic_increasing):
        result.update(status="invalid", reason="aligned chronological nonmissing event intervals required")
        return result
    result["observations"] = n = len(starts)
    if n < n_groups:
        result["reason"] = "not enough events for nonempty groups"
        return result
    # Refuse same-time events spanning group boundaries: their arbitrary ordering
    # would create artificial distinct groups and leak contemporaneous decisions.
    groups = [list(map(int, group)) for group in np.array_split(np.arange(n), n_groups)]
    if any(starts[groups[g][-1]] == starts[groups[g + 1][0]] for g in range(n_groups - 1)):
        result.update(status="invalid", reason="same-time events straddle a group boundary")
        return result
    base_folds = purged_cv_indices(starts, ends, n_splits=n_groups,
                                  embargo_fraction=embargo_fraction)
    # Preserve the primitive's group identities when equal-start events are present.
    if any(set(fold["test"]) != set(group) for fold, group in zip(base_folds, groups)):
        result.update(status="invalid", reason="unstable same-time event grouping in purge primitive")
        return result
    result.update(groups=groups, observation_positions=list(range(n)),
                  event_starts=[stamp.isoformat() for stamp in starts],
                  event_ends=[stamp.isoformat() for stamp in ends])
    all_indices = set(range(n))
    for split_id, selected_groups in enumerate(combinations(range(n_groups), n_test_groups)):
        test = set().union(*(set(groups[group]) for group in selected_groups))
        train = set.intersection(*(set(base_folds[group]["train"]) for group in selected_groups))
        embargo = set().union(*(set(base_folds[group]["embargoed"]) for group in selected_groups)) - test
        result["splits"].append({"split_id": split_id, "test_groups": list(selected_groups),
                                 "train": sorted(train), "test": sorted(test),
                                 "embargoed": sorted(embargo),
                                 "purged": sorted(all_indices - test - train - embargo)})
    n_paths = math.comb(n_groups - 1, n_test_groups - 1)
    for path_id in range(n_paths):
        segments = []
        for group_id, group in enumerate(groups):
            containing = [row for row in result["splits"] if group_id in row["test_groups"]]
            segments.append({"group_id": group_id, "split_id": containing[path_id]["split_id"],
                             "positions": group})
        result["paths"].append({"path_id": path_id, "segments": segments})
    empty = [row["split_id"] for row in result["splits"] if not row["train"]]
    result.update(split_count=len(result["splits"]), path_count=n_paths,
                  empty_train_splits=empty, status="insufficient" if empty else "ok",
                  reason="purge/embargo removed every training event in some folds" if empty else None)
    return result


def assemble_cpcv_paths(
    plan: Mapping[str, Any], split_predictions: Mapping[int, pd.Series],
) -> dict[str, Any]:
    """Gather complete event-prediction paths without computing trading returns.

    Every Series must have precisely its split's original integer test positions
    as its index. Missing, duplicate, extra or nonfinite predictions fail closed.
    Caller must fit each split model on that split's permissible training events.
    """
    result = _base("cpcv_event_prediction_paths/v1", paths=[],
                   value_kind="event_predictions_not_strategy_returns",
                   independence="paths are dependent; never multiply sample size by path_count")
    if plan.get("schema") != "cpcv_plan/v1" or plan.get("status") != "ok":
        result["reason"] = "a complete usable CPCV plan is required"
        return result
    required = {row["split_id"] for row in plan["splits"]}
    if set(split_predictions) != required:
        result.update(status="invalid", reason="exactly one OOS prediction Series per split is required")
        return result
    for row in plan["splits"]:
        series = split_predictions[row["split_id"]]
        if (not isinstance(series, pd.Series) or not series.index.is_unique
                or any(not isinstance(i, (int, np.integer)) or isinstance(i, bool) for i in series.index)
                or set(series.index) != set(row["test"])):
            result.update(status="invalid", reason=f"prediction positions mismatch for split {row['split_id']}")
            return result
        try:
            if not np.isfinite(series.to_numpy(dtype=float)).all():
                raise ValueError("nonfinite predictions")
        except (TypeError, ValueError):
            result.update(status="invalid", reason=f"invalid predictions for split {row['split_id']}")
            return result
    for path in plan["paths"]:
        observations = []
        for segment in path["segments"]:
            series = split_predictions[segment["split_id"]]
            for position in segment["positions"]:
                observations.append({"position": position, "event_start": plan["event_starts"][position],
                                     "event_end": plan["event_ends"][position],
                                     "split_id": segment["split_id"], "group_id": segment["group_id"],
                                     "prediction": float(series.loc[position])})
        if [row["position"] for row in observations] != plan["observation_positions"]:
            result.update(status="invalid", reason="path does not cover each chronological event exactly once")
            return result
        result["paths"].append({"path_id": path["path_id"], "observations": observations})
    result.update(status="ok", observations=plan["observations"], path_count=len(result["paths"]))
    return result


def block_bootstrap_indices(
    n_observations: int, *, iterations: int, block_length: int, seed: int = 42,
    method: str = "stationary",
) -> np.ndarray:
    """Common chronological index draws for an entire dependent candidate family.

    Circular mode generalizes trend_portfolio_validation's fixed-block scheme.
    Stationary mode restarts with probability 1/block_length, otherwise advancing
    one circular position (Politis/Romano random geometric blocks).
    """
    if (n_observations < 2 or iterations < 1 or block_length < 1
            or method not in {"stationary", "circular"}):
        raise ValueError("positive bootstrap sizes, >= 2 observations and supported method required")
    rng = np.random.default_rng(seed)
    if method == "circular":
        starts = rng.integers(0, n_observations,
                              size=(iterations, math.ceil(n_observations / block_length)))
        return ((starts[..., None] + np.arange(block_length)) % n_observations).reshape(
            iterations, -1)[:, :n_observations]
    indices = np.empty((iterations, n_observations), dtype=int)
    indices[:, 0] = rng.integers(0, n_observations, size=iterations)
    for position in range(1, n_observations):
        restart = rng.random(iterations) < 1 / block_length
        indices[:, position] = np.where(restart, rng.integers(0, n_observations, size=iterations),
                                       (indices[:, position - 1] + 1) % n_observations)
    return indices


def white_reality_check(
    candidate_returns: pd.DataFrame, benchmark_returns: pd.Series, *, family_complete: bool = False,
    iterations: int = 2000, block_length: int = 5, bootstrap: str = "stationary", seed: int = 42,
    minimum_observations: int = 30,
) -> dict[str, Any]:
    """Unstudentized White family maximum using joint centered block bootstrap.

    d[t,j] is cost-net candidate minus the fixed benchmark on exactly the same
    dates. T = sqrt(n)*max(0,max_j(mean(d_j))); centered draws impose the
    least-favorable null for the entire family. The explicit zero includes the
    benchmark, so a family without positive sample excess has p=1. All candidate
    columns share each bootstrap draw. This is not Hansen's SPA test.
    """
    if (iterations < 100 or block_length < 1 or minimum_observations < 3
            or bootstrap not in {"stationary", "circular"}):
        raise ValueError(">= 100 iterations, positive block length and valid bootstrap required")
    result = _base("white_reality_check/v1", p_value=None, diagnostic_p_value=None,
                   iterations=iterations, block_length=block_length, seed=seed, bootstrap=bootstrap,
                   family_complete=family_complete is True,
                   null="no candidate in the declared family has positive expected benchmark excess",
                   statistic="sqrt(n) * max(0, maximum candidate mean excess)",
                   assumptions="stationary weakly dependent research returns; block-length sensitivity required")
    values, errors = _panel(candidate_returns)
    if not isinstance(benchmark_returns, pd.Series):
        errors.append("benchmark_returns must be a Series")
    elif not isinstance(candidate_returns, pd.DataFrame) or not benchmark_returns.index.equals(candidate_returns.index):
        errors.append("benchmark must use exactly the candidate observation axis; no implicit intersection")
    else:
        try:
            benchmark = benchmark_returns.to_numpy(dtype=float)
            if not np.isfinite(benchmark).all():
                errors.append("benchmark contains missing/nonfinite values")
        except (TypeError, ValueError):
            errors.append("benchmark returns must be numeric")
    if errors:
        result.update(status="invalid", reason="; ".join(errors))
        return result
    n, m = values.shape
    result.update(observations=n, candidates=m, candidate_ids=list(candidate_returns.columns))
    if m < 1 or n < minimum_observations or n < 2 * block_length:
        result["reason"] = "enough observations, candidates and at least two nominal blocks required"
        return result
    excess = values - benchmark[:, None]
    means = excess.mean(axis=0)
    centered = excess - means
    selected = int(np.argmax(means))
    observed = math.sqrt(n) * max(0.0, float(means[selected]))
    draws = block_bootstrap_indices(n, iterations=iterations, block_length=block_length,
                                   seed=seed, method=bootstrap)
    maxima = np.asarray([math.sqrt(n) * max(0.0, float(centered[draw].mean(axis=0).max()))
                         for draw in draws])
    exceedances = int(np.sum(maxima >= observed))
    p_value = (1 + exceedances) / (iterations + 1)
    complete = family_complete is True
    result.update(status="ok" if complete else "diagnostic",
                  reason=None if complete else "candidate-family completeness was not attested",
                  p_value=p_value if complete else None, diagnostic_p_value=p_value,
                  observed_statistic=observed, best_candidate=candidate_returns.columns[selected],
                  candidate_mean_excess={name: float(means[i]) for i, name in enumerate(candidate_returns.columns)},
                  bootstrap_exceedances=exceedances, p_value_rule="(1 + exceedances)/(1 + iterations)",
                  monte_carlo_standard_error=math.sqrt(p_value * (1 - p_value) / (iterations + 1)),
                  bootstrap_max_quantiles={str(q): float(np.quantile(maxima, q)) for q in (.5, .9, .95, .99)})
    return result


def _expected_max_sharpe(trial_std: float, trials: float) -> float:
    if trials <= 1 or trial_std == 0:
        return 0.0
    # The asymptotic approximation is unstable close to 1: interpolate the
    # estimated-count sensitivity between zero at N=1 and the N=2 formula.
    if trials < 2:
        return (trials - 1) * _expected_max_sharpe(trial_std, 2)
    gamma, normal = .5772156649015329, NormalDist()
    return trial_std * ((1 - gamma) * normal.inv_cdf(1 - 1 / trials)
                        + gamma * normal.inv_cdf(1 - 1 / (trials * math.e)))


def deflated_sharpe_evidence(
    candidate_returns: pd.DataFrame, selected_candidate: str, *,
    historical_trials_complete: bool = False, registered_before_results: bool = False,
    declared_total_trials: int | None = None, minimum_observations: int = 30,
    periods_per_year: float = 365,
) -> dict[str, Any]:
    """DSR intermediates plus explicitly estimated dependence sensitivities.

    Actual same-axis trial Sharpes determine cross-trial dispersion. The raw
    trial-count analytic probability uses the Bailey/Lopez de Prado moment
    correction in per-observation units. Eigenvalue participation ratio is only
    a heuristic effective-trial estimate; it never replaces the raw trial count.
    Initial-positive autocorrelation ESS is also a sensitivity estimate, not an
    independent sample certificate. Global search completeness is required to
    populate probability; incomplete histories retain diagnostic_probability.
    """
    if minimum_observations < 3 or not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("minimum_observations >= 3 and positive periods_per_year required")
    result = _base("deflated_sharpe_evidence/v1", probability=None, diagnostic_probability=None,
                   selected_candidate=selected_candidate, declared_total_trials=declared_total_trials,
                   historical_trials_complete=historical_trials_complete is True,
                   registered_before_results=registered_before_results is True,
                   periods_per_year=periods_per_year, sharpe_units="per observation; annualization for display only",
                   inference="analytic non-Normal iid approximation; dependence sensitivities are estimates")
    values, errors = _panel(candidate_returns)
    if errors:
        result.update(status="invalid", reason="; ".join(errors))
        return result
    n, m = values.shape
    result.update(observations=n, observed_trial_count=m)
    if selected_candidate not in candidate_returns.columns:
        result.update(status="invalid", reason="selected candidate is absent from the complete observed family")
        return result
    if n < minimum_observations or m < 1:
        result["reason"] = "insufficient observations or trial evidence"
        return result
    sharpes = _score(values, "sharpe")
    if not np.isfinite(sharpes).all():
        result["reason"] = "undefined trial Sharpe; zero-variance trials cannot be removed from accounting"
        return result
    selected = int(candidate_returns.columns.get_loc(selected_candidate))
    chosen = values[:, selected]
    observed = float(sharpes[selected])
    std = float(sharpes.std(ddof=1)) if m > 1 else 0.0
    centered, variance = chosen - chosen.mean(), float(chosen.var(ddof=0))
    skew = float(np.mean(centered ** 3) / variance ** 1.5)
    kurtosis = float(np.mean(centered ** 4) / variance ** 2)
    sampling_variance = 1 - skew * observed + (kurtosis - 1) * observed ** 2 / 4
    result.update(trial_period_sharpes={name: float(sharpes[i]) for i, name in enumerate(candidate_returns.columns)},
                  trial_sharpe_sample_std=std, trial_sharpe_sample_variance=std ** 2,
                  observed_period_sharpe=observed, observed_annualized_sharpe=observed * math.sqrt(periods_per_year),
                  skewness=skew, kurtosis=kurtosis, kurtosis_convention="raw moment; Gaussian=3",
                  sharpe_sampling_variance_factor=sampling_variance)
    if sampling_variance <= 0 or not math.isfinite(sampling_variance):
        result["reason"] = "nonpositive or nonfinite Sharpe sampling variance"
        return result
    if m > 1:
        corr = np.corrcoef(values, rowvar=False)
        eigenvalues = np.maximum(np.linalg.eigvalsh(corr), 0)
        effective_trials = float(np.clip(eigenvalues.sum() ** 2 / np.sum(eigenvalues ** 2), 1, m))
        average_correlation = float((corr.sum() - m) / (m * (m - 1)))
    else:
        corr, effective_trials, average_correlation = np.ones((1, 1)), 1.0, None
    max_lag = min(n - 2, max(1, int(n ** (1 / 3))))
    autocorrelations = []
    for lag in range(1, max_lag + 1):
        acf = float(np.dot(centered[:-lag], centered[lag:]) / np.dot(centered, centered))
        if acf <= 0:
            break
        autocorrelations.append(acf)
    time_ess = float(np.clip(n / (1 + 2 * sum(autocorrelations)), 2, n))
    def scenario(trials: float, observations: float) -> dict[str, Any]:
        expected = _expected_max_sharpe(std, trials)
        probability = NormalDist().cdf((observed - expected) * math.sqrt(observations - 1)
                                      / math.sqrt(sampling_variance))
        return {"trials": trials, "observations": observations,
                "expected_max_period_sharpe": expected,
                "expected_max_annualized_sharpe": expected * math.sqrt(periods_per_year),
                "probability": probability}
    raw = scenario(float(m), float(n))
    complete = (historical_trials_complete is True and registered_before_results is True
                and isinstance(declared_total_trials, int) and not isinstance(declared_total_trials, bool)
                and declared_total_trials == m)
    result.update(status="ok" if complete else "diagnostic", search_accounting_complete=complete,
                  reason=None if complete else "complete preregistered global search with exact trial count required",
                  probability=raw["probability"] if complete else None,
                  diagnostic_probability=raw["probability"], expected_max_period_sharpe=raw["expected_max_period_sharpe"],
                  candidate_correlation=corr.tolist(), mean_off_diagonal_correlation=average_correlation,
                  effective_trial_count_estimate=effective_trials,
                  effective_trial_count_method="eigenvalue participation ratio; heuristic, not proof of independent trials",
                  temporal_effective_observations_estimate=time_ess,
                  temporal_estimate_method="initial-positive ACF sum, max lag floor(n^(1/3)); heuristic",
                  positive_autocorrelations=autocorrelations,
                  sensitivity={"raw_trial_count_iid": raw,
                               "estimated_effective_trials_iid": scenario(effective_trials, float(n)),
                               "raw_trial_count_estimated_time_ess": scenario(float(m), time_ess),
                               "estimated_trial_and_time_ess": scenario(effective_trials, time_ess)})
    return result
