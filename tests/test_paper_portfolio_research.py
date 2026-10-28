"""Factor/risk/portfolio research with causal callbacks and real account bridges."""

import numpy as np
import pandas as pd
import pytest

from analysis.paper_portfolio import PaperSpec, run_paper_replay
from analysis.paper_portfolio_research import (
    factor_attribution_general,
    covariance_risk_comparison,
    run_covariance_portfolio_study,
    run_allocation_portfolio_study,
    allocation_policy_ablation,
)
from core.planning.cost_aware_allocation import CostAwareAllocationPolicy


def frames(n=90):
    index = pd.date_range("2024-01-01", periods=n, tz="UTC")
    rng = np.random.default_rng(38)
    returns = rng.normal(0.001, 0.006, (n, 3))
    returns[:, 1] += 0.4 * returns[:, 0]
    result = {}
    for j, symbol in enumerate(("BTC/USDT", "ETH/USDT", "SOL/USDT")):
        close = 100 * np.cumprod(1 + returns[:, j])
        result[symbol] = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 100000.0,
            },
            index=index,
        )
    return result


def factor_fixture(n=80):
    index = pd.date_range("2024-01-01", periods=n, tz="UTC")
    x = pd.DataFrame(
        np.random.default_rng(4).normal(0, 0.01, (n, 2)), index=index, columns=["mkt", "mom"]
    )
    y = (0.002 + 1.5 * x.mkt - 0.4 * x.mom).rename("strategy")
    return y, x


def test_general_factor_fit_recovers_independent_known_intercept_and_slopes():
    y, x = factor_fixture()
    result = factor_attribution_general(
        y, x, factor_ids=["mkt", "mom"], factor_versions={"mkt": "v1", "mom": "v2"}, iterations=100
    )
    assert result["status"] == "diagnostic"
    assert result["alpha_daily"] == pytest.approx(0.002, abs=1e-15)
    assert result["beta"] == pytest.approx({"mkt": 1.5, "mom": -0.4}, abs=1e-12)
    assert max(map(abs, result["residuals"])) < 1e-15
    assert result["rank"] == 3 and result["sample_size"] == 80
    assert result["intervals"]["valid_block_samples"] == 100
    assert not result["admission_eligible"] and not result["full_market_replication"]


def test_collinear_factors_return_insufficient_without_fake_coefficients():
    y, x = factor_fixture()
    x["mom"] = x["mkt"] * 2
    result = factor_attribution_general(
        y, x, factor_ids=["mkt", "mom"], factor_versions={"mkt": "v1", "mom": "v1"}, iterations=0
    )
    assert result["status"] == "insufficient" and result["alpha_daily"] is None


@pytest.mark.parametrize(
    "bad", ["different_axis", "duplicate", "missing", "future_availability", "missing_version"]
)
def test_attribution_bad_inputs_never_align_or_fill_silently(bad):
    y, x = factor_fixture()
    kwargs = {
        "factor_ids": ["mkt", "mom"],
        "factor_versions": {"mkt": "v1", "mom": "v1"},
        "iterations": 0,
    }
    if bad == "different_axis":
        y.index = y.index + pd.Timedelta(days=1)
    if bad == "duplicate":
        x.index = pd.DatetimeIndex([x.index[0]] + list(x.index[:-1]))
    if bad == "missing":
        x.iloc[4, 0] = np.nan
    if bad == "missing_version":
        kwargs["factor_versions"] = {"mkt": "v1"}
    if bad == "future_availability":
        kwargs.update(
            as_of=x.index[-1],
            factor_available_at=pd.DataFrame(
                x.index[-1] + pd.Timedelta(days=1), index=x.index, columns=x.columns
            ),
        )
    with pytest.raises(ValueError):
        factor_attribution_general(y, x, **kwargs)


