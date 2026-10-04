import pandas as pd

from analysis.paper_labels import BarrierConfig
from scripts.review_barrier_first_touch import review
from tests.test_paper_label_execution import inputs


def test_newly_downloaded_fine_data_resolves_diagnostics_without_backdating_training():
    daily, hourly, candidate = inputs()
    hourly["available_at"] = "2026-10-03T08:00:00+00:00"
    labels, changes, summary = review({"BTC/USDT": daily}, [candidate], {"BTC/USDT": hourly},
        fine_timeframe="1h", observed_at="2026-10-03T09:00:00+00:00", config=BarrierConfig(max_holding_bars=1))
    assert summary["before"]["ambiguous"] == 1
    assert summary["after"]["ambiguous"] == 0
    assert summary["barrier_changed"] == 1
    assert summary["newly_resolved_available_in_original_history"] == 0
    assert not changes[0]["usable_at_historical_end"]
    assert pd.Timestamp(labels["outcomes"][0]["available_at"]).year == 2026
    assert not summary["historical_point_in_time_complete"]


def test_same_fine_bar_crossing_both_barriers_remains_unresolved():
    daily, hourly, candidate = inputs()
    hourly.loc[hourly.index[0], "low"] = 98.
    hourly["available_at"] = "2026-10-03T08:00:00+00:00"
    _, changes, summary = review({"BTC/USDT": daily}, [candidate], {"BTC/USDT": hourly},
        fine_timeframe="1h", observed_at="2026-10-03T09:00:00+00:00", config=BarrierConfig(max_holding_bars=1))
    assert summary["after"]["ambiguous"] == 1
    assert changes[0]["status"] == "fine_path_still_ambiguous"


def test_shortened_exchange_bar_cannot_establish_a_complete_path():
    daily, hourly, candidate = inputs()
    hourly["available_at"] = "2026-10-03T08:00:00+00:00"
    hourly["is_complete_bar"] = True
    hourly.loc[hourly.index[0], "is_complete_bar"] = False
    _, changes, summary = review({"BTC/USDT": daily}, [candidate], {"BTC/USDT": hourly},
        fine_timeframe="1h", observed_at="2026-10-03T09:00:00+00:00", config=BarrierConfig(max_holding_bars=1))
    assert summary["after"]["ambiguous"] == 1
    assert changes[0]["status"] == "fine_interval_incomplete"


def test_cascade_keeps_already_resolved_rows_when_next_feed_is_empty():
    from analysis.paper_labels import LabelCosts
    from scripts.review_barrier_cascade import cascade
    daily, hourly, candidate = inputs()
    now = "2026-10-03T09:00:00+00:00"
    hourly["available_at"] = now
    labels, _, _ = review({"BTC/USDT": daily}, [candidate], {"BTC/USDT": hourly},
        fine_timeframe="1h", observed_at=now, config=BarrierConfig(max_holding_bars=1))
    result, stage = cascade(labels, {"BTC/USDT": daily}, [candidate], {},
        fine_timeframe="1m", as_of=now, config=BarrierConfig(max_holding_bars=1), costs=LabelCosts())
    assert result["outcomes"] == labels["outcomes"]
    assert stage["newly_resolved"] == 0
    assert result["summary"]["resolved_with_fine_data"] == 1
