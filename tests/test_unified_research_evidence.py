from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from analysis.research_evidence import ResearchEvidenceRun, candidate_panel_evidence, sharpe_dependence_diagnostic
from analysis.research_validation import deflated_sharpe_ratio, evaluate_holdout_admission
from analysis.walk_forward import WalkForwardConfig, run_walk_forward
from core.metrics import bootstrap_return_distribution, calculate_profit_factor, one_sided_bootstrap_p_value


def series(values):
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), tz="UTC"))


def test_old_dsr_requires_real_trial_dispersion_and_matches_full_panel_scale():
    rng = np.random.default_rng(9)
    panel = {"a": series(rng.normal(.002, .01, 400)), "b": series(rng.normal(.001, .01, 400))}
    evidence = candidate_panel_evidence(panel)
    chosen = evidence["dsr"]["a"]
    assert chosen["probability"] is None
    assert deflated_sharpe_ratio(panel["a"], trials=2)["status"] == "insufficient"
    legacy = deflated_sharpe_ratio(panel["a"], trials=2, trial_sharpe_std=chosen["trial_sharpe_sample_std"])
    assert legacy["expected_max_period_sharpe"] == pytest.approx(chosen["expected_max_period_sharpe"])
    assert legacy["diagnostic_probability"] == pytest.approx(chosen["diagnostic_probability"])
    assert legacy["probability"] is None


def test_dsr_full_panel_is_scale_invariant_and_never_claims_old_history_complete():
    rng = np.random.default_rng(11)
    panel = {"a": series(rng.normal(.001, .01, 200)), "b": series(rng.normal(.001, .012, 200))}
    a = candidate_panel_evidence(panel)["dsr"]["a"]
    b = candidate_panel_evidence({k: v * 10 for k, v in panel.items()})["dsr"]["a"]
    assert a["diagnostic_probability"] == pytest.approx(b["diagnostic_probability"])
    assert not a["search_accounting_complete"] and a["probability"] is None


@pytest.mark.parametrize("bad", [pd.Series(dtype=float), series([.01] * 12).iloc[1:]])
def test_incomplete_candidates_are_preserved_not_intersected_or_zero_filled(bad):
    result = candidate_panel_evidence({"good": series([.01, -.01] * 6), "failed": bad})
    assert result["status"] == "invalid"
    assert result["candidate_ids"] == ["good", "failed"]
    assert result["dsr"]["good"]["probability"] is None
    assert result["pbo"]["status"] == "invalid"
    assert all(row["status"] == "invalid" and row["diagnostic_p_value"] is None
               for row in result["reality_check"].values())


def test_duplicate_same_time_assets_do_not_increase_time_evidence():
    values = series([.02, -.01, .01, -.005] * 20)
    duplicates = pd.Series(np.repeat(values.to_numpy(), 4), index=values.index.repeat(4))
    a = bootstrap_return_distribution(values, block_length=5)
    b = bootstrap_return_distribution(duplicates, block_length=5)
    assert a["effective_time_cohorts"] == b["effective_time_cohorts"] == 80
    assert a["lower"] == pytest.approx(b["lower"])
    assert a["upper"] == pytest.approx(b["upper"])
    pf_a = calculate_profit_factor(values, timestamps=values.index)
    pf_b = calculate_profit_factor(duplicates, timestamps=duplicates.index)
    assert pf_a["lower"] == pytest.approx(pf_b["lower"])
    assert pf_a["resampling"]["nominal_blocks"] == pf_b["resampling"]["nominal_blocks"]


def test_missing_time_keeps_only_diagnostic_probability():
    result = one_sided_bootstrap_p_value([.01] * 100, n_samples=100)
    assert result["status"] == "diagnostic" and result["p_value"] is None
    assert result["diagnostic_p_value"] == pytest.approx(1 / 101)
    assert not calculate_profit_factor([2., -1.] * 30)["evidence_eligible"]


