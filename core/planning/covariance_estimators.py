"""Daily covariance estimators with explicit targets, units and degeneracies.

Constant-correlation intensity follows Ledoit/Wolf (June 2003), Appendix A/B:
1/T centered moments determine intensity, then the matrix is converted to the
project's sample ddof=1 convention. It is not scaled-identity shrinkage.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

ESTIMATORS = ("sample", "fixed_diag_020", "lw_constant_correlation")
ALGORITHM_VERSION = "daily-covariance/v1"


@dataclass(frozen=True)
class CovarianceEstimate:
    covariance: np.ndarray
    audit: dict


def _constant_correlation_target(centered, sample):
    """Compute the MLE shrinkage moments, then convert the target to ddof=1."""
    observations, assets = centered.shape
    target = sample.copy()
    intensity, average_correlation = 0.0, None
    degeneration, moments = None, {}
    mle = centered.T @ centered / observations
    variance = np.diag(mle)
    if assets == 1:
        degeneration = "single_asset_target_equals_sample"
    elif (variance <= 0).any():
        degeneration = "zero_variance_sample_fallback"
    else:
        deviations = np.sqrt(variance)
        scales = np.outer(deviations, deviations)
        # Sum only off-diagonal pairs; diagonal is not estimated correlation.
        mask: np.ndarray = ~np.eye(assets, dtype=bool)
        average_correlation = float((mle / scales)[mask].mean())
        mle_target = average_correlation * scales
        np.fill_diagonal(mle_target, variance)
        gamma = float(np.sum((mle - mle_target) ** 2))
        target = mle_target * observations / (observations - 1)
        # A relative test is scale invariant: multiplying returns by c must
        # not change whether the denominator is degenerate.
        scale_squared = float(np.sum(mle**2))
        if assets == 2 or gamma <= 1e-24 * max(scale_squared, float(np.finfo(float).tiny)):
            target = sample.copy()
            degeneration = "constant_correlation_target_equals_sample"
        else:
            squared = centered**2
            fourth = squared.T @ squared / observations
            pi_matrix = fourth - mle**2
            pi = float(pi_matrix.sum())
            theta = (centered**3).T @ centered / observations - variance[:, None] * mle
            np.fill_diagonal(theta, 0.0)
            rho = float(
                np.trace(pi_matrix)
                + average_correlation * np.sum((deviations[None, :] / deviations[:, None]) * theta)
            )
            raw_intensity = (pi - rho) / (gamma * observations)
            intensity = float(np.clip(raw_intensity, 0.0, 1.0))
            moments = {
                "pi": pi,
                "rho": rho,
                "gamma": gamma,
                "unclipped_intensity": float(raw_intensity),
            }
    return target, intensity, average_correlation, degeneration, moments


def _covariance_result(
    matrix,
    estimator_id,
    fixed_shrinkage,
    observations,
    names,
    intensity,
    average_correlation,
    eigenvalues,
    degeneration,
    moments,
):
    """Keep estimator audit semantics separate from the numerical calculation."""
    return CovarianceEstimate(
        matrix,
        {
            "schema": ALGORITHM_VERSION,
            "estimator_id": (
                estimator_id
                if estimator_id != "fixed_diag_020" or fixed_shrinkage == 0.20
                else "fixed_diag_custom"
            ),
            "estimator_parameters": {"diagonal_shrinkage": float(fixed_shrinkage)}
            if estimator_id == "fixed_diag_020"
            else {},
            "algorithm_version": "lw-constant-correlation-2003-mle-intensity-ddof1/v1"
            if estimator_id == "lw_constant_correlation"
            else ALGORITHM_VERSION,
            "observations": observations,
            "assets": names,
            "centering": "sample_mean",
            "ddof": 1,
            "unit": "simple_daily_return_squared",
            "periods_per_year_applied": 0,
            "target": "constant_correlation"
            if estimator_id == "lw_constant_correlation"
            else "diagonal"
            if estimator_id == "fixed_diag_020"
            else "sample",
            "intensity_moment_denominator": "T"
            if estimator_id == "lw_constant_correlation"
            else None,
            "outgoing_conversion": "multiply MLE matrix by T/(T-1)",
            "shrinkage_intensity": intensity,
            "average_correlation": average_correlation,
            "minimum_eigenvalue": float(eigenvalues.min()),
            "degeneracy": degeneration,
            "moments": moments,
            "limitations": [
                "iid finite-fourth-moment intensity approximation; serial dependence is not calibrated here",
                "daily output; annualization and forecast horizon are caller boundaries",
            ],
        },
    )


def estimate_covariance(returns, *, estimator_id="fixed_diag_020", fixed_shrinkage=0.20):
    """Estimate from a common complete prefix of simple daily asset returns.

    No missing rows are dropped. The caller must supply its visible information
    set; this function never downloads data or infers publication availability.
    Zero-variance constant-correlation input explicitly falls back to sample.
    """
    if estimator_id not in ESTIMATORS:
        raise ValueError("unknown covariance estimator")
    if (
        isinstance(fixed_shrinkage, bool)
        or not math.isfinite(float(fixed_shrinkage))
        or not 0 <= fixed_shrinkage <= 1
    ):
        raise ValueError("fixed_shrinkage must be in [0, 1]")
    if isinstance(returns, pd.DataFrame):
        if (
            returns.index.has_duplicates
            or not returns.index.is_monotonic_increasing
            or returns.columns.has_duplicates
        ):
            raise ValueError("unique ordered observations and asset identifiers required")
        names = list(map(str, returns.columns))
        values = returns.to_numpy(dtype=float)
    else:
        values = np.asarray(returns, dtype=float)
        names = list(map(str, range(values.shape[1]))) if values.ndim == 2 else []
    if (
        values.ndim != 2
        or values.shape[0] < 2
        or values.shape[1] < 1
        or not np.isfinite(values).all()
    ):
        raise ValueError("at least two complete finite observations and one asset required")
    if (values < -1).any():
        raise ValueError("simple returns cannot be below -100 percent")
    observations, assets = values.shape
    centered = values - values.mean(axis=0)
    sample = (
        returns.cov(ddof=1).to_numpy(dtype=float)
        if isinstance(returns, pd.DataFrame)
        else centered.T @ centered / (observations - 1)
    )
    target = sample.copy()
    intensity, average_correlation = 0.0, None
    degeneration, moments = None, {}
    if estimator_id == "fixed_diag_020":
        intensity = float(fixed_shrinkage)
        target = np.diag(np.diag(sample))
    elif estimator_id == "lw_constant_correlation":
        target, intensity, average_correlation, degeneration, moments = (
            _constant_correlation_target(centered, sample)
        )
    matrix = (1.0 - intensity) * sample + intensity * target
    matrix = (matrix + matrix.T) / 2
    if not np.isfinite(matrix).all():
        raise ValueError("covariance moments overflowed finite output")
    eigenvalues = np.linalg.eigvalsh(matrix)
    tolerance = 1e-10 * max(float(np.max(np.abs(eigenvalues))), float(np.finfo(float).tiny))
    if eigenvalues.min() < -tolerance:
        raise ValueError("covariance estimator produced a non-PSD matrix")
    return _covariance_result(
        matrix,
        estimator_id,
        fixed_shrinkage,
        observations,
        names,
        intensity,
        average_correlation,
        eigenvalues,
        degeneration,
        moments,
    )
