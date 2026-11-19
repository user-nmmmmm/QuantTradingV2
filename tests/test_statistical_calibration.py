"""Independent deterministic checks; no random rejection-rate pytest thresholds."""

from copy import deepcopy
import json
import math

import numpy as np
import pytest

from analysis.statistical_calibration import (
    DEFAULT_CALIBRATION_DESIGN,
    calibration_job_ids,
    run_calibration,
    simulated_calibration_panel,
    validate_calibration_design,
    wilson_interval,
)


def small_design():
    return {
        **deepcopy(DEFAULT_CALIBRATION_DESIGN),
        "replications": 2,
        "observations": 80,
        "candidate_count": 2,
        "ar_coefficients": [0.0, 0.5],
        "block_lengths": [5],
        "bootstrap_iterations": 100,
    }


def test_wilson_interval_matches_independent_hand_formula_and_endpoints():
    value = wilson_interval(5, 10)
    z = 1.959963984540054
    expected_center = (0.5 + z * z / 20) / (1 + z * z / 10)
    expected_radius = z * math.sqrt(0.25 / 10 + z * z / 400) / (1 + z * z / 10)
    assert value["lower"] == pytest.approx(expected_center - expected_radius)
    assert value["upper"] == pytest.approx(expected_center + expected_radius)
    assert wilson_interval(0, 10)["lower"] == pytest.approx(0, abs=1e-15)
    assert wilson_interval(10, 10)["upper"] == pytest.approx(1, abs=1e-15)


@pytest.mark.parametrize(
    "change",
    [
        {"replications": True},
        {"replications": 0},
        {"ar_coefficients": [1.0]},
        {"block_lengths": [1000]},
        {"bootstrap_iterations": 99},
        {"noise_scale": float("nan")},
        {"cross_candidate_correlation": 1},
        {"candidate_count": 100, "observations": 10000, "replications": 1000},
    ],
)
def test_calibration_refuses_invalid_design_or_excess_resource_budget(change):
    with pytest.raises(ValueError):
        validate_calibration_design({**small_design(), **change})


def test_same_innovations_are_paired_and_only_declared_candidate_has_signal():
    design = small_design()
    null = simulated_calibration_panel(design, ar=0.5, replication=0, signal=False)
    signal = simulated_calibration_panel(design, ar=0.5, replication=0, signal=True)
    assert np.allclose(signal.iloc[:, 0] - null.iloc[:, 0], design["known_signal_mean"])
    assert np.array_equal(signal.iloc[:, 1:], null.iloc[:, 1:])
    assert null.equals(simulated_calibration_panel(design, ar=0.5, replication=0, signal=False))
    assert not null.equals(simulated_calibration_panel(design, ar=0.5, replication=1, signal=False))


def test_bounded_simulation_reports_all_cells_and_monte_carlo_uncertainty():
    result = run_calibration(small_design())
    assert result["engineering"] == "pass" and result["jobs_planned"] == 8
    assert len(result["attempts"]) == 8 and len(result["cells"]) == 4
    assert len(set(calibration_job_ids(small_design()))) == 8
    for cell in result["cells"]:
        assert cell["attempted"] == cell["computed"] == 2
        assert (
            0
            <= cell["interval"]["lower"]
            <= cell["rejection_rate"]
            <= cell["interval"]["upper"]
            <= 1
        )
        assert len(cell["p_values"]) == 2
    assert not result["admission_eligible"]
    json.dumps(result, allow_nan=False)
    # Repeating a fixed simulation is a reproducibility check, not a significance assertion.
    assert result == run_calibration(small_design())


def test_failed_simulation_replication_is_retained_without_shrinking_the_rate_denominator(
    monkeypatch,
):
    import analysis.statistical_calibration as module

    original = module.white_reality_check
    calls = []

    def fail_first(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("injected estimator failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "white_reality_check", fail_first)
    result = run_calibration(small_design())
    assert result["engineering"] == "fail" and len(result["attempts"]) == 8
    assert result["attempts"][0]["status"] == "failed"
    assert result["cells"][0]["status"] == "incomplete"
    assert result["cells"][0]["rejection_rate"] is None
    assert result["cells"][0]["interval"] is None
    assert result["cells"][0]["computed"] == 1 and result["cells"][0]["attempted"] == 2
