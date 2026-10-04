from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from analysis.paper_portfolio import (PaperSpec, capacity_cost_study, cost_aware_target,
                                      default_paper_specs, run_paper_replay,
                                      _limit_target_turnover, _target)


def frames(n=100, volume=100_000., slope=.002):
    index = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = 100*np.exp(np.arange(n)*slope+np.sin(np.arange(n))*.003)
    return {symbol: pd.DataFrame({"open": close, "high": close*1.01,
        "low": close*.99, "close": close, "volume": volume}, index=index)
        for symbol in ("BTC/USDT", "ETH/USDT")}


def basic(**kwargs):
    return PaperSpec(lookback=5, vol_window=5, warmup_bars=5, rebalance_every=2, **kwargs)


def test_cash_is_actual_unchanged_cash():
    result = run_paper_replay(frames(), basic(signal="cash"))
    assert result["fills"].empty
    assert (result["equity"].equity == 10000).all()
    assert (result["returns"] == 0).all()
    assert result["accounting"]["ok"]


def test_next_bar_fills_and_future_prefix_invariance():
    data = frames()
    baseline = run_paper_replay(data, basic())
    changed = {s: df.copy() for s, df in data.items()}
    cut = data["BTC/USDT"].index[60]
    for df in changed.values():
        df.loc[cut:, ["open", "high", "low", "close"]] *= 3
        df.loc[cut:, "volume"] *= .1
    future = run_paper_replay(changed, basic())
    pd.testing.assert_frame_equal(baseline["equity"].loc[:cut-pd.Timedelta(days=1)],
                                  future["equity"].loc[:cut-pd.Timedelta(days=1)])
    fills = baseline["fills"]
    assert (pd.to_datetime(fills.fill_time) > pd.to_datetime(fills.signal_time)).all()
    first = fills.iloc[0]
    assert first.theoretical_price == pytest.approx(data[first.symbol].loc[first.fill_time, "open"])
    assert baseline["accounting"]["ok"]


def test_capacity_constraint_real_fills_and_unfilled_not_become_inventory():
    data = frames(volume=1.)
    result = run_paper_replay(data, basic(signal="equal_weight", participation_rate=.01), initial_capital=1e6)
    fills = result["fills"]
    assert len(fills)
    per_bar = fills.groupby(["fill_time", "symbol"])["qty"].sum()
    assert per_bar.max() <= .01+1e-12
    assert result["equity"].gross_weight.max() < .001
    assert (result["equity"].cash >= 0).all()
    assert result["accounting"]["ok"]


def test_flat_prices_higher_cost_reduces_account_equity_and_bridge():
    data = frames(slope=0.)
    for df in data.values():
        df[["open", "close"]] = 100.
        df["high"], df["low"] = 101., 99.
    spec = basic(signal="equal_weight", holding_cost_annual=.02)
    low = run_paper_replay(data, spec)
    high = run_paper_replay(data, spec, cost_multiplier=1.5)
    assert high["equity"].equity.iloc[-1] < low["equity"].equity.iloc[-1]
    for result in (low, high):
        a = result["accounting"]
        assert a["ok"]
        assert a["final_equity"] == pytest.approx(10000-a["commission"]-a["slippage"]-a["holding_cost"])
        assert a["holding_cost"] > 0
        assert (result["equity"].cash >= 0).all()


def test_risk_scaling_is_lagged_bounded_and_inverse_variance_distinct():
    data = frames(slope=.01)
    fixed = run_paper_replay(data, basic())
    invvol = run_paper_replay(data, basic(scaling="inverse_vol", target_vol=.01))
    invvar = run_paper_replay(data, basic(scaling="inverse_variance", target_vol=.01))
    for result in (fixed, invvol, invvar):
        assert result["equity"].target_gross.max() <= .9+1e-12
    assert invvar["equity"].target_gross.max() < invvol["equity"].target_gross.max()
    assert invvol["equity"].target_gross.max() < fixed["equity"].target_gross.max()


def test_cost_optimizer_shrinks_on_cost_and_has_cash_fallback():
    mu, cov, old = [.005, .004], np.eye(2)*.001, np.zeros(2)
    low, status = cost_aware_target(mu, cov, old, gross_cap=.9, trading_cost=.0001)
    high, _ = cost_aware_target(mu, cov, old, gross_cap=.9, trading_cost=.02)
    assert status == "optimal"
    assert low.sum() > high.sum()
    assert low.sum() <= .9+1e-8
    fallback, status = cost_aware_target([np.nan, .1], cov, old, gross_cap=.9, trading_cost=.001)
    assert (fallback == 0).all()
    assert "cash_fallback" in status


