import numpy as np
import pandas as pd
import pytest

from research.ml_selection.dataset import (
    FEATURE_COLUMNS, build_dataset, chronological_split, feature_snapshot,
    forward_proxy_outcome,
)


def candles(n=100, *, start="2020-01-01", trend=0.2):
    index = pd.date_range(start, periods=n, freq="D", tz="UTC")
    close = 100 + np.arange(n) * trend
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                         "close": close, "volume": np.full(n, 10000.0)}, index=index)


def row_for(dataset, symbol, candle):
    return dataset.loc[(dataset.symbol == symbol) & (dataset.bar_time == candle)].iloc[0]


def test_keeps_all_candidates_and_marks_observed_membership_honestly():
    frames = {"A": candles(85), "B": candles(72, start="2020-01-05")}
    dataset = build_dataset(frames, horizon_bars=3)
    assert len(dataset) == 157
    assert dataset.membership_basis.eq("observed_history_only").all()
    assert dataset.label_basis.eq("independent_shadow_proxy_not_portfolio").all()
    assert len(FEATURE_COLUMNS) == 21
    first = row_for(dataset, "A", frames["A"].index[0])
    assert not first.eligible and first.exclusion_reason == "insufficient_history"
    mature = row_for(dataset, "A", frames["A"].index[60])
    assert mature.eligible
    assert mature.as_of == mature.bar_time + pd.Timedelta(days=1)


def test_future_mutation_cannot_change_earlier_features_or_eligibility():
    frame = candles(120)
    btc = candles(120, trend=0.4)
    first = build_dataset({"A": frame, "BTC/USDT": btc}, horizon_bars=3)
    altered, changed_btc = frame.copy(), btc.copy()
    for changed in (altered, changed_btc):
        changed.loc[changed.index[80]:, ["open", "high", "low", "close", "volume"]] *= 10
    second = build_dataset({"A": altered, "BTC/USDT": changed_btc}, horizon_bars=3)
    before = first.as_of <= frame.index[80]
    columns = ["as_of", "symbol", "eligible", "exclusion_reason", *FEATURE_COLUMNS]
    pd.testing.assert_frame_equal(first.loc[before, columns], second.loc[before, columns])


def test_snapshot_is_shared_causal_feature_implementation():
    frame, btc = candles(), candles(trend=0.3)
    dataset = build_dataset({"A": frame, "BTC/USDT": btc}, horizon_bars=3)
    for position in (60, 75, 90):
        expected = row_for(dataset, "A", frame.index[position])
        snapshot = feature_snapshot(frame, as_of=expected.as_of, benchmark=btc)
        assert snapshot["bar_time"] == expected.bar_time
        assert snapshot["history_available"]
        for column in FEATURE_COLUMNS:
            assert snapshot[column] == pytest.approx(expected[column])
    # The still-open current candle and all later candles are invisible.
    snapshot = feature_snapshot(frame, as_of=frame.index[75] + pd.Timedelta(hours=12), benchmark=btc)
    assert snapshot["bar_time"] == frame.index[74]


def test_explicit_late_availability_fails_closed_without_old_bar_fallback():
    frame = candles()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.loc[frame.index[65], "available_at"] += pd.Timedelta(days=2)
    dataset = build_dataset({"A": frame}, horizon_bars=3)
    delayed = row_for(dataset, "A", frame.index[65])
    assert not delayed.eligible
    assert delayed.exclusion_reason == "history_unavailable"
    assert delayed[list(FEATURE_COLUMNS)].isna().all()
    snap = feature_snapshot(frame, as_of=delayed.as_of)
    assert snap["bar_time"] == delayed.bar_time
    assert not snap["history_available"]
    assert np.isnan(snap["return_20d"])
    recovered = row_for(dataset, "A", frame.index[67])
    assert recovered.eligible


def test_nanosecond_after_decision_is_not_rounded_into_available_history():
    frame = candles()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.loc[frame.index[65], "available_at"] += pd.Timedelta(nanoseconds=1)
    dataset = build_dataset({"A": frame}, horizon_bars=3)
    point = row_for(dataset, "A", frame.index[65])
    assert not point.eligible and not point.history_available