def test_existing_attribution_entry_accepts_versioned_factor_ids():
    from analysis.paper_study import factor_attribution

    y, x = factor_fixture()
    result = factor_attribution(
        y, x, factor_ids=["mkt", "mom"], factor_versions={"mkt": "v1", "mom": "v1"}, iterations=0
    )
    assert result["schema"] == "factor-attribution/v2"
    assert result["alpha_daily"] == pytest.approx(0.002)


def test_risk_comparison_has_fixed_relative_weights_and_nonoverlapping_later_blocks():
    data = frames(110)
    close = pd.DataFrame({s: f.close for s, f in data.items()})
    returns = close.pct_change(fill_method=None).iloc[1:]
    result = covariance_risk_comparison(returns, lookback=60, horizon=10)
    assert result["engineering"] == "pass"
    assert result["candidate_ids"] == ["sample", "fixed_diag_020", "lw_constant_correlation"]
    for offset in range(0, len(result["rows"]), 3):
        group = result["rows"][offset : offset + 3]
        assert (
            group[0]["relative_base_weights"]
            == group[1]["relative_base_weights"]
            == group[2]["relative_base_weights"]
        )
        assert group[0]["training_last"] < group[0]["decision_at"]
    assert result["portfolio_return"] is None


def test_real_covariance_account_study_uses_three_arms_and_reconciles():
    result = run_covariance_portfolio_study(frames(35), lookback=8)
    assert result["engineering"] == "pass"
    assert len(result["arms"]) == 3
    for arm in result["arms"].values():
        assert arm["accounting"]["ok"] and arm["accounting"]["minimum_cash"] >= -1e-7
        assert (
            pd.to_datetime(arm["fills"]["fill_time"]) > pd.to_datetime(arm["fills"]["signal_time"])
        ).all()


def test_two_asset_lw_and_sample_account_paths_are_identical():
    data = frames(30)
    del data["SOL/USDT"]
    result = run_covariance_portfolio_study(data, lookback=8)
    pd.testing.assert_series_equal(
        result["arms"]["sample"]["equity"].equity,
        result["arms"]["lw_constant_correlation"]["equity"].equity,
    )


def test_four_allocation_accounts_keep_actual_costs_fixed_and_constant_provider_equivalent():
    result = run_allocation_portfolio_study(frames(28), lookback=8)
    assert result["engineering"] == "pass"
    assert len(result["arms"]) == 4
    pd.testing.assert_series_equal(
        result["arms"]["native_constant_immediate"]["equity"].equity,
        result["arms"]["provider_immediate"]["equity"].equity,
    )
    for arm in result["arms"].values():
        assert arm["accounting"]["ok"] and arm["accounting"]["minimum_cash"] >= -1e-7
        assert arm["spec"]["commission_rate"] == 0.001
    assert result["conditional_multi_period"]["status"] == "not_triggered"


def test_callback_has_only_past_returns_and_actual_existing_holdings():
    observations = []

    def provider(context):
        assert (
            context["returns"].empty
            or context["returns"].index[-1].isoformat() == context["source_bar"]
        )
        observations.append(context["existing_weights"].copy())
        return np.full(len(context["symbols"]), 0.1), {"status": "fixed_fixture"}

    result = run_paper_replay(
        frames(20), PaperSpec(lookback=5, vol_window=5, warmup_bars=5), target_provider=provider
    )
    assert result["accounting"]["ok"]
    assert observations[0].sum() == 0 and observations[-1].sum() > 0
    assert result["target_provider_audit"]


def test_callback_envelope_violation_is_refused_before_ordering():
    with pytest.raises(ValueError, match="envelope"):
        run_paper_replay(
            frames(10),
            PaperSpec(lookback=5, vol_window=5, warmup_bars=5),
            target_provider=lambda context: (np.ones(3), {"status": "bad"}),
        )


