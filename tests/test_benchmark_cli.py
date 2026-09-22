"""Contract checks for the reproducible, offline performance command."""
import copy
import json

import pytest

from scripts import benchmark_backtest as benchmark


@pytest.mark.parametrize("arguments", [
    ["--bars", "30"], ["--symbols", "0"], ["--repeats", "0"],
    ["--seed", "-1"], ["--seed", str(2**32)],
    ["--profile", "same.json", "--output", "same.json"],
    ["--reference", "same.json", "--output", "same.json"],
])
def test_reject_invalid_arguments(arguments):
    with pytest.raises(SystemExit) as error:
        benchmark.parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("field,value", [
    ("bars", 181), ("config_sha256", "different"), ("data_sha256", "different"),
])
def test_reject_reference_workload_changes(field, value):
    workload = {"bars": 180, "config_sha256": "config", "data_sha256": "data"}
    reference = {"schema_version": benchmark.SCHEMA_VERSION,
                 "workload": dict(workload), "timing": {"median_seconds": 1.0},
                 "artifacts": {}}
    workload[field] = value
    with pytest.raises(ValueError, match="workload/config/data"):
        benchmark.validate_reference(reference, workload)


def test_repeats_require_exact_equality_but_reference_tolerates_float_noise():
    actual = {"equity": 10000.0 + 1e-7}
    expected = {"equity": 10000.0}
    benchmark._require_equivalent(actual, expected, "reference")
    with pytest.raises(ValueError, match="exactly"):
        benchmark._require_equivalent(actual, expected, "repeat 2", exact=True)


def test_offline_runs_verify_reference_and_profile(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    profile = tmp_path / "engine.prof"
    arguments = ["--bars", "60", "--symbols", "2", "--repeats", "2", "--memory"]
    assert benchmark.main([*arguments, "--output", str(baseline)]) == 0
    assert benchmark.main([*arguments, "--output", str(current),
                           "--reference", str(baseline), "--profile", str(profile)]) == 0
    report = json.loads(current.read_text(encoding="utf-8"))
    assert len(report["timing"]["wall_seconds"]) == 2
    assert report["comparison"]["artifacts_equivalent"] is True
    assert report["comparison"]["speedup"] > 0
    assert len(report["workload"]["data_sha256"]) == 64
    assert report["artifacts"]["equity_curve"]
    assert report["artifacts"]["benchmark_fixed"]
    assert report["artifacts"]["benchmark_dynamic"]
    assert report["artifacts"]["accounting_check"]["ok"] is True
    assert profile.stat().st_size > 0
    assert report["memory"]["peak_bytes"] >= report["memory"]["retained_bytes"] > 0
    assert "not process RSS" in report["memory"]["scope"]
    assert "traced_peak_reduction_pct" in report["comparison"]

    # A numerically different reference is a failure, not a performance win.
    tampered = copy.deepcopy(report)
    tampered["artifacts"]["equity_curve"][0]["equity"] += 100.0
    baseline.write_text(json.dumps(tampered), encoding="utf-8")
    rejected = tmp_path / "rejected.json"
    assert benchmark.main([*arguments, "--repeats", "1", "--output", str(rejected),
                           "--reference", str(baseline)]) == 1
    assert not rejected.exists()