def test_missing_daily_history_excluded_until_full_contiguous_window_recovers():
    frame = candles(150).drop(candles(150).index[64])
    dataset = build_dataset({"A": frame}, horizon_bars=3)
    gap = row_for(dataset, "A", pd.Timestamp("2020-03-06", tz="UTC"))
    assert gap.exclusion_reason == "missing_daily_history"
    assert not gap.eligible
    assert dataset.iloc[-1].eligible


def test_next_open_horizon_label_costs_and_maturity():
    frame = candles(90)
    dataset = build_dataset({"A": frame}, horizon_bars=3, commission_rate=0.002,
                            slippage_bps=10, stop_atr_multiple=10)
    point = row_for(dataset, "A", frame.index[65])
    entry = frame.open.iloc[66] * 1.001
    exit_fill = frame.close.iloc[68] * 0.999
    expected = exit_fill * 0.998 / (entry * 1.002) - 1
    assert point.label_net_return == pytest.approx(expected)
    assert point.label_available_at == frame.index[68] + pd.Timedelta(days=1)
    assert point.label_exit_reason == "horizon_close"
    assert point.label_mae <= 0


def test_intrabar_stop_and_gap_stop_use_conservative_fill():
    frame = candles(90, trend=0)
    frame.loc[frame.index[67], ["open", "high", "low", "close"]] = [90, 92, 88, 91]
    dataset = build_dataset({"A": frame}, horizon_bars=5, commission_rate=0,
                            slippage_bps=0, stop_atr_multiple=2)
    gap = row_for(dataset, "A", frame.index[65])
    assert gap.label_net_return == pytest.approx(-0.1)
    assert gap.label_mae == pytest.approx(-0.1)
    assert gap.label_exit_reason == "initial_atr_stop"
    assert gap.label_available_at == frame.index[67] + pd.Timedelta(days=1)
    intrabar = candles(90, trend=0)
    intrabar.loc[intrabar.index[66], ["high", "low", "close"]] = [102, 94, 99]
    result = build_dataset({"A": intrabar}, horizon_bars=5, commission_rate=0,
                           slippage_bps=0, stop_atr_multiple=2)
    stopped = row_for(result, "A", intrabar.index[65])
    assert stopped.label_net_return == pytest.approx(-0.04)
    assert stopped.label_mae == pytest.approx(-0.04)


def test_missing_future_is_unavailable_not_flat_or_synthetic_loss():
    frame = candles(90, trend=0).drop(candles(90).index[67])
    dataset = build_dataset({"A": frame}, horizon_bars=5)
    point = row_for(dataset, "A", pd.Timestamp("2020-03-06", tz="UTC"))
    assert np.isnan(point.label_net_return)
    assert pd.isna(point.label_available_at)
    assert np.isnan(dataset.iloc[-1].label_net_return)
    assert dataset.iloc[-1].label_exit_reason == "future_unavailable"


def test_stop_before_truncated_tail_is_still_matured():
    frame = candles(68, trend=0)
    frame.loc[frame.index[66], ["high", "low", "close"]] = [102, 94, 99]
    dataset = build_dataset({"A": frame}, horizon_bars=20, commission_rate=0,
                            slippage_bps=0, stop_atr_multiple=2)
    assert row_for(dataset, "A", frame.index[65]).label_net_return == pytest.approx(-0.04)


def test_delayed_future_candle_controls_label_maturity():
    frame = candles()
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.loc[frame.index[66], "available_at"] = frame.index[75]
    dataset = build_dataset({"A": frame}, horizon_bars=3)
    assert row_for(dataset, "A", frame.index[65]).label_available_at == frame.index[75]


def test_lifecycle_blocks_entry_and_forces_shadow_close():
    frame = candles(90)
    frame["entry_blocked"] = False
    frame["scheduled_exit"] = False
    frame.loc[frame.index[68]:, "entry_blocked"] = True
    frame.loc[frame.index[70], "scheduled_exit"] = True
    dataset = build_dataset({"A": frame}, horizon_bars=20, commission_rate=0,
                            slippage_bps=0, stop_atr_multiple=10)
    early = row_for(dataset, "A", frame.index[65])
    assert early.label_exit_reason == "scheduled_exit"
    assert early.label_net_return == pytest.approx(frame.close.iloc[70] / frame.open.iloc[66] - 1)
    assert early.label_available_at == frame.index[70] + pd.Timedelta(days=1)
    blocked = row_for(dataset, "A", frame.index[68])
    assert not blocked.eligible and blocked.exclusion_reason == "lifecycle_entry_blocked"
    assert np.isnan(blocked.label_net_return)