def test_mandatory_reduction_is_not_part_of_discretionary_turnover():
    policy = CostAwareAllocationPolicy(enabled=True, max_turnover_weight=0.0, max_gross_weight=0.9)
    result = allocation_policy_ablation(
        np.array([0.05]), np.array([[0.01]]), np.array([0.8]), np.array([0.2]), policy=policy
    )
    for row in result["arms"].values():
        assert row["target_weights"] == [0.2]
        assert row["audit"]["mandatory_turnover_weight"] == pytest.approx(0.6)
        assert row["audit"]["discretionary_turnover_weight"] == 0
    assert result["actual_costs_posted"] == 0


def test_historical_csv_reader_stops_before_forbidden_future_numeric_values(tmp_path):
    from scripts.run_paper_portfolio_research import _read_fixed_window

    path = tmp_path / "bars.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n2024-01-01,1,1,1,1,10\n2024-01-02,2,2,2,2,10\n2024-01-03,FORBIDDEN,FUTURE,VALUES,NOT,NUMERIC\n"
    )
    result = _read_fixed_window(path, "2024-01-01", "2024-01-02")
    assert len(result) == 2 and result.iloc[-1].close == 2


def test_historical_csv_reader_refuses_missing_internal_bar(tmp_path):
    from scripts.run_paper_portfolio_research import _read_fixed_window

    path = tmp_path / "bars.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n2024-01-01,1,1,1,1,10\n2024-01-03,2,2,2,2,10\n"
    )
    with pytest.raises(ValueError, match="Missing"):
        _read_fixed_window(path, "2024-01-01", "2024-01-03")


def test_fixed_study_protocol_declares_each_family_and_preserves_engineering_boundary():
    from scripts.run_paper_portfolio_research import _protocol, ROOT
    from analysis.search_scope import validate_protocol
    import json

    path = ROOT / "config/paper_portfolio_research_20261027.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    for family, count in [("covariance", 3), ("allocation", 4)]:
        protocol = _protocol(
            config,
            family=family,
            source_hashes={"fixture": "a" * 64},
            inputs={},
            config_path=path,
            study_id="fixture-" + family,
        )
        assert validate_protocol(protocol)["attempt_budget"] == count
        assert len(set(protocol["candidate_ids"] + protocol["benchmark_ids"])) == count
        assert protocol["evaluation_kind"] == "retrospective"
        assert protocol["resampling"]["method"] == "not_applicable"
        assert not protocol["historical_global_search_complete"]


def test_covariance_target_estimator_failure_is_started_and_failed_not_left_planned(monkeypatch):
    import analysis.paper_portfolio_research as module

    def fail(*args, **kwargs):
        raise ValueError("independent estimator fixture")

    monkeypatch.setattr(module, "estimate_covariance", fail)
    events = []
    with pytest.raises(ValueError, match="estimator fixture"):
        module.run_covariance_portfolio_study(
            frames(20), lookback=8, on_arm=lambda arm, status, value: events.append((arm, status))
        )
    assert events == [("sample", "started"), ("sample", "failed")]


def test_unsupported_cost_arm_is_failed_instead_of_successful_zero_trade_ablation():
    class Unsupported:
        def estimate(self, context):
            return {"status": "unsupported", "reason": "missing_real_domain"}

    events = []
    with pytest.raises(ValueError, match="unsupported cost"):
        run_allocation_portfolio_study(
            frames(22),
            lookback=8,
            provider=Unsupported(),
            on_arm=lambda arm, status, value: events.append((arm, status)),
        )
    assert ("native_constant_immediate", "completed") in events
    assert events[-1] == ("provider_immediate", "failed")
    assert not any(arm == "provider_immediate" and status == "completed" for arm, status in events)


