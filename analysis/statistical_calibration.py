"""Preregistered, bounded AR-noise/signal calibration of local White diagnostics.

This offline simulation calibrates an implementation under an explicitly narrow
DGP. It neither validates a trading strategy nor certifies broad type-I control.
"""

from __future__ import annotations

import math
import json
from statistics import NormalDist

import numpy as np
import pandas as pd

from analysis.paper_validation import white_reality_check
from core.foundation.reproducibility import canonical_json, sha256_bytes


DEFAULT_CALIBRATION_DESIGN = {
    "schema": "paper-statistics-calibration-design/v1",
    "replications": 24,
    "observations": 160,
    "candidate_count": 3,
    "ar_coefficients": [0.0, 0.6],
    "block_lengths": [5, 20],
    "bootstrap_iterations": 199,
    "bootstrap_method": "stationary",
    "noise_scale": 0.01,
    "known_signal_mean": 0.004,
    "cross_candidate_correlation": 0.4,
    "alpha": 0.05,
    "seed": 20261027,
    "confidence": 0.95,
    "primary_metric": "White family rejection rate under each declared DGP",
    "precision_target": "Report Wilson Monte Carlo intervals; 24 repetitions are a bounded pilot, not a precision certificate.",
    "search_policy": "All declared AR/block/DGP cells reported; no best block selection or grid expansion.",
    "time_evidence": "one same-axis time row per simulated date; candidates and seeds do not add market observations",
}


