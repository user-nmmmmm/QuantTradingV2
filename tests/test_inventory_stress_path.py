import pandas as pd
import pytest

from analysis.paper_risk import StressSpec, liquidation_stress_path, stress_inventory_path


def spec():
    return StressSpec(common_price_shock=-.5, spread_bps=0, impact_coefficient=0,
        commission_rate=0, depth_fraction=.1, liquidation_participation=.1)


def test_transient_largest_inventory_detected_even_when_terminal_account_is_cash():
    dates = pd.date_range("2020-01-01", periods=3, tz="UTC")
    market = {"BTC": pd.DataFrame({"close": [100., 100., 100.]}, index=dates)}
    equity = pd.DataFrame({"equity": [1100.] * 3, "cash": [100., 0., 1100.],
        "qty_BTC": [10., 11., 0.]}, index=dates)
    result = stress_inventory_path(equity, market, depth_notional={"BTC": 1000}, spec=spec())
    assert result["summary"]["observations"] == 3
    assert result["summary"]["worst_timestamp"] == str(dates[1])
    assert result["summary"]["worst_loss_fraction"] == .5
    assert result["path"].loss_fraction.iloc[-1] == 0
    assert result["summary"]["accounting_ok"]


def test_one_shock_finite_rounds_and_no_infinite_liquidity():
    result = liquidation_stress_path({"BTC": 10.}, {"BTC": 100.}, 100.,
        depth_notional={"BTC": 1000.}, spec=spec(), max_steps=3)
    assert [row["price_pnl"] for row in result["steps"]] == [-500., 0., 0.]
    assert result["steps"][-1]["remaining_quantities"]["BTC"] == pytest.approx(9.4)
    assert result["final_equity"] == pytest.approx(600.)
    assert result["status"] == "residual_inventory_at_horizon"
    assert result["accounting_ok"]


def test_zero_depth_and_insufficient_cash_retains_unpaid_carry_in_equity():
    result = liquidation_stress_path({"BTC": 1.}, {"BTC": 100.}, 0.,
        depth_notional={"BTC": 0.}, spec=spec(), holding_cost_bps_per_step=100.)
    assert len(result["steps"]) == 1 and result["status"] == "unpaid_scenario_carry"
    assert result["final_equity"] == pytest.approx(49.5)
    assert result["steps"][0]["remaining_quantities"]["BTC"] == 1
    assert result["accounting_ok"]


def test_path_refuses_inconsistent_inventory_marks():
    dates = pd.date_range("2020-01-01", periods=1)
    with pytest.raises(ValueError, match="reconcile"):
        stress_inventory_path(pd.DataFrame({"equity": [100.], "cash": [0.], "qty_BTC": [2.]}, index=dates),
            {"BTC": pd.DataFrame({"close": [100.]}, index=dates)}, depth_notional={"BTC": 1000.})