def test_persistent_returns_have_wider_block_uncertainty_than_iid():
    values = np.repeat([.02, -.015, .01, -.02, .015, -.005, .02, -.01, .01, -.015], 20)
    iid = bootstrap_return_distribution(values)
    blocks = bootstrap_return_distribution(series(values), block_length=20)
    assert blocks["upper"] - blocks["lower"] > 1.5 * (iid["upper"] - iid["lower"])


def test_nonfinite_observations_cannot_be_deleted_to_pass():
    assert one_sided_bootstrap_p_value(series([.01] * 99 + [np.nan]))["status"] == "invalid_input"
    assert calculate_profit_factor([2., -1.] * 30 + [np.nan])["status"] == "invalid_input"


def test_pf_gate_cannot_treat_one_day_of_many_assets_as_independent_trades():
    trades = [{"net_pnl": n, "exit_time": "2025-01-01", "commission": .1, "slippage": .1,
               "gross_pnl_theoretical": n + .2} for n in [2.] * 50 + [-1.] * 10]
    equity = series(np.arange(100., 161.))
    result = evaluate_holdout_admission(trades=trades, equity=equity, benchmark=equity * 0 + 100)
    assert not result["gates"]["G13_pf_significance"]
    assert result["profit_factor"]["resampling"]["cohort_count"] == 1


def test_hac_sharpe_exposes_serial_dependence_and_annualization():
    rng = np.random.default_rng(3)
    noise = np.zeros(1000)
    for i in range(1, len(noise)):
        noise[i] = .9 * noise[i - 1] + rng.normal(0, .001)
    values = .004 + noise
    a = sharpe_dependence_diagnostic(values, periods_per_year=1, max_lag=10)
    b = sharpe_dependence_diagnostic(values, periods_per_year=365, max_lag=10)
    assert a["hac_annualized_sharpe"] < a["iid_annualized_sharpe"]
    assert b["hac_annualized_sharpe"] == pytest.approx(a["hac_annualized_sharpe"] * np.sqrt(365))
    assert b["probability"] is None and not b["admission_eligible"]


def fixture_run(monkeypatch, tmp_path, *, future_sign=1, fail=None, interrupt=False):
    import analysis.walk_forward as module
    timeline = pd.date_range("2024-01-01", periods=45)
    frame = pd.DataFrame({key: np.ones(45) for key in ("open", "high", "low", "close", "volume")}, index=timeline)
    def factory(name):
        def build():
            return {}
        build.name = name
        return build
    candidates = {name: factory(name) for name in ("a", "b")}
    journal = ResearchEvidenceRun(candidates=list(candidates), parameters={"test": True},
        data_map={"X": frame}, output=tmp_path / "run", source_hashes={"fixture": "v1"})
    def fake(data_map, build, *, start, end, **kwargs):
        assert (journal.output / "registration.json").exists()
        if fail == build.name:
            if interrupt:
                raise KeyboardInterrupt("fixture interruption")
            raise ValueError("fixture failure")
        dates = pd.date_range(start, end)
        values = (.02 if build.name == "a" else .01) + np.arange(len(dates)) * .0001
        if start >= timeline[30]:
            values = values * future_sign
        return {"returns": pd.Series(values, index=dates), "trades": 1}
    monkeypatch.setattr(module, "_run_window", fake)
    config = WalkForwardConfig(train_size=20, validation_size=10, test_size=10,
        warmup_period=0, selection_metric="TotalReturn", bootstrap_samples=100)
    return lambda: run_walk_forward({"X": frame}, candidates, config, evidence_run=journal), journal


def test_future_returns_do_not_change_selection_or_pretest_journal(monkeypatch, tmp_path):
    selected = []
    for sign in (1, -1):
        run, journal = fixture_run(monkeypatch, tmp_path / str(sign), future_sign=sign)
        result = run()
        decisions = [row for row in journal.events if row["status"] == "selected"]
        selected.append(decisions[0]["candidate"])
        assert decisions[0]["sequence"] < min(row["sequence"] for row in journal.events
            if row["phase"] == "test" and row["status"] == "started")
        assert pd.Timestamp(decisions[0]["information_cutoff"]) < pd.Timestamp(decisions[0]["test_start"])
        assert result["candidate_family_evidence"]["candidate_ids"] == ["a", "b"]
        assert result["candidate_family_evidence"]["protocol"]["benchmark"] == "cash_zero_return"
        assert "pbo" in result["candidate_family_evidence"]
        assert set(result["candidate_family_evidence"]["reality_check"]) == {"5", "20", "60"}
        assert result["procedure"]["deflated_sharpe"]["probability"] is None
    assert selected == ["a", "a"]