def test_pit_membership_requires_known_listing_and_delisting_evidence():
    frame = candles(100)
    facts = {"A": {"listed_at": frame.index[0], "available_at": frame.index[63],
                   "delisted_at": frame.index[80], "delisting_available_at": frame.index[77]}}
    dataset = build_dataset({"A": frame}, horizon_bars=3, membership=facts)
    assert not row_for(dataset, "A", frame.index[61]).eligible
    assert row_for(dataset, "A", frame.index[63]).eligible
    assert not row_for(dataset, "A", frame.index[79]).eligible
    assert dataset.membership_basis.eq("supplied_point_in_time_facts").all()
    facts["A"].pop("delisting_available_at")
    with pytest.raises(ValueError, match="delisting_available_at"):
        build_dataset({"A": frame}, membership=facts)


def test_chronological_split_purges_boundary_labels_and_keeps_dates_together():
    frame = candles(140)
    dataset = build_dataset({"A": frame, "B": frame.copy()}, horizon_bars=3)
    train_end, validation_end, test_end = frame.index[90], frame.index[110], frame.index[135]
    parts = chronological_split(dataset, train_end=train_end,
                                validation_end=validation_end, test_end=test_end)
    for name, upper in (("train", train_end), ("validation", validation_end), ("test", test_end)):
        part = parts[name]
        assert len(part)
        assert (part.label_available_at < upper).all()
        assert part.groupby("as_of").size().eq(2).all()
        assert part.eligible.all()
    assert set(parts["train"].as_of).isdisjoint(set(parts["validation"].as_of))
    boundary = dataset.loc[dataset.label_available_at == train_end]
    assert len(boundary)
    assert set(boundary.as_of).isdisjoint(set(parts["train"].as_of))
    with pytest.raises(ValueError, match="increasing"):
        chronological_split(dataset, train_end=validation_end, validation_end=train_end)


def test_boundary_purge_withholds_early_loser_when_winning_peer_unmatured():
    loser = candles(110, trend=0)
    winner = candles(110, trend=.2)
    loser.loc[loser.index[80], ["open", "high", "low", "close"]] = [100, 101, 94, 99]
    dataset = build_dataset({"LOSS": loser, "WIN": winner}, horizon_bars=5,
                            commission_rate=0, slippage_bps=0)
    point = loser.index[79] + pd.Timedelta(days=1)
    cohort = dataset.loc[dataset.as_of == point]
    boundary = loser.index[82]
    loss = cohort.loc[cohort.symbol == "LOSS"].iloc[0]
    win = cohort.loc[cohort.symbol == "WIN"].iloc[0]
    assert loss.eligible and win.eligible
    assert loss.label_net_return < 0 and loss.label_available_at < boundary
    assert win.label_net_return > 0 and win.label_available_at >= boundary
    parts = chronological_split(dataset, train_end=boundary,
                                validation_end=loser.index[96], test_end=loser.index[-1])
    assert point not in set(parts["train"].as_of)
    assert parts["train"].groupby("as_of").size().eq(2).all()


@pytest.mark.parametrize("damage", ["missing_label", "nonfinite_label", "nonfinite_feature"])
def test_unavailable_member_withholds_whole_eligible_cohort(damage):
    frame = candles(110)
    dataset = build_dataset({"A": frame, "B": frame.copy()}, horizon_bars=3)
    point = frame.index[70] + pd.Timedelta(days=1)
    target = (dataset.as_of == point) & (dataset.symbol == "B")
    if damage == "missing_label":
        dataset.loc[target, "label_available_at"] = pd.NaT
        dataset.loc[target, "label_net_return"] = np.nan
    elif damage == "nonfinite_label":
        dataset.loc[target, "label_net_return"] = np.inf
    else:
        dataset.loc[target, FEATURE_COLUMNS[0]] = np.nan
    parts = chronological_split(dataset, train_end=frame.index[85], validation_end=frame.index[96])
    assert point not in set(parts["train"].as_of)
    assert parts["train"].groupby("as_of").size().eq(2).all()