def validate_calibration_design(design):
    if not isinstance(design, dict):
        raise ValueError("calibration design object required")
    try:
        value = json.loads(json.dumps(design, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("finite JSON calibration design required") from exc
    if value.get("schema") != "paper-statistics-calibration-design/v1":
        raise ValueError("unsupported calibration design")
    for key, minimum, maximum in (
        ("replications", 2, 1000),
        ("observations", 40, 10000),
        ("candidate_count", 2, 100),
        ("bootstrap_iterations", 100, 10000),
    ):
        number = value.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not minimum <= number <= maximum
        ):
            raise ValueError(f"bounded integer {key} required")
    for key in ("ar_coefficients", "block_lengths"):
        if (
            not isinstance(value.get(key), list)
            or not value[key]
            or len(set(value[key])) != len(value[key])
        ):
            raise ValueError(f"unique nonempty {key} required")
    if any(
        isinstance(x, bool)
        or not isinstance(x, (float, int))
        or not math.isfinite(x)
        or abs(x) >= 1
        for x in value["ar_coefficients"]
    ):
        raise ValueError("stationary AR coefficients required")
    if any(
        isinstance(x, bool) or not isinstance(x, int) or x < 1 or 2 * x > value["observations"]
        for x in value["block_lengths"]
    ):
        raise ValueError("at least two nominal blocks required")
    for key in ("noise_scale", "known_signal_mean"):
        if (
            not isinstance(value.get(key), (int, float))
            or isinstance(value[key], bool)
            or not math.isfinite(value[key])
            or value[key] <= 0
        ):
            raise ValueError(f"positive finite {key} required")
    for key in ("alpha", "confidence", "cross_candidate_correlation"):
        if (
            not isinstance(value.get(key), (float, int))
            or isinstance(value[key], bool)
            or not math.isfinite(value[key])
            or not 0 < value[key] < 1
        ):
            raise ValueError(f"{key} must be strictly between zero and one")
    if value.get("bootstrap_method") not in {"stationary", "circular"}:
        raise ValueError("supported bootstrap method required")
    if (
        isinstance(value.get("seed"), bool)
        or not isinstance(value.get("seed"), int)
        or value["seed"] < 0
    ):
        raise ValueError("nonnegative integer seed required")
    jobs = value["replications"] * len(value["ar_coefficients"]) * len(value["block_lengths"]) * 2
    if (
        jobs > 20000
        or jobs * value["bootstrap_iterations"] * value["observations"] * value["candidate_count"]
        > 200_000_000
    ):
        raise ValueError("calibration compute budget exceeded")
    return value


def wilson_interval(successes, trials, *, confidence=0.95):
    """Wilson score interval for independent simulation replication indicators."""
    if (
        isinstance(trials, bool)
        or not isinstance(trials, int)
        or trials < 1
        or isinstance(successes, bool)
        or not isinstance(successes, int)
        or not 0 <= successes <= trials
        or not 0 < confidence < 1
    ):
        raise ValueError("valid binomial count and confidence required")
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    radius = (
        z
        * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
        / denominator
    )
    return {
        "lower": max(0.0, center - radius),
        "upper": min(1.0, center + radius),
        "confidence": confidence,
        "method": "Wilson score across independent simulation replications",
    }


def simulated_calibration_panel(design, *, ar, replication, signal):
    """Stationary Gaussian AR(1) with explicit candidate common noise.

    Signal is injected into the first candidate only. Null and signal share the
    same innovations at each AR/replication for a paired sensitivity comparison.
    """
    n, m = design["observations"], design["candidate_count"]
    seed = np.random.SeedSequence([design["seed"], int(round((ar + 1) * 1_000_000)), replication])
    rng = np.random.default_rng(seed)
    rho = design["cross_candidate_correlation"]
    innovations = math.sqrt(rho) * rng.normal(size=(n + 256, 1)) + math.sqrt(1 - rho) * rng.normal(
        size=(n + 256, m)
    )
    values = np.empty_like(innovations)
    values[0] = innovations[0] * design["noise_scale"]
    innovation_scale = design["noise_scale"] * math.sqrt(1 - ar * ar)
    for i in range(1, len(values)):
        values[i] = ar * values[i - 1] + innovation_scale * innovations[i]
    values = values[256:].copy()
    if signal:
        values[:, 0] += design["known_signal_mean"]
    return pd.DataFrame(
        values,
        index=pd.date_range("2000-01-01", periods=n, tz="UTC"),
        columns=[f"candidate_{i}" for i in range(m)],
    )


def calibration_job_ids(design):
    design = validate_calibration_design(design)
    return [
        f"ar={ar:g}|dgp={dgp}|block={block}|rep={replication}"
        for ar in design["ar_coefficients"]
        for dgp in ("null", "signal")
        for block in design["block_lengths"]
        for replication in range(design["replications"])
    ]


def _validate_calibration_scope(scope, design, digest, jobs):
    if scope is not None:
        audit = scope.audit()
        if audit["status"] != "ok" or scope.protocol["candidate_ids"] != jobs:
            raise ValueError("calibration requires the exact byte-verified preregistered job list")
        if scope.protocol.get("calibration_design_sha256") != digest:
            raise ValueError("registered calibration design changed")
        expected_resampling = {
            "method": design["bootstrap_method"],
            "block_lengths": design["block_lengths"],
            "iterations": design["bootstrap_iterations"],
            "seed": design["seed"],
        }
        if scope.protocol["resampling"] != expected_resampling:
            raise ValueError("registered resampling differs from the fixed calibration design")
        if (
            scope.protocol["benchmark_ids"] != ["fixed_zero_cash_reference"]
            or audit["states"].get("fixed_zero_cash_reference") != "completed"
        ):
            raise ValueError("the fixed null comparator must be completed before calibration")


def _run_calibration_cell(design, ar, dgp, block, scope, attempts):
    counts, pvalues, failures = 0, [], []
    for rep in range(design["replications"]):
        job = f"ar={ar:g}|dgp={dgp}|block={block}|rep={rep}"
        if scope is not None:
            scope.record(candidate=job, status="started")
        try:
            panel = simulated_calibration_panel(
                design, ar=ar, replication=rep, signal=dgp == "signal"
            )
            benchmark = pd.Series(0.0, index=panel.index, name="cash")
            evidence = white_reality_check(
                panel,
                benchmark,
                family_complete=True,
                iterations=design["bootstrap_iterations"],
                block_length=block,
                bootstrap=design["bootstrap_method"],
                seed=design["seed"] + rep,
            )
            if evidence["status"] != "ok":
                raise ValueError(
                    "calibration diagnostic did not compute: " + str(evidence.get("reason"))
                )
            p = evidence["p_value"]
            pvalues.append(p)
            counts += int(p <= design["alpha"])
            row = {
                "job_id": job,
                "status": "completed",
                "p_value": p,
                "data_sha256": sha256_bytes(canonical_json(panel.to_numpy().tolist()).encode()),
                "bootstrap_monte_carlo_standard_error": evidence["monte_carlo_standard_error"],
            }
            if scope is not None:
                scope.record(candidate=job, status="completed", result=row)
        except KeyboardInterrupt:
            if scope is not None:
                scope.record(
                    candidate=job,
                    status="interrupted",
                    reason="calibration interrupted",
                )
            raise
        except Exception as exc:
            row = {
                "job_id": job,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            failures.append(row)
            if scope is not None:
                scope.record(candidate=job, status="failed", reason=row["reason"])
        attempts.append(row)
    complete = len(pvalues) == design["replications"]
    return {
        "ar": ar,
        "dgp": dgp,
        "block_length": block,
        "attempted": design["replications"],
        "computed": len(pvalues),
        "failures": failures,
        "rejections": counts,
        "rejection_rate": counts / len(pvalues) if complete else None,
        "computed_rejection_rate_diagnostic": counts / len(pvalues) if pvalues else None,
        "metric": "false_positive_rate" if dgp == "null" else "known_signal_detection_rate",
        "interval": wilson_interval(counts, len(pvalues), confidence=design["confidence"])
        if complete
        else None,
        "status": "diagnostic" if complete else "incomplete",
        "p_values": pvalues,
    }


def run_calibration(design, *, scope=None):
    """Run a previously fixed finite design, retaining every failed attempt."""
    design = validate_calibration_design(design)
    digest = sha256_bytes(canonical_json(design).encode())
    jobs = calibration_job_ids(design)
    _validate_calibration_scope(scope, design, digest, jobs)
    cells, attempts = [], []
    for ar in design["ar_coefficients"]:
        for dgp in ("null", "signal"):
            for block in design["block_lengths"]:
                cells.append(_run_calibration_cell(design, ar, dgp, block, scope, attempts))
    return {
        "schema": "paper-statistics-calibration/v1",
        "design": design,
        "design_sha256": digest,
        "engineering": "pass" if all(row["status"] == "completed" for row in attempts) else "fail",
        "research": "simulation_diagnostic",
        "historical_global_search_complete": False,
        "admission_eligible": False,
        "jobs_planned": len(jobs),
        "attempts": attempts,
        "cells": cells,
        "limitations": [
            "Finite Gaussian AR calibration only; not empirical market coverage or a trading claim.",
            "Block-length cells are paired sensitivities, not independent replications or a grid to optimize.",
            "Wilson intervals describe independent outer simulation replications; bootstrap p-value Monte Carlo error is separate.",
        ],
    }
