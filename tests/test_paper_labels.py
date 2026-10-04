"""Hand-calculated offline barrier labels, availability and censoring contracts."""
from copy import deepcopy

import pandas as pd
import pytest

from analysis.paper_labels import BarrierConfig, LabelCosts, label_candidates, triple_barrier_label


def bars(count=5):
    return pd.DataFrame({"open": 100., "high": 100.5, "low": 99.5, "close": 100., "volume": 1000.},
                        index=pd.date_range("2020-01-01", periods=count, tz="UTC"))


def candidate(direction="long", **updates):
    value = {"candidate_id": "c1", "timestamp": "2020-01-01T00:00:00Z", "symbol": "BTC/USDT",
             "strategy": "Test", "direction": direction, "reference_price": 800.,
             "context": {"available_at": "2020-01-02T00:00:00Z", "timeframe": "1d"}}
    value.update(updates)
    return value


def label(frame=None, event=None, *, cutoff="2020-01-08", config=None, costs=None, split="retrospective"):
    return triple_barrier_label(bars() if frame is None else frame,
                               candidate() if event is None else event,
                               config=config or BarrierConfig(max_holding_bars=2),
                               costs=costs or LabelCosts(), as_of=cutoff, split=split)


def test_long_starts_next_actual_open_and_costs_have_explicit_notional_basis():
    frame = bars()
    frame.iloc[0] = [800, 900, 700, 800, 1000]
    frame.iloc[1] = [110, 113, 109.5, 111, 1000]
    out = label(frame, costs=LabelCosts(commission_bps_per_side=5, slippage_bps_per_side=2,
                                       spread_bps_per_side=1, impact_bps_per_side=3, carry_bps=4))
    assert out["entry_reference"] == 110
    assert out["exit_reference"] == pytest.approx(112.2)
    assert out["barrier"] == "profit_take"
    assert out["gross_return_bps"] == pytest.approx(200)
    assert out["cost_components_bps"] == pytest.approx(
        {"commission": 10.1, "slippage": 4.04, "spread": 2.02, "impact": 6.06, "carry": 4})
    assert out["net_return_bps"] == pytest.approx(173.78)
    assert out["label_end_time"] == out["available_at"] == "2020-01-03T00:00:00+00:00"
    assert out["training_eligible"] and out["label"] == 1


def test_short_take_profit_and_commission_are_charged_on_both_notionals():
    frame = bars()
    frame.iloc[1] = [100, 100.5, 97, 99, 1000]
    out = label(frame, candidate("short"), costs=LabelCosts(commission_bps_per_side=10))
    assert out["exit_reference"] == 98
    assert out["gross_return_bps"] == pytest.approx(200)
    assert out["net_return_bps"] == pytest.approx(180.2)


@pytest.mark.parametrize("direction,opening,high,low,expected", [
    ("long", 95, 96, 94, -500), ("short", 105, 106, 104, -500),
    ("long", 104, 105, 98, 400), ("short", 96, 102, 95, 400)])
def test_gap_uses_actual_open_before_unobservable_intrabar_path(direction, opening, high, low, expected):
    frame = bars()
    frame.iloc[2] = [opening, high, low, opening, 1000]
    out = label(frame, candidate(direction))
    assert out["exit_reference"] == opening
    assert out["gross_return_bps"] == pytest.approx(expected)
    assert out["execution_flags"] == ["gap_exited_at_actual_open"]
    assert not out["ambiguous"]


@pytest.mark.parametrize("direction,stop", [("long", 99), ("short", 101)])
def test_same_bar_both_barriers_is_stop_first_and_ineligible(direction, stop):
    frame = bars()
    frame.iloc[1] = [100, 103, 97, 100, 1000]
    out = label(frame, candidate(direction))
    assert out["status"] == "matured" and out["ambiguous"]
    assert out["exit_reference"] == stop
    assert out["barrier"] == "stop_loss"
    assert out["gross_return_bps"] == pytest.approx(-100)
    assert not out["training_eligible"]
    assert out["execution_flags"] == ["bar_path_ambiguous_stop_first"]


def test_time_barrier_exits_final_complete_bar_close():
    frame = bars()
    frame.iloc[2] = [100, 100.7, 99.5, 100.5, 1000]
    out = label(frame)
    assert out["barrier"] == "time" and out["bars_held"] == 2
    assert out["net_return_bps"] == pytest.approx(50)
    assert out["label_end_time"] == "2020-01-04T00:00:00+00:00"


def test_unclosed_future_bar_cannot_leak_a_hit_or_bad_price():
    baseline = label(cutoff="2020-01-02T12:00Z")
    frame = bars()
    frame.iloc[1] = [100, 9999, -1, 100, 1000]
    assert label(frame, cutoff="2020-01-02T12:00Z") == baseline
    assert baseline["reason"] == "not_matured" and baseline["net_return_bps"] is None


@pytest.mark.parametrize("missing", [1, 2])
def test_missing_entry_or_interior_bar_censors_without_jumping(missing):
    out = label(bars().drop(bars().index[missing]))
    assert out["status"] == "insufficient" and out["reason"] == "missing_bar"
    assert out["label_end_time"] is None and out["net_return_bps"] is None