def test_optimizer_matches_diagonal_solution_and_shared_budget():
    target, status = cost_aware_target([.03, .02], np.eye(2)*.1, [0., 0.],
        gross_cap=.9, trading_cost=.01, risk_aversion=1., impact_penalty=0.)
    assert status == "optimal"
    assert target == pytest.approx([.2, .1], abs=1e-8)
    capped, status = cost_aware_target([.03, .03], np.eye(2)*.01, [0., 0.],
        gross_cap=.9, trading_cost=0., risk_aversion=1., impact_penalty=0.)
    assert status == "optimal"
    assert capped == pytest.approx([.45, .45], abs=1e-8)


def test_partial_and_no_trade_methods_are_real_order_policies():
    data = frames()
    immediate = run_paper_replay(data, basic(signal="equal_weight"))
    partial = run_paper_replay(data, basic(signal="equal_weight", rebalance="partial"))
    assert partial["fills"].iloc[0].qty == pytest.approx(immediate["fills"].iloc[0].qty*.5)
    none = run_paper_replay(data, basic(signal="equal_weight", rebalance="no_trade", no_trade_band=.5))
    assert none["fills"].empty


def test_stop_observed_at_close_executes_next_open_not_threshold():
    data = frames(n=40)
    for df in data.values():
        df.loc[df.index[20]:, ["open", "high", "low", "close"]] *= .5
    result = run_paper_replay(data, basic(signal="equal_weight", stop_loss=.10))
    stopped = result["fills"].query("exit_reason == 'paper_close_observed_stop'")
    assert len(stopped)
    for _, fill in stopped.iterrows():
        assert fill.fill_time == fill.signal_time+pd.Timedelta(days=1)
        assert fill.theoretical_price == pytest.approx(data[fill.symbol].loc[fill.fill_time, "open"])
    assert result["accounting"]["ok"]


def test_same_prices_assets_and_budget_across_fixed_family_and_capacity_grid():
    specs = default_paper_specs()
    assert len({s.name for s in specs}) == len(specs)
    assert {s.max_gross for s in specs} == {.9}
    assert {s.commission_rate for s in specs} == {.001}
    grid = capacity_cost_study(frames(n=20, volume=10), basic(signal="equal_weight"), capitals=(1e4, 1e6))
    assert len(grid) == 4
    assert grid.accounting_ok.all()
    assert set(grid.cost_multiplier) == {1., 1.5}


def test_invalid_market_data_fails_closed():
    data = frames()
    data["BTC/USDT"].iloc[0, 0] = np.nan
    with pytest.raises(ValueError, match="nonfinite"):
        run_paper_replay(data, basic())
    data = frames()
    data["ETH/USDT"] = data["ETH/USDT"].iloc[1:]
    with pytest.raises(ValueError, match="time axes differ"):
        run_paper_replay(data, basic())