def test_candidate_failure_is_retained_and_invalidates_family(monkeypatch, tmp_path):
    run, journal = fixture_run(monkeypatch, tmp_path, fail="b")
    result = run()
    assert result["candidate_family_evidence"]["status"] == "invalid"
    assert result["candidate_family_evidence"]["failed_candidates"] == ["b"]
    assert result["candidate_family_evidence"]["pbo"]["status"] == "invalid"
    assert all(row["status"] == "invalid" for row in result["candidate_family_evidence"]["reality_check"].values())
    assert not any(row["survives_fdr"] for row in result["candidates"].values())
    assert any(row["candidate"] == "b" and row["status"] == "failed" for row in journal.events)


def test_interruption_is_persisted_with_remaining_candidates_planned(monkeypatch, tmp_path):
    run, journal = fixture_run(monkeypatch, tmp_path, fail="a", interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        run()
    rows = [json.loads(line) for line in (journal.output / "attempts.jsonl").read_text().splitlines()]
    assert any(row["candidate"] == "a" and row["status"] == "interrupted" for row in rows)
    assert any(row["candidate"] == "b" and row["status"] == "planned" for row in rows)


def test_grid_failure_does_not_remove_candidate(monkeypatch, tmp_path):
    from analysis import optimize
    journal = ResearchEvidenceRun(candidates=["entry=20,exit=5", "entry=30,exit=5"],
        parameters={}, data_map={}, output=tmp_path / "grid", source_hashes={})
    def fail_second(task):
        if task[1] == 30:
            raise ValueError("failed candidate")
        return {"name": "entry=20,exit=5", "returns": series([.01, -.01] * 10)}
    monkeypatch.setattr(optimize, "evaluate_one_candidate", fail_second)
    rows = optimize._evaluate_grid({}, [(20, 5), (30, 5)], 10000, 1, journal)
    assert len(rows) == 2 and rows[1]["status"] == "failed"
    assert candidate_panel_evidence({row["name"]: row["returns"] for row in rows})["status"] == "invalid"


def test_identity_detects_data_mutation_without_rewriting_registration(tmp_path):
    frame = pd.DataFrame({"close": [1., 2.]}, index=pd.date_range("2024-01-01", periods=2))
    journal = ResearchEvidenceRun(candidates=["a"], parameters={}, data_map={"X": frame},
        output=tmp_path / "identity", source_hashes={})
    original = (journal.output / "registration.json").read_bytes()
    frame.iloc[0, 0] = 3.
    assert journal.verify_identity({"X": frame})["identity_changed"]
    assert (journal.output / "registration.json").read_bytes() == original


def test_shared_family_includes_pbo_declared_tail_and_joint_reality_check():
    from analysis.paper_validation import cscv_pbo, white_reality_check
    rng = np.random.default_rng(18)
    panel = pd.DataFrame({"a": series(rng.normal(.001, .01, 131)),
                          "b": series(rng.normal(.0005, .01, 131))})
    result = candidate_panel_evidence(dict(panel.items()), bootstrap_iterations=100, seed=17)
    protocol = result["pbo"]["axis_protocol"]
    assert protocol["policy"] == "leading_multiple_of_groups"
    assert protocol["full_observations"] == 131 and protocol["used_observations"] == 128
    assert protocol["excluded_tail_count"] == 3
    assert protocol["excluded_tail_timestamps"] == [t.isoformat() for t in panel.index[-3:]]
    expected_pbo = cscv_pbo(panel.iloc[:128], n_groups=8, family_complete=False)
    assert result["pbo"]["diagnostic_probability"] == expected_pbo["diagnostic_probability"]
    benchmark = pd.Series(0., index=panel.index)
    for block in (5, 20, 60):
        expected = white_reality_check(panel, benchmark, iterations=100, block_length=block,
                                      bootstrap="circular", family_complete=False, seed=17)
        actual = result["reality_check"][str(block)]
        assert actual["observations"] == 131
        assert actual["candidate_ids"] == ["a", "b"]
        assert actual["diagnostic_p_value"] == expected["diagnostic_p_value"]
        assert actual["p_value"] is None and not actual["admission_eligible"]
    assert len(result["panel"]["timestamps"]) == 131
    assert result["benchmark"]["returns"] == [0.] * 131
    assert result["pbo"]["probability"] is None


def test_explicit_benchmark_is_used_and_bad_benchmark_invalidates_all_statistics():
    rng = np.random.default_rng(19)
    panel = {"a": series(rng.normal(0, .005, 128)), "b": series(rng.normal(0, .005, 128))}
    benchmark = series([.1] * 128).rename("declared_control")
    result = candidate_panel_evidence(panel, benchmark_returns=benchmark, bootstrap_iterations=100)
    assert result["benchmark"]["kind"] == "supplied_same_axis_benchmark"
    assert result["benchmark"]["name"] == "declared_control"
    assert all(row["diagnostic_p_value"] == 1. for row in result["reality_check"].values())
    bad = candidate_panel_evidence(panel, benchmark_returns=benchmark.iloc[1:], bootstrap_iterations=100)
    assert bad["status"] == "invalid" and bad["pbo"]["status"] == "invalid"
    assert all(row["status"] == "invalid" for row in bad["dsr"].values())
    assert all(row["status"] == "invalid" for row in bad["reality_check"].values())


def test_short_panel_is_insufficient_without_deleting_flat_candidates():
    small = candidate_panel_evidence({"a": series([.01, -.01, .02, -.02, .01, -.01, .03]),
                                      "flat": series([0.] * 7)})
    assert small["protocol"]["bootstrap_iterations"] == 500
    assert small["pbo"]["status"] == "insufficient"
    assert small["pbo"]["axis_protocol"]["used_observations"] == 0
    assert small["pbo"]["axis_protocol"]["excluded_tail_count"] == 7
    assert all(row["status"] == "insufficient" for row in small["reality_check"].values())
    large = candidate_panel_evidence({"a": series([.01, -.01] * 64), "flat": series([0.] * 128)},
                                      bootstrap_iterations=100)
    assert large["candidate_ids"] == ["a", "flat"]
    assert large["pbo"]["status"] == "insufficient"
    assert large["pbo"]["candidate_ids"] == ["a", "flat"]
    assert all(row["candidate_ids"] == ["a", "flat"] for row in large["reality_check"].values())


def test_window_scores_keep_first_period_loss_and_costs():
    from analysis.walk_forward import _score

    loser = series([-.50, .10, .10])
    winner = series([.01, .01, .01])
    assert _score(loser, "TotalReturn") == pytest.approx(-.395)
    assert _score(winner, "TotalReturn") == pytest.approx(.030301)
    assert _score(loser, "TotalReturn") < _score(winner, "TotalReturn")
    assert _score(loser, "SharpeRatio") == pytest.approx(
        loser.mean() / loser.std(ddof=1) * np.sqrt(365.25))
    assert _score(loser, "CAGR", initial_time=loser.index[0] - pd.Timedelta(days=1)) == pytest.approx(
        .605 ** (365.25 / 3) - 1)
    assert _score(series([-.01]), "TotalReturn") == pytest.approx(-.01)


def test_window_cagr_uses_opening_time_without_guessing_irregular_duration():
    from analysis.walk_forward import _score

    returns = pd.Series([.01, .02, -.005], index=pd.to_datetime(
        ["2025-01-02", "2025-01-03", "2025-01-08"], utc=True))
    assert _score(returns, "CAGR") is None
    assert _score(returns, "CAGR", initial_time=pd.Timestamp("2025-01-01", tz="UTC")) == pytest.approx(
        (1.01 * 1.02 * .995) ** (365.25 / 7) - 1)