def test_callback_native_risk_exit_overrides_outer_turnover_and_schedule():
    seen = []

    def callback(context):
        seen.append(context)
        weights = np.full(3, 0.1) if len(seen) < 8 else np.zeros(3)
        mandatory = {s: 0.0 for s in context["symbols"]} if len(seen) >= 8 else {}
        return weights, {
            "status": "native_mandatory_reduction",
            "mandatory_reduction_weights": mandatory,
        }

    spec = PaperSpec(
        lookback=5, vol_window=5, warmup_bars=5, rebalance_every=5, max_turnover_weight=0.01
    )
    result = run_paper_replay(frames(20), spec, target_provider=callback)
    assert seen[7]["existing_weights"].sum() > 0.01
    exits = result["decisions"][result["decisions"].signal_time >= seen[7]["source_bar"]]
    assert not exits.empty and (exits.target_weight == 0.0).all()
    assert exits.turnover_cap_override.any()
    assert any(
        "callback_native_risk_envelope" in value for value in exits.turnover_override_reasons
    )
    assert result["accounting"]["ok"]
    assert "decision_cutoff" in exits and "signal_bar_time" in exits


def test_callback_cannot_label_a_new_position_as_a_mandatory_reduction():
    with pytest.raises(ValueError, match="monotone reduction"):
        run_paper_replay(
            frames(10),
            PaperSpec(lookback=5, vol_window=5, warmup_bars=5),
            target_provider=lambda context: (
                np.full(3, 0.1),
                {"status": "bad", "mandatory_reduction_weights": {"BTC/USDT": 0.1}},
            ),
        )


def test_legacy_factor_entry_keeps_known_slopes_and_exposure_only_interpretation():
    from analysis.paper_study import factor_attribution

    y, factors = factor_fixture()
    factors = factors.rename(columns={"mkt": "market", "mom": "momentum_spread"})
    result = factor_attribution(y, factors, iterations=40, block_length=7, seed=15)
    assert result["status"] == "diagnostic"
    assert result["alpha_daily"] == pytest.approx(0.002, abs=1e-15)
    assert result["market_beta"] == pytest.approx(1.5, abs=1e-12)
    assert result["momentum_proxy_beta"] == pytest.approx(-0.4, abs=1e-12)
    assert result["bootstrap_samples"] == 40
    assert result["alpha_daily_block_ci95"] == pytest.approx([0.002, 0.002], abs=1e-15)
    assert "not causal alpha" in result["interpretation"]
    assert "proxy financing" in result["unmodeled"]
    with pytest.raises(ValueError, match="explicit factor_ids"):
        factor_attribution(y, factors, as_of=factors.index[-1])
    incomplete = factors.copy()
    incomplete.iloc[0, 0] = np.nan
    assert factor_attribution(y, incomplete)["reason"] == "factor_history_incomplete"
    assert factor_attribution(y.iloc[:20], factors)["reason"] == "sample_size_or_rank"


def test_owned_external_targets_keep_candidate_identity_in_native_fills():
    data = frames(14)
    symbols = list(data)
    index = data[symbols[0]].index
    targets = pd.DataFrame(0.1, index=index, columns=symbols)
    candidates = pd.DataFrame({symbol: "frozen-" + symbol for symbol in symbols}, index=index)
    spec = PaperSpec(lookback=5, vol_window=5, warmup_bars=5, rebalance_every=1)
    result = run_paper_replay(data, spec, target_overrides=targets, candidate_targets=candidates)
    buys = result["fills"].loc[result["fills"].side == "buy"]
    assert len(buys) > 0
    owned = buys.merge(
        result["decisions"][["order_id", "candidate_id"]], on="order_id", validate="many_to_one"
    )
    assert len(owned) == len(buys)
    assert set(owned.candidate_id) == set(candidates.iloc[0])
    assert (owned.candidate_id == "frozen-" + owned.symbol).all()
    assert result["accounting"]["ok"]
    assert result["summary"]["target_source"] == "external_targets_research"
    with pytest.raises(ValueError, match="candidate ownership"):
        run_paper_replay(data, spec, candidate_targets=candidates)
    malformed = targets.iloc[1:]
    with pytest.raises(ValueError, match="full common bar index"):
        run_paper_replay(data, spec, target_overrides=malformed)
