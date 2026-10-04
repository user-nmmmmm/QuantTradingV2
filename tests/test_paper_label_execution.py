import pandas as pd
import pytest

from analysis.paper_labels import BarrierConfig, LabelCosts
from analysis.paper_label_execution import refine_barrier_labels, reconcile_label_cashflows


def inputs():
    daily = pd.DataFrame({"open": [100., 100.], "high": [100., 103.],
        "low": [100., 98.], "close": [100., 100.], "volume": [1000., 1000.]},
        index=pd.date_range("2020-01-01", periods=2, tz="UTC"))
    hourly = pd.DataFrame({"open": 100., "high": 100., "low": 100.,
        "close": 100., "volume": 40.}, index=pd.date_range("2020-01-02", periods=24, freq="h", tz="UTC"))
    hourly.loc[hourly.index[0], "high"] = 103.
    hourly.loc[hourly.index[12], "low"] = 98.
    candidate = {"candidate_id": "one", "symbol": "BTC/USDT", "direction": "long",
        "timestamp": "2020-01-01T00:00:00Z", "context": {
            "available_at": "2020-01-02T00:00:00Z", "timeframe": "1d"}}
    return daily, hourly, candidate


def refine(daily, hourly, candidate):
    return refine_barrier_labels({"BTC/USDT": daily}, [candidate],
        config=BarrierConfig(max_holding_bars=1), costs=LabelCosts(commission_bps_per_side=5),
        as_of="2020-01-04", fine_frames=None if hourly is None else {"BTC/USDT": hourly})


def test_complete_hourly_path_resolves_first_touch_and_retains_availability():
    daily, hourly, candidate = inputs()
    result = refine(daily, hourly, candidate)
    row = result["outcomes"][0]
    assert row["barrier"] == "profit_take" and row["exit_reference"] == 102
    assert row["label_end_time"] == "2020-01-02T01:00:00+00:00"
    assert row["available_at"] == "2020-01-03T00:00:00+00:00"
    assert row["net_return_bps"] == pytest.approx(189.9)
    assert result["summary"]["resolved_with_fine_data"] == 1


@pytest.mark.parametrize("case,reason", [("missing", "fine_interval_incomplete"),
    ("mismatch", "fine_parent_mismatch"), ("delayed", "fine_data_not_available"),
    ("none", "fine_data_missing"), ("double", "fine_path_still_ambiguous")])
def test_unproven_fine_path_never_upgrades_label(case, reason):
    daily, hourly, candidate = inputs()
    if case == "missing":
        hourly = hourly.iloc[:-1]
    elif case == "mismatch":
        hourly.loc[hourly.index[-1], "close"] = 100.1
        hourly.loc[hourly.index[-1], "high"] = 100.1
    elif case == "delayed":
        hourly["available_at"] = pd.Timestamp("2020-01-05", tz="UTC")
    elif case == "none":
        hourly = None
    elif case == "double":
        hourly.loc[hourly.index[0], "low"] = 98.
    row = refine(daily, hourly, candidate)["outcomes"][0]
    assert row["refinement_status"] == reason
    assert row["ambiguous"] and not row["training_eligible"]
    assert row["barrier"] == "stop_loss"


def test_cashflow_bridge_has_real_price_fees_financing_and_deduplicates():
    label = {"candidate_id": "one", "direction": "long", "net_return_bps": 180}
    entry = {"candidate_id": "one", "fill_id": "a", "leg": "entry", "qty": 2,
             "price": 100, "fee_quote": .1, "quote_currency": "USDT", "occurred_at": "2020-01-02"}
    exit_ = {**entry, "fill_id": "b", "leg": "exit", "price": 102, "occurred_at": "2020-01-03"}
    financing = {"candidate_id": "one", "event_id": "f", "cost_quote": .2, "quote_currency": "USDT"}
    out = reconcile_label_cashflows(label, [entry, exit_, entry], [financing, financing], financing_complete=True)
    assert out["net_pnl"] == pytest.approx(3.6)
    assert out["actual_net_bps"] == pytest.approx(180)
    assert out["label_error_bps"] == pytest.approx(0)
    assert out["accounting_residual"] == 0
    assert reconcile_label_cashflows(label, [entry, exit_])["actual_net_bps"] is None
    with pytest.raises(ValueError, match="conflicting duplicate"):
        reconcile_label_cashflows(label, [entry, {**entry, "price": 101}])
    with pytest.raises(ValueError, match="currency"):
        reconcile_label_cashflows(label, [entry, {**exit_, "quote_currency": "USD"}])
