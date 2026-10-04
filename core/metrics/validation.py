"""Out-of-sample validation: train/test split, walk-forward, bootstrap, Monte Carlo, FDR.

Split out of core/metrics.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations
from typing import Any, Dict, Iterable, Optional
import numpy as np
import pandas as pd


def train_test_split_returns(returns: pd.Series, train_fraction: float = 0.7) -> Dict[str, pd.Series]:
    """Chronological (never shuffled) split of a return series into train/test (BM8).

    ``train_fraction`` is applied by position, not by calendar date. A
    random/shuffled split would leak future information into "train" and
    defeat the point of an out-of-sample test.
    """
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1 exclusive")
    clean = pd.Series(returns).dropna()
    split_pos = int(len(clean) * train_fraction)
    return {"train": clean.iloc[:split_pos], "test": clean.iloc[split_pos:]}


def walk_forward_windows(
    n_periods: int, train_size: int, test_size: int, step: Optional[int] = None,
) -> list[Dict[str, int]]:
    """Chronological, non-anticipating walk-forward window boundaries (BM8).

    Returns index-position boundaries only (train_start/train_end/
    test_start/test_end); the caller applies them to whatever series it is
    validating. Every test window starts exactly where its train window
    ends — there is no gap and no overlap between a window's train and
    test portions — and ``step`` (default ``test_size``, i.e. non-
    overlapping test windows) controls how far the next window slides.
    """
    if train_size < 1 or test_size < 1:
        raise ValueError("train_size and test_size must be at least 1")
    step = test_size if step is None else step
    if step < 1:
        raise ValueError("step must be at least 1")
    windows = []
    train_start = 0
    while train_start + train_size + test_size <= n_periods:
        train_end = train_start + train_size
        test_end = train_end + test_size
        windows.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": train_end, "test_end": test_end,
        })
        train_start += step
    return windows


def cohort_block_plan(values, *, timestamps=None, block_length=5, n_samples=2000,
                      seed=42, cohort_frequency=None):
    """Keep simultaneous observations together; never certify missing time metadata.

    Draws index chronological cohorts, not individual assets/trades. Nominal
    blocks describe support, not a proof that the blocks are independent.
    """
    raw = np.asarray(list(values), dtype=float)
    if raw.ndim != 1 or not np.isfinite(raw).all():
        raise ValueError("all observations must be finite; missing rows cannot be discarded")
    if block_length < 1 or n_samples < 1:
        raise ValueError("positive block_length and n_samples required")
    if timestamps is None and isinstance(values, pd.Series) and isinstance(values.index, pd.DatetimeIndex):
        timestamps = values.index
    if timestamps is None:
        codes = np.arange(len(raw))
        count, method, eligible = len(raw), "legacy_iid_diagnostic_missing_time", False
    else:
        times = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
        if len(times) != len(raw) or times.hasnans or not times.is_monotonic_increasing:
            raise ValueError("aligned, nonmissing chronological timestamps required")
        if cohort_frequency:
            times = times.floor(cohort_frequency)
        codes, unique = pd.factorize(times, sort=False)
        count, method = len(unique), "circular_chronological_cohort_blocks"
        eligible = count >= max(2, 2 * block_length)
    rng = np.random.default_rng(seed)
    if not count:
        draws = np.empty((n_samples, 0), dtype=int)
    elif timestamps is None:
        draws = rng.integers(count, size=(n_samples, count))
    else:
        starts = rng.integers(count, size=(n_samples, int(np.ceil(count / block_length))))
        draws = ((starts[..., None] + np.arange(block_length)) % count).reshape(n_samples, -1)[:, :count]
    metadata = {"method": method, "cohort_count": count, "block_length": block_length,
                "nominal_blocks": count // block_length, "time_evidence_eligible": eligible,
                "cohort_frequency": cohort_frequency, "seed": seed,
                "interpretation": "dependence sensitivity; nominal blocks are not independent-sample certificates"}
    return raw, codes, draws, metadata


def _cohort_means(values, *, timestamps=None, block_length=5, n_samples=2000, seed=42):
    raw, codes, draws, meta = cohort_block_plan(values, timestamps=timestamps,
        block_length=block_length, n_samples=n_samples, seed=seed)
    counts = np.bincount(codes, minlength=meta["cohort_count"])
    means = np.divide(np.bincount(codes, weights=raw, minlength=len(counts)), counts,
                      out=np.zeros(len(counts)), where=counts > 0)
    meta["cohort_aggregation"] = "equal-weight mean of simultaneous observations; not an account return unless caller declares it"
    return raw, means, means[draws], meta


def bootstrap_return_distribution(
    returns: Iterable[float], statistic: str = "mean",
    n_samples: int = 2000, confidence: float = 0.95, seed: int = 42,
    *, timestamps=None, block_length: int = 5,
) -> Dict[str, Any]:
    """Chronological cohort-block interval, with explicit legacy diagnostics.

    Sharpe bounds use per-observation units. Untimed input retains numeric
    legacy output for reports, but evidence_eligible is false. Missing/nonfinite
    observations are rejected rather than deleted. Compare several block lengths.
    """
    if statistic not in {"mean", "sharpe"}:
        raise ValueError("statistic must be 'mean' or 'sharpe'")
    try:
        raw, values, samples, metadata = _cohort_means(returns, timestamps=timestamps,
            block_length=block_length, n_samples=n_samples, seed=seed)
    except (ValueError, TypeError) as exc:
        return {"status": "invalid_input", "sample_size": 0, "value": None,
                "lower": None, "upper": None, "reason": str(exc), "evidence_eligible": False}
    if len(values) < 2 or not 0 < confidence < 1:
        return {"status": "insufficient", "sample_size": int(len(values)),
                "value": None, "lower": None, "upper": None}
    if statistic == "mean":
        stat_values = samples.mean(axis=1)
        point: Optional[float] = float(values.mean())
    else:
        means = samples.mean(axis=1)
        stds = samples.std(axis=1, ddof=1)
        stat_values = np.divide(
            means, stds, out=np.full_like(means, np.nan), where=stds > 0,
        )
        stat_values = stat_values[np.isfinite(stat_values)]
        point_std = float(values.std(ddof=1))
        point = float(values.mean() / point_std) if point_std > 0 else None
    if len(stat_values) == 0:
        return {"status": "undefined", "sample_size": int(len(values)),
                "value": point, "lower": None, "upper": None}
    alpha = (1 - confidence) / 2
    return {"status": "ok" if metadata["time_evidence_eligible"] else "diagnostic",
            "sample_size": int(len(raw)), "effective_time_cohorts": len(values),
            "evidence_eligible": metadata["time_evidence_eligible"], "resampling": metadata, "value": point,
            "lower": float(np.quantile(stat_values, alpha)),
            "upper": float(np.quantile(stat_values, 1 - alpha))}


def one_sided_bootstrap_p_value(
    returns: Iterable[float], *, n_samples: int = 2000, seed: int = 42,
    timestamps=None, block_length: int = 5,
) -> Dict[str, Any]:
    """One-sided null-centered cohort-block test of nonpositive mean.

    Untimed/short-block input only populates diagnostic_p_value, preventing
    a legacy IID number from silently entering FDR. Shared cohorts preserve
    simultaneous cross-asset dependence. Stationarity remains an assumption.
    """
    try:
        raw, values, samples, metadata = _cohort_means(returns, timestamps=timestamps,
            block_length=block_length, n_samples=n_samples, seed=seed)
    except (ValueError, TypeError) as exc:
        return {"status": "invalid_input", "sample_size": 0, "p_value": None,
                "mean": None, "reason": str(exc), "evidence_eligible": False}
    if len(values) < 2:
        return {"status": "insufficient", "sample_size": int(len(values)),
                "p_value": None, "mean": None}
    # Null-centered joint time blocks: tail of the null mean above the observed
    # mean. Resampling uncentered signs is not this hypothesis test.
    null_means = samples.mean(axis=1) - values.mean()
    failures = int(np.count_nonzero(null_means >= values.mean()))
    probability = float((failures + 1) / (n_samples + 1))
    return {
        "status": "ok" if metadata["time_evidence_eligible"] else "diagnostic",
        "sample_size": int(len(raw)), "effective_time_cohorts": len(values),
        "evidence_eligible": metadata["time_evidence_eligible"], "resampling": metadata,
        "n_samples": int(n_samples),
        "mean": float(values.mean()),
        "p_value": probability if metadata["time_evidence_eligible"] else None,
        "diagnostic_p_value": probability,
    }


def monte_carlo_trade_sequence(
    trade_pnls: Iterable[float], n_simulations: int = 2000, seed: int = 42,
) -> Dict[str, Any]:
    """Monte Carlo reordering of realized trade PnLs to estimate sequence risk (BM8).

    Shuffles the SAME realized trade outcomes (sampling without
    replacement, i.e. permutation) to build a distribution of cumulative-
    PnL paths, then reports the distribution of final PnL and of maximum
    drawdown across simulated orderings. This tests sequence risk given
    trades that already happened — it invents no new trade outcomes, so it
    says nothing about whether these trades would repeat.
    """
    values = np.asarray(list(trade_pnls), dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return {"status": "insufficient", "sample_size": int(len(values))}
    rng = np.random.default_rng(seed)
    final_pnls = np.empty(n_simulations)
    max_drawdowns = np.empty(n_simulations)
    for i in range(n_simulations):
        cumulative = np.cumsum(rng.permutation(values))
        final_pnls[i] = cumulative[-1]
        running_peak = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))[1:]
        max_drawdowns[i] = (cumulative - running_peak).min()
    return {
        "status": "ok", "sample_size": int(len(values)), "n_simulations": n_simulations,
        "realized_final_pnl": float(values.sum()),
        "final_pnl_mean": float(final_pnls.mean()),
        "final_pnl_p05": float(np.quantile(final_pnls, 0.05)),
        "final_pnl_p95": float(np.quantile(final_pnls, 0.95)),
        "max_drawdown_mean": float(max_drawdowns.mean()),
        "max_drawdown_p05": float(np.quantile(max_drawdowns, 0.05)),
    }


def benjamini_hochberg(p_values: Iterable[float], fdr: float = 0.05) -> Dict[str, Any]:
    """Benjamini-Hochberg FDR correction across multiple hypothesis tests (BM8).

    Given raw p-values from independently tested hypotheses (e.g. one per
    strategy variant tried), returns each hypothesis's FDR-adjusted p-value
    and whether it survives the threshold. This is what makes "we tried N
    variants and picked the best" defensible: without a multiple-testing
    correction, the best of N random strategies looks significant purely
    from search breadth, not genuine edge.
    """
    if not 0 < fdr < 1:
        raise ValueError("fdr must be between 0 and 1 exclusive")
    values = np.asarray(list(p_values), dtype=float)
    n = len(values)
    if n == 0:
        return {"status": "insufficient", "sample_size": 0,
                "adjusted_p_values": [], "rejected": [], "rejected_count": 0}
    order = np.argsort(values)
    ranked = values[order]
    adjusted_ranked = ranked * n / (np.arange(n) + 1)
    # Standard BH step-up: enforce monotonicity from the largest p-value down.
    adjusted_ranked = np.minimum.accumulate(adjusted_ranked[::-1])[::-1]
    adjusted_ranked = np.clip(adjusted_ranked, 0.0, 1.0)
    adjusted = np.empty(n)
    adjusted[order] = adjusted_ranked
    rejected = [bool(p <= fdr) for p in adjusted]
    return {
        "status": "ok", "sample_size": n, "fdr": fdr,
        "adjusted_p_values": [float(p) for p in adjusted],
        "rejected": rejected, "rejected_count": int(sum(rejected)),
    }