def test_incomplete_file_tail_is_insufficient_even_with_later_as_of():
    out = label(bars(2), config=BarrierConfig(max_holding_bars=3))
    assert out["reason"] == "tail_insufficient" and out["net_return_bps"] is None


def test_late_signal_availability_starts_at_next_executable_grid_open():
    event = candidate(context={"available_at": "2020-01-02T06:00Z", "timeframe": "1d"})
    frame = bars()
    frame.iloc[1] = [120, 130, 110, 120, 1000]
    out = label(frame, event)
    assert out["entry_time"] == "2020-01-03T00:00:00+00:00"
    assert out["entry_reference"] == 100


def test_delayed_bar_availability_is_respected_and_invalid_availability_censors():
    frame = bars()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.iloc[1, frame.columns.get_loc("available_at")] = pd.Timestamp("2020-01-04", tz="UTC")
    assert label(frame, cutoff="2020-01-03")["reason"] == "not_matured"
    frame.iloc[1, frame.columns.get_loc("available_at")] = pd.Timestamp("2020-01-02", tz="UTC")
    assert label(frame)["reason"] == "availability_before_bar_close"


@pytest.mark.parametrize("late_bar", [1, 2])
def test_delayed_entry_and_intermediate_bars_bound_label_availability(late_bar):
    frame = bars()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.iloc[late_bar, frame.columns.get_loc("available_at")] = pd.Timestamp("2020-01-07", tz="UTC")
    config = BarrierConfig(max_holding_bars=3)
    before = label(frame, config=config, cutoff="2020-01-06")
    assert before["status"] == "insufficient" and before["reason"] == "not_matured"
    assert before["available_at"] is None and not before["training_eligible"]
    mature = label(frame, config=config)
    assert mature["label_end_time"] == "2020-01-05T00:00:00+00:00"
    assert mature["available_at"] == "2020-01-07T00:00:00+00:00"
    assert mature["training_eligible"]


def test_unknown_explicit_timestamp_does_not_become_a_nominal_known_timestamp():
    frame = bars()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.iloc[1, frame.columns.get_loc("available_at")] = pd.NaT
    out = label(frame)
    assert out["reason"] == "invalid_available_at" and out["status"] == "insufficient"
    assert out["available_at"] is None and not out["training_eligible"]
    # Legacy frames with no availability evidence retain their explicit lower-
    # bound research policy; this change does not certify historical publication.
    assert label()["availability_policy"] == "explicit_bar_available_at_or_nominal_close_lower_bound"


@pytest.mark.parametrize("late_bar", [1, 2])
def test_fine_refinement_retains_late_coarse_dependencies(late_bar):
    from analysis.paper_label_execution import refine_barrier_labels

    frame = bars()
    frame.iloc[3] = [100, 103, 98, 100, 1000]
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.iloc[late_bar, frame.columns.get_loc("available_at")] = pd.Timestamp("2020-01-07", tz="UTC")
    hourly = pd.DataFrame({"open": 100., "high": 100., "low": 100.,
                          "close": 100., "volume": 40.},
                         index=pd.date_range("2020-01-04", periods=24, freq="h", tz="UTC"))
    hourly.iloc[0, hourly.columns.get_loc("high")] = 103.
    hourly.iloc[12, hourly.columns.get_loc("low")] = 98.
    out = refine_barrier_labels({"BTC/USDT": frame}, [candidate()],
        config=BarrierConfig(max_holding_bars=3), costs=LabelCosts(),
        as_of="2020-01-08", fine_frames={"BTC/USDT": hourly})["outcomes"][0]
    assert out["refinement_status"] == "resolved" and out["training_eligible"]
    assert out["barrier"] == "profit_take" and out["exit_reference"] == 102
    assert out["label_end_time"] == "2020-01-04T01:00:00+00:00"
    assert out["available_at"] == "2020-01-07T00:00:00+00:00"


def test_duplicate_or_unordered_bars_fail_instead_of_being_repaired():
    frame = bars()
    with pytest.raises(ValueError, match="ordered"):
        label(pd.concat([frame, frame.iloc[:1]]))
    with pytest.raises(ValueError, match="ordered"):
        label(frame.iloc[::-1])


def test_batch_accepts_p0_to_dict_and_compatible_symbol_spelling_without_mutating_inputs():
    event, frame = candidate(), bars()
    original = deepcopy(event)
    class P0:
        def to_dict(self):
            return event
    out = label_candidates({"BTC-USDT": frame}, [P0()], config=BarrierConfig(max_holding_bars=1),
                           costs=LabelCosts(), as_of="2020-01-04")
    assert out["summary"] == {"total": 1, "matured": 1, "ambiguous": 0,
                               "training_eligible": 1, "insufficient": 0}
    assert event == original and frame.index.tz is not None


def test_invalid_contracts_and_final_are_rejected():
    with pytest.raises(ValueError, match="nonnegative"):
        LabelCosts(commission_bps_per_side=-1)
    with pytest.raises(ValueError, match="strictly"):
        BarrierConfig(stop_loss_bps=10000)
    with pytest.raises(ValueError, match="adjudication"):
        label(split="final")
    with pytest.raises(ValueError, match="availability"):
        label(event=candidate(context={"available_at": "2020-01-01", "timeframe": "1d"}))
