import pytest

from analysis.paper_risk import (StressSpec, empirical_cvar, joint_stress_scenario,
                                 risk_diagnostics)


def test_empirical_tail_fractional_boundary_and_cdar_hand_calculation():
    assert empirical_cvar([0, 1, 2, 3], .5) == pytest.approx(2.5)
    assert empirical_cvar([0, 1, 2, 3], .625) == pytest.approx((3+2*.5)/1.5)
    result = risk_diagnostics([100, 90, 80, 100, 95], returns=[0, -.1, -.1, .25, -.05], alpha=.6)
    assert result["cvar_loss"] == pytest.approx(.1)
    assert result["cdar"] == pytest.approx(.15)
    assert result["max_drawdown"] == pytest.approx(.2)
    assert result["max_drawdown_duration_bars"] == 2
    assert result["current_drawdown_duration_bars"] == 1


def test_tail_validation_and_flat_equity():
    assert risk_diagnostics([100, 100, 100])["max_drawdown"] == 0
    for values, alpha in (([], .95), ([1, float("nan")], .95), ([1, 2], 1)):
        with pytest.raises(ValueError):
            empirical_cvar(values, alpha)


def test_initial_trade_cost_is_included_in_drawdown():
    result = risk_diagnostics([99., 99.], returns=[-.01, 0.])
    assert result["max_drawdown"] == pytest.approx(.01)
    assert result["max_drawdown_duration_bars"] == 2


def test_joint_shock_hand_calculation_capacity_cash_bridge():
    spec = StressSpec(common_price_shock=-.5, spread_bps=0., depth_fraction=.1,
        liquidation_participation=.1, commission_rate=0., impact_coefficient=0., collateral_haircut=0.)
    result = joint_stress_scenario({"BTC": 10.}, {"BTC": 100.}, 100.,
        depth_notional={"BTC": 1000.}, spec=spec)
    assert result["initial_equity"] == 1100
    assert result["price_pnl"] == -500
    row = result["positions"][0]
    assert row["exit_quantity"] == pytest.approx(.2)
    assert row["remaining_quantity"] == pytest.approx(9.8)
    assert result["cash_after"] == pytest.approx(110.)
    assert result["final_equity"] == pytest.approx(600.)
    assert result["accounting_ok"]
    assert "not a venue" in result["scope"]


def test_joint_stress_spread_depth_common_loading_and_margin():
    spec = StressSpec(common_price_shock=-.3, correlation=1., maintenance_margin_rate=.9,
                      margin_multiplier=2.)
    result = joint_stress_scenario({"BTC": 10., "ETH": 20.}, {"BTC": 100., "ETH": 50.}, 0.,
        depth_notional={"BTC": 100., "ETH": 100.}, spec=spec,
        idiosyncratic_shocks={"BTC": .5, "ETH": -.9})
    assert all(row["shock"] == -.3 for row in result["positions"])
    assert result["commission"] > 0
    assert result["slippage"] > 0
    assert result["hypothetical_margin_shortfall"] > 0
    assert result["accounting_ok"]
    assert all(row["capacity_constrained"] for row in result["positions"])


def test_zero_depth_cannot_be_sold():
    result = joint_stress_scenario({"BTC": 1.}, {"BTC": 100.}, 10., depth_notional={"BTC": 0.})
    assert result["positions"][0]["exit_quantity"] == 0
    assert result["cash_after"] == 10
    assert result["accounting_ok"]