@pytest.mark.parametrize("parameter", [
    {"horizon_bars": 0}, {"horizon_bars": 1.5}, {"min_history": 60},
    {"commission_rate": 1}, {"slippage_bps": 10000}, {"stop_atr_multiple": 0},
    {"min_quote_volume": -1}, {"commission_rate": float("nan")},
])
def test_invalid_parameters_fail_closed(parameter):
    with pytest.raises(ValueError):
        build_dataset({"A": candles()}, **parameter)


def test_duplicate_timestamps_and_unknown_availability_fail_closed():
    frame = candles()
    with pytest.raises(ValueError, match="unique"):
        build_dataset({"A": pd.concat((frame, frame.iloc[:1]))})
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.loc[frame.index[65], "available_at"] = pd.NaT
    dataset = build_dataset({"A": frame}, horizon_bars=3)
    assert not row_for(dataset, "A", frame.index[65]).eligible
    assert np.isnan(row_for(dataset, "A", frame.index[64]).label_net_return)


def test_liquidity_threshold_excludes_without_deleting_candidate():
    frame = candles()
    dataset = build_dataset({"A": frame}, min_quote_volume=10_000_000)
    assert len(dataset) == len(frame)
    point = row_for(dataset, "A", frame.index[65])
    assert not point.eligible and point.exclusion_reason == "insufficient_liquidity"


def forward_outcome(frame, *, observed="2020-01-03T12:00:00Z", cutoff="2020-01-10",
                    horizon=3, atr=2., commission=.001, slippage=5., stop_multiple=2.):
    return forward_proxy_outcome(frame, entry_not_before=observed,
        decision_atr_absolute=atr, horizon_bars=horizon, commission_rate=commission,
        slippage_bps=slippage, stop_atr_multiple=stop_multiple, mature_as_of=cutoff)


def test_forward_proxy_midday_observation_enters_only_next_day_and_costs_once():
    frame = candles(10)
    # The already-past Jan 3 open must not enter the trade despite being present.
    frame.loc[frame.index[2], ["open", "high", "low", "close"]] = [50, 51, 49, 50]
    result = forward_outcome(frame, atr=20.)
    assert result["status"] == "resolved"
    assert result["entry_time"] == pd.Timestamp("2020-01-04", tz="UTC")
    assert result["exit_time"] == pd.Timestamp("2020-01-07", tz="UTC")
    expected = frame.close.iloc[5] * .9995 * .999 / (frame.open.iloc[3] * 1.0005 * 1.001) - 1
    assert result["label_net_return"] == pytest.approx(expected)
    assert not result["label_stop_hit"]
    assert result["label_basis"] == "independent_shadow_proxy_not_portfolio"


def test_forward_proxy_exact_open_and_one_nanosecond_after_have_different_entry():
    frame = candles(10)
    exact = forward_outcome(frame, observed="2020-01-03T00:00:00Z")
    later = forward_outcome(frame, observed="2020-01-03T00:00:00.000000001Z")
    assert exact["entry_time"] == pd.Timestamp("2020-01-03", tz="UTC")
    assert later["entry_time"] == pd.Timestamp("2020-01-04", tz="UTC")


def test_forward_proxy_gap_stop_fills_worse_open_using_frozen_atr():
    frame = candles(10, trend=0)
    frame.loc[frame.index[4], ["open", "high", "low", "close"]] = [90, 92, 88, 91]
    result = forward_outcome(frame, atr=2., commission=.002, slippage=10.)
    assert result["status"] == "resolved" and result["label_stop_hit"]
    assert result["exit_time"] == frame.index[4]
    assert result["label_available_at"] == frame.index[4] + pd.Timedelta(days=1)
    assert result["label_net_return"] == pytest.approx(90 * .999 * .998 / (100 * 1.001 * 1.002) - 1)
    assert result["label_mae"] == pytest.approx(90 / (100 * 1.001) - 1)
    # ATR is frozen input; no recomputation from the unusually wide future bar.
    wider_stop = forward_outcome(frame, atr=20., commission=0, slippage=0)
    assert not wider_stop["label_stop_hit"]


