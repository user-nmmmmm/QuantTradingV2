"""Decision availability must precede every simulated execution, including gaps."""
from copy import deepcopy

import pandas as pd
import pytest

from backtest.delayed_execution import replay_delayed_decisions
from core.broker import Broker
from core.portfolio import Portfolio


def bars():
    return pd.DataFrame({"open": [100., 200., 300., 400.], "high": [101., 201., 301., 401.],
        "low": [99., 199., 299., 399.], "close": [100., 200., 300., 400.], "volume": 1000.},
        index=pd.date_range("2020-01-02", periods=4, freq="h", tz="UTC"))


def decision(**changes):
    return {"decision_id": "buy1", "symbol": "X", "side": "buy", "quantity": 1.,
        "reference_price": 100., "signal_time": "2020-01-01T00:00:00Z", "signal_timeframe": "1d",
        "available_at": "2020-01-02T00:30:00Z", **changes}


def replay(frame=None, decisions=None, **kwargs):
    return replay_delayed_decisions({"X": bars() if frame is None else frame},
        [decision()] if decisions is None else decisions, commission_rate=0, slippage=0, spread_bps=0, **kwargs)


@pytest.mark.parametrize("available,fill_hour,price", [("00:00", 0, 100.), ("00:00:01", 1, 200.),
    ("00:30", 1, 200.), ("01:00", 1, 200.)])
def test_fill_uses_first_actual_open_at_or_after_availability(available, fill_hour, price):
    result = replay(decisions=[decision(available_at=f"2020-01-02T{available}Z")])
    assert len(result["fills"]) == 1
    fill = result["fills"][0]
    assert fill["fill_time"].hour == fill_hour
    assert fill["fill_price"] == price
    assert result["no_fill_before_decision"]
    assert result["live_orders_submitted"] == 0


def test_gap_waits_for_next_real_bar_and_does_not_invent_an_open():
    result = replay(frame=bars().drop(bars().index[1]))
    assert result["fills"][0]["fill_time"].hour == 2
    assert result["fills"][0]["fill_price"] == 300


def test_unavailable_decision_remains_unexecuted_and_future_bars_do_not_change_prior_fill():
    result = replay(decisions=[decision(), decision(decision_id="late", available_at="2020-01-03T00:00:00Z")])
    assert result["unexecuted_decisions"] == ["late"]
    frame = bars()
    frame.loc[frame.index[-1], ["open", "high", "low", "close"]] = [9., 10., 8., 9.]
    changed = replay(frame)
    assert result["fills"] == changed["fills"]


def test_ioc_partial_fill_and_fees_conserve_cash_on_round_trip():
    frame = bars()
    frame["volume"] = 10.
    result = replay_delayed_decisions({"X": frame}, [decision(quantity=2),
        decision(decision_id="sell1", side="sell", available_at="2020-01-02T01:30:00Z", reference_price=200)],
        max_participation_rate=.1, commission_rate=.001, slippage=0, spread_bps=0)
    assert [fill["qty"] for fill in result["fills"]] == [1., 1.]
    assert result["cash"] == pytest.approx(10000-200-.2+300-.3)
    assert result["final_equity"] == result["cash"]
    assert result["positions"].get("X", 0) == 0


@pytest.mark.parametrize("change", [{"available_at": "2020-01-01T12:00:00Z"},
    {"order_type": "limit"}, {"side": "short"}, {"quantity": float("nan")}, {"reference_price": 0}])
def test_invalid_or_unsupported_decisions_fail_before_execution(change):
    with pytest.raises(ValueError):
        replay(decisions=[decision(**change)])


def test_duplicate_decisions_fail_and_inputs_remain_unchanged():
    decisions = [decision()]
    original = deepcopy(decisions)
    replay(decisions=decisions)
    assert decisions == original
    with pytest.raises(ValueError, match="unique"):
        replay(decisions=decisions*2)


def test_default_broker_retains_next_bar_rule_and_event_clock_is_explicit():
    at = pd.Timestamp("2020-01-02")
    bar = bars().iloc[0].copy()
    bar.name = at
    ordinary = Broker(Portfolio(10000, account_mode="spot"))
    ordinary.submit_order("X", "buy", 1, price=100, timestamp=at)
    ordinary.process_orders({"X": bar})
    assert not ordinary.trades
    bar.name = at+pd.Timedelta(hours=1)
    ordinary.process_orders({"X": bar})
    assert len(ordinary.trades) == 1
    for kwargs in ({"timestamp": None, "match_not_before": at},
                   {"timestamp": at, "match_not_before": at-pd.Timedelta(seconds=1)},
                   {"timestamp": at, "match_not_before": at, "order_type": "stop"}):
        with pytest.raises(ValueError):
            ordinary.submit_order("X", "buy", 1, price=100, **kwargs)


@pytest.mark.parametrize("earliest", ["2020-01-02T00:00:00", "2020-01-02T08:00:00+08:00",
    pd.Timestamp("2020-01-02", tz="UTC")])
def test_opt_in_matching_normalizes_aware_naive_and_text_clocks(earliest):
    broker = Broker(Portfolio(10000, account_mode="spot"))
    order = broker.submit_order("X", "buy", 1, price=100, timestamp=pd.Timestamp("2020-01-02"),
        match_not_before=earliest)
    assert order.match_not_before == pd.Timestamp("2020-01-02", tz="UTC")
    bar = bars().iloc[0].copy()
    bar.name = pd.Timestamp("2020-01-02")
    broker.process_orders({"X": bar})
    assert len(broker.trades) == 1
