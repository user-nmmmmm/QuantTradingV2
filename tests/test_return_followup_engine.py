from copy import deepcopy
import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_return_followup_engine as followup


def prior():
    return json.loads((Path(__file__).parent / "fixtures/paper_followup/registration.json").read_text(encoding="utf-8"))


def test_full_family_preserves_old_controls_and_common_risk():
    original = prior()
    before = deepcopy(original)
    jobs = followup.build_jobs(original)
    assert original == before
    assert len(jobs) == 56 and len({job["name"] for job in jobs}) == 56
    assert {job["arm"] for job in jobs} == set(followup.ARMS)
    for job in jobs:
        comparison = next(j for j in jobs if j["arm"] == "prior_momentum_no_obv"
                          and j["window"] == job["window"] and j["cost_multiplier"] == job["cost_multiplier"])
        assert job["parameters"]["account"] == comparison["parameters"]["account"]
        assert job["parameters"]["risk"] == comparison["parameters"]["risk"]
        assert job["parameters"]["execution"] == comparison["parameters"]["execution"]
        assert not any(job["parameters"][name]["enabled"] for name in followup.OBSERVERS)
        if job["arm"] in followup.CONTROLS and job["window"] != "continuous":
            reference = next(j for j in original["jobs"] if j["arm"] == followup.CONTROLS[job["arm"]]
                and j["window"] == job["window"] and j["cost_multiplier"] == job["cost_multiplier"])
            assert job["parameters"] == reference["parameters"]


def test_candidates_change_only_declared_strategy_options():
    jobs = followup.build_jobs(prior())
    group = {j["arm"]: j["parameters"] for j in jobs if j["window"] == "continuous" and j["cost_multiplier"] == 1.}
    expected = {"no_obv_hybrid": {"exit_mode": "hybrid"},
        "no_obv_wide_trail": {"trailing_atr_multiple": 3.5},
        "no_obv_medium_trend": {"horizons": [60, 120], "weights": [.5, .5]},
        "no_obv_equal_ensemble": {"horizons": [20, 60, 120], "weights": [1 / 3] * 3}}
    for name, changes in expected.items():
        params = deepcopy(group["prior_momentum_no_obv"])
        params["research"]["trend_portfolio_v2"].update(changes)
        assert params == group[name]
    modified = prior()
    modified["jobs"][0]["parameters"]["risk"]["max_leverage"] = 9.
    with pytest.raises(ValueError, match="old control parameters changed"):
        followup.build_jobs(modified)


def test_cash_cost_does_not_charge_embedded_slippage_twice_and_flat_tail_exposure():
    timeline = pd.date_range("2020-01-01", periods=3)
    result = {"trades": [{"commission": 2., "slip": 5., "qty": 3.}],
        "financing_ledger": [{"amount": -1.}],
        "equity_curve": pd.DataFrame({"equity": [10000., 10001.],
            "gross_exposure_pct_equity": [.5, 0.], "net_exposure_pct_equity": [.5, 0.]}, index=timeline[:2])}
    metrics = followup.extra_metrics(result, timeline)
    assert metrics["net_cash_cost_quote"] == 1.
    assert metrics["price_embedded_slippage_quote"] == 15.
    assert metrics["net_pnl_quote"] == 1.
    assert metrics["average_gross_exposure_pct"] == pytest.approx(100 / 6)


def test_source_drift_fails_before_engine_and_preserves_failure(tmp_path, monkeypatch):
    job = {"name": "candidate", "arm": "baseline", "window": "continuous", "cost_multiplier": 1.}
    protocol = {"jobs": [job], "source_hashes": {"fake.py": "old"}}
    path = tmp_path / "registration.json"
    digest = followup.freeze_registration(path, protocol)
    monkeypatch.setattr(followup, "source_identity", lambda: {"fake.py": "changed"})
    monkeypatch.setattr(followup, "run_job", lambda *args, **kwargs: pytest.fail("engine must not run after identity drift"))
    previous_logging = logging.root.manager.disable
    output = followup._worker((str(path), "candidate", digest))
    assert logging.root.manager.disable == previous_logging
    assert output["status"] == "failed"
    assert "source identity changed" in output["message"]
    assert (tmp_path / "failures/candidate.json").is_file()


def test_registration_only_is_complete_before_any_new_engine_result(tmp_path, monkeypatch):
    output = tmp_path / "study"
    # CI must not depend on ignored local research reports. Use the registered
    # parameter family and explicitly synthetic receipt files for hash capture.
    reference = tmp_path / "prior"
    reference.mkdir()
    original = prior()
    (reference / "registration.json").write_text(json.dumps(original), encoding="utf-8")
    for job in followup.build_jobs(original):
        if job["arm"] not in followup.CONTROLS or job["window"] == "continuous":
            continue
        name = f"{followup.CONTROLS[job['arm']]}_{job['window']}_cost{job['cost_multiplier']:g}"
        folder = reference / "runs" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "resolved_config.json").write_text(json.dumps(job["parameters"]), encoding="utf-8")
        (folder / "summary.json").write_text('{"synthetic_test_fixture": true}', encoding="utf-8")
        (folder / "trades.csv").write_text("timestamp,qty\n", encoding="utf-8")
        (folder / "financing_ledger.csv").write_text("timestamp,amount\n", encoding="utf-8")
    monkeypatch.setattr(followup, "source_identity", lambda: {})
    monkeypatch.setattr(followup, "run_job", lambda *args, **kwargs: pytest.fail("registration must not run engine"))
    previous_logging = logging.root.manager.disable
    result = followup.main(["--output", str(output), "--prior", str(reference), "--register-only"])
    assert logging.root.manager.disable == previous_logging
    protocol = json.loads((output / "registration.json").read_text())
    assert result["status"] == "registered"
    assert protocol["maximum_runs"] == 56 and len(protocol["jobs"]) == 56
    assert (output / "frozen_inputs/BTC_USDT.csv").is_file()
    assert not (output / "results.json").exists()