def test_forward_proxy_intrabar_stop_truncates_excursion_at_fill():
    frame = candles(10, trend=0)
    frame.loc[frame.index[3], ["high", "low", "close"]] = [102, 93, 99]
    result = forward_outcome(frame, commission=0, slippage=0)
    assert result["status"] == "resolved" and result["label_stop_hit"]
    assert result["label_net_return"] == pytest.approx(-.04)
    assert result["label_mae"] == pytest.approx(-.04)
    assert result["exit_time"] == frame.index[3] + pd.Timedelta(days=1)


def test_forward_proxy_waits_for_explicit_availability_not_nominal_horizon():
    frame = candles(10)
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame.loc[frame.index[4], "available_at"] = pd.Timestamp("2020-01-09", tz="UTC")
    pending = forward_outcome(frame, cutoff="2020-01-08")
    assert pending["status"] == "pending"
    assert pending["pending_reason"] == "execution_bar_not_mature"
    assert pending["label_net_return"] is None and pending["label_available_at"] is None
    assert pending["exit_time"] is None and pending["label_stop_hit"] is None
    mature = forward_outcome(frame, cutoff="2020-01-09")
    assert mature["status"] == "resolved"
    assert mature["label_available_at"] == pd.Timestamp("2020-01-09", tz="UTC")


@pytest.mark.parametrize("removed,reason", [(3, "entry_bar_unavailable"), (4, "missing_execution_history"),
                                           (5, "missing_execution_history")])
def test_forward_proxy_missing_continuity_never_skips_into_later_entry_or_exit(removed, reason):
    frame = candles(10)
    result = forward_outcome(frame.drop(frame.index[removed]))
    assert result["status"] == "pending" and result["pending_reason"] == reason
    assert result["entry_time"] == pd.Timestamp("2020-01-04", tz="UTC")
    assert result["label_net_return"] is None


def test_forward_proxy_stop_matures_without_remaining_horizon_and_future_prices_irrelevant():
    frame = candles(10, trend=0)
    frame.loc[frame.index[3], ["high", "low", "close"]] = [102, 93, 99]
    first = forward_outcome(frame.iloc[:4], cutoff="2020-01-05", horizon=20)
    frame.loc[frame.index[4]:, ["open", "high", "low", "close"]] *= 100
    second = forward_outcome(frame, cutoff="2020-01-05", horizon=20)
    assert first["status"] == "resolved" and first == second


@pytest.mark.parametrize("damage", ["bad_price", "zero_volume", "unknown_availability"])
def test_forward_proxy_invalid_or_untradable_execution_bar_is_pending(damage):
    frame = candles(10)
    if damage == "bad_price":
        frame.loc[frame.index[4], "close"] = np.nan
    elif damage == "zero_volume":
        frame.loc[frame.index[4], "volume"] = 0
    else:
        frame["available_at"] = frame.index + pd.Timedelta(days=1)
        frame.loc[frame.index[4], "available_at"] = pd.NaT
    result = forward_outcome(frame)
    assert result["status"] == "pending"
    assert result["label_net_return"] is None and result["label_available_at"] is None


def test_forward_proxy_before_entry_and_before_closed_execution_remain_pending():
    frame = candles(10)
    early = forward_outcome(frame, cutoff="2020-01-03T18:00:00Z")
    assert early["status"] == "pending" and early["pending_reason"] == "entry_not_reached"
    open_bar = forward_outcome(frame, cutoff="2020-01-04T12:00:00Z")
    assert open_bar["status"] == "pending" and open_bar["pending_reason"] == "execution_bar_not_mature"


@pytest.mark.parametrize("parameters", [
    {"decision_atr_absolute": -1}, {"decision_atr_absolute": np.nan},
    {"horizon_bars": 0}, {"commission_rate": 1}, {"slippage_bps": 10000},
    {"stop_atr_multiple": 0},
])
def test_forward_proxy_invalid_frozen_parameters_fail_closed(parameters):
    values = {"entry_not_before": "2020-01-03T12:00:00Z", "decision_atr_absolute": 2.,
              "horizon_bars": 3, "mature_as_of": "2020-01-10"}
    values.update(parameters)
    with pytest.raises(ValueError):
        forward_proxy_outcome(candles(10), **values)