def test_external_targets_are_frozen_previous_bar_and_hashed():
    data = frames(n=30)
    index = data["BTC/USDT"].index
    targets = pd.DataFrame(0., index=index, columns=list(data))
    targets.loc[index[10]:, "BTC/USDT"] = .5
    baseline = run_paper_replay(data, basic(), target_overrides=targets)
    assert baseline["fills"].iloc[0].signal_time == index[10]
    assert baseline["fills"].iloc[0].fill_time == index[11]
    altered = targets.copy()
    altered.loc[index[20]:, "BTC/USDT"] = 0
    future = run_paper_replay(data, basic(), target_overrides=altered)
    pd.testing.assert_frame_equal(baseline["equity"].loc[:index[19]], future["equity"].loc[:index[19]])
    assert baseline["summary"]["target_source"] == "external_targets_research"
    assert baseline["summary"]["target_overrides_sha256"] != future["summary"]["target_overrides_sha256"]
    altered.iloc[0, 0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        run_paper_replay(data, basic(), target_overrides=altered)


@pytest.mark.parametrize("value", [-.1, float("nan"), float("inf")])
def test_invalid_turnover_caps_and_cost_penalties_are_rejected(value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        basic(max_turnover_weight=value)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        basic(cost_penalty_scale=value)


def test_default_cost_aware_arm_has_hard_cap_without_expanding_family():
    specs = default_paper_specs()
    assert len(specs) == 14
    assert next(s for s in specs if s.name == "trend_60_cost_aware").max_turnover_weight == .25


@pytest.mark.parametrize("external", [False, True])
def test_native_and_external_turnover_limits_reach_actual_account_fills(external):
    data = frames(n=40)
    spec = basic(signal="equal_weight", max_turnover_weight=.15)
    overrides = None
    if external:
        index = data["BTC/USDT"].index
        overrides = pd.DataFrame({"BTC/USDT": .9, "ETH/USDT": 0.}, index=index)
        overrides.loc[index[20]:] = [0., .9]
    result = run_paper_replay(data, spec, target_overrides=overrides)
    assert not result["fills"].empty
    normal = result["equity"].query("not turnover_cap_override")
    assert normal.target_turnover_weight.max() <= .15+1e-12
    assert result["equity"].turnover_constraint_binding.any()
    for _, group in result["decisions"].groupby("execution_bar"):
        change = (group.target_weight-group.existing_weight).abs().sum()
        if not group.turnover_cap_override.any():
            assert change <= .15+1e-12
    assert result["accounting"]["ok"]
    assert (result["equity"].cash >= 0).all()
    assert "not realised fill notional" in result["summary"]["turnover_constraint_semantics"]
    frozen = run_paper_replay(data, replace(spec, max_turnover_weight=0.), target_overrides=overrides)
    assert frozen["fills"].empty
    assert frozen["accounting"]["final_equity"] == 10000


def test_stop_reductions_override_turnover_cap_and_are_audited():
    data = frames(n=45)
    for frame in data.values():
        frame.loc[frame.index[25]:, ["open", "high", "low", "close"]] *= .5
    result = run_paper_replay(data, basic(signal="equal_weight", max_turnover_weight=.1, stop_loss=.1))
    stopped = result["decisions"].query("stop_triggered")
    assert not stopped.empty and stopped.turnover_cap_override.any()
    assert (stopped.target_weight == 0).all()
    assert all("stop_loss" in reason for reason in stopped.turnover_override_reasons)
    assert len(result["fills"].query("exit_reason == 'paper_close_observed_stop'"))
    assert result["summary"]["turnover_cap_override_decisions"] > 0
    assert result["accounting"]["ok"]


def test_drift_gross_reduction_has_priority_over_turnover_and_schedule():
    limited, audit = _limit_target_turnover(basic(max_gross=.5, max_turnover_weight=.05),
        [.9, 0.], [.9, 0.], [False, False])
    assert limited == pytest.approx([.5, 0.])
    assert audit["target_turnover_weight"] == pytest.approx(.4)
    assert audit["turnover_cap_override"] and audit["turnover_override_reasons"] == ["gross_cap"]
    data = frames(n=50)
    for frame in data.values():
        frame.loc[frame.index[26]:, ["open", "high", "low", "close"]] *= 20.
    result = run_paper_replay(data, basic(signal="equal_weight", max_gross=.5, max_turnover_weight=.1))
    forced = result["decisions"].query("gross_cap_triggered and turnover_cap_override")
    assert not forced.empty
    assert all("gross_cap" in reason for reason in forced.turnover_override_reasons)
    assert result["equity"].target_gross.max() <= .5+1e-12
    assert result["accounting"]["ok"]


def test_cost_penalty_control_has_same_forecast_risk_and_realised_cost_contract():
    data = frames(n=40, slope=.002)
    # Independent return variation avoids testing the separate singular-
    # covariance cash-fallback policy in this cost-penalty comparison.
    eth = data["ETH/USDT"]
    eth["close"] = 100*np.exp(np.arange(len(eth))*.0018+np.cos(np.arange(len(eth)))*.004)
    eth["open"], eth["high"], eth["low"] = eth.close, eth.close*1.01, eth.close*.99
    close = pd.DataFrame({symbol: frame.close for symbol, frame in data.items()})
    volume = pd.DataFrame({symbol: frame.volume for symbol, frame in data.items()})
    spec = basic(cost_aware=True, commission_rate=.01, max_turnover_weight=.25)
    without = replace(spec, cost_penalty_scale=0.)
    # Identical history, covariance, existing exposure, gross cap and aversion;
    # only the objective's cost terms change.
    charged, _, status = _target(spec, close, volume, 20, np.zeros(2), 1.)
    uncharged, _, control_status = _target(without, close, volume, 20, np.zeros(2), 1.)
    assert status == control_status == "optimal"
    assert uncharged.sum() > charged.sum()
    actual = run_paper_replay(data, without)
    assert actual["accounting"]["ok"] and actual["accounting"]["commission"] > 0
    assert actual["spec"]["commission_rate"] == spec.commission_rate
    assert actual["spec"]["risk_aversion"] == spec.risk_aversion
    assert actual["summary"]["cost_penalty_scale"] == 0
