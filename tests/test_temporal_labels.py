"""Strict labels preserve availability, source identity and old training prefixes."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from core.runtime import MarketDataSlice
from core.signal_observation import SignalObserver
from core.signal_observation_types import ObservationPolicy, fingerprint, iso
from core.signal_outcomes import ForwardOutcomeTracker, ObservationCosts
from core.state import MarketState
from core.temporal_data import TemporalOHLCV
from core.temporal_labels import LABEL_PROTOCOL, TemporalLabelError, VersionedOutcomeTracker
from tests.test_signal_observation import candidate, prices, DailyRaw


def documented(periods=7):
    frame = prices(periods)
    frame["available_at"] = (frame.index+pd.Timedelta(days=1)).tz_localize("UTC")
    frame["observed_at"] = frame["available_at"]
    frame["revision_id"] = "original"
    frame.attrs["availability_evidence"] = {"kind": "source_publication", "reference": "fixture:archive"}
    return frame


def reader(frame=None, knowledge="local"):
    data = TemporalOHLCV("fixture:X", timeframe="1d", policy={"mode": "strict", "knowledge": knowledge})
    data.ingest(documented() if frame is None else frame)
    return data


def event(data, cutoff, *, realised=99999.):
    frame = data.as_of(cutoff)
    stamp = pd.Timestamp(cutoff)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    stamp = (stamp-pd.Timedelta(days=1)).tz_localize(None)
    bar = pd.Series({"open": realised, "high": realised+1, "low": realised-1,
                     "close": realised, "volume": 1e12}, name=stamp)
    return MarketDataSlice(stamp, {"X": bar}, {"X": frame}, source="historical_strict", timeframe="1d")


def tracker(data, *, horizons=(1, 3), costs=None):
    result = VersionedOutcomeTracker(ObservationPolicy(enabled=True, horizons=horizons), costs or ObservationCosts())
    result.advance(event(data, "2020-01-02"))
    result.add(candidate())
    return result


def revision(data, index=1, available="2020-01-08", **changes):
    frame = documented().iloc[[index]].copy()
    frame["revision_id"] = f"revision:{available}"
    frame["available_at"] = frame["observed_at"] = pd.Timestamp(available, tz="UTC")
    for column, value in changes.items():
        frame[column] = value
    data.ingest(frame)


def test_source_provenance_is_detached_complete_and_matches_exact_raw_row():
    data = reader()
    first = data.as_of("2020-01-03")
    reference = first.attrs["temporal_source_versions"][iso("2020-01-02")]
    assert reference["record_sha256"] == fingerprint(reference["record"])
    assert reference["record"]["data"]["open"] == first.loc["2020-01-02", "open"]
    reference["record"]["data"]["open"] = -1
    second = data.as_of("2020-01-03")
    assert second.attrs["temporal_source_versions"][iso("2020-01-02")]["record"]["data"]["open"] == 102.


def test_full_window_replay_matches_existing_cost_calculator_and_ignores_realised_bars():
    data = reader()
    costs = ObservationCosts(commission_rate=.001, slippage=.0005, spread_bps=2,
        volatility_slippage_factor=.02, use_impact_cost=True, impact_coefficient=.1)
    result = tracker(data, costs=costs)
    result.advance(event(data, "2020-01-03"))
    result.advance(event(data, "2020-01-05"))
    old = ForwardOutcomeTracker(result.policy, costs)
    old.add(candidate())
    for at, row in documented().iloc[1:4].iterrows():
        old.advance(SimpleNamespace(timestamp=at, bars={"X": row}))
    for actual, expected in zip(result.results, old.results):
        for field in ("entry_reference", "exit_reference", "quantity", "net_pnl", "commission",
                      "slippage", "impact", "carry", "mae_bps", "mfe_bps"):
            assert actual[field] == pytest.approx(expected[field])
        assert actual["training_eligible"] is True
        assert actual["available_at"] == expected["available_at"]
        assert actual["revision_id"] == "label_"+actual["content_sha256"]
        payload = {key: value for key, value in actual.items() if key not in {"revision_id", "content_sha256"}}
        assert fingerprint(payload) == actual["content_sha256"]
        assert len(actual["source_versions"]) == actual["horizon_bars"]


@pytest.mark.parametrize("late_index", [1, 2])
def test_late_entry_or_intermediate_bar_waits_and_can_mature_later(late_index):
    frame = documented()
    frame.loc[frame.index[late_index], ["available_at", "observed_at"]] = pd.Timestamp("2020-01-07", tz="UTC")
    data = reader(frame)
    result = tracker(data, horizons=(3,))
    for day in (3, 4, 5, 6):
        result.advance(event(data, f"2020-01-{day:02d}"))
    assert result.results == [] and result.revisions == []
    result.advance(event(data, "2020-01-07"))
    row = result.results[0]
    assert row["status"] == "matured" and row["training_eligible"]
    assert row["available_at"] == row["observed_at"] == iso("2020-01-07")
    assert row["entry_time"] == iso("2020-01-02")
    assert row["exit_bar"] == iso("2020-01-04")


def test_first_calculation_observation_cannot_be_backdated_to_window_close():
    data = reader()
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-07"))
    assert result.results[0]["window_close"] == iso("2020-01-03")
    assert result.results[0]["available_at"] == iso("2020-01-07")


def test_revision_replays_entry_costs_and_preserves_old_training_prefix():
    data = reader()
    result = tracker(data, horizons=(3,), costs=ObservationCosts(use_impact_cost=True, impact_coefficient=.1))
    result.advance(event(data, "2020-01-05"))
    original = result.revisions
    revision(data, open=101., low=100., volume=100.)
    result.advance(event(data, "2020-01-07"))
    assert result.revisions == original
    result.advance(event(data, "2020-01-08"))
    assert result.revisions[:1] == original
    assert result.results == [result.revisions[-1]]
    assert result.results[0]["entry_reference"] == 101.
    assert result.results[0]["impact"] != original[0]["impact"]
    assert result.results[0]["available_at"] == iso("2020-01-08")
    old_training = [row for row in result.revisions if pd.Timestamp(row["available_at"]) < pd.Timestamp("2020-01-08", tz="UTC")]
    assert old_training == original
    detached = result.revisions
    detached[0]["source_versions"][0]["record"]["data"]["open"] = -5
    assert result.revisions[:1] == original
    result.advance(event(data, "2020-01-09"))
    assert len(result.revisions) == 2  # unchanged inputs do not create daily pseudo-revisions


def test_changed_source_identity_creates_revision_even_when_return_unchanged():
    data = reader()
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-03"))
    first = result.revisions[0]
    revision(data, volume=200000.)
    result.advance(event(data, "2020-01-08"))
    assert len(result.revisions) == 2
    assert result.results[0]["net_pnl"] == first["net_pnl"]
    assert result.results[0]["revision_id"] != first["revision_id"]


def test_invalid_revised_ohlcv_appends_ineligible_revision_instead_of_retaining_old_mature_value():
    data = reader()
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-03"))
    revision(data, high=1.)
    result.advance(event(data, "2020-01-08"))
    assert len(result.revisions) == 2
    assert result.results == [result.revisions[-1]]
    assert result.results[0]["status"] == "censored_invalid_bar"
    assert result.results[0]["training_eligible"] is False
    assert result.results[0]["net_pnl"] is None
    assert result.results[0]["source_versions"]


def test_missing_or_unknown_sources_never_become_zero_returns_and_finish_retains_matured_versions():
    frame = documented()
    frame.loc[frame.index[2], ["available_at", "observed_at"]] = pd.NaT
    frame.loc[frame.index[2], "availability_evidence"] = None
    # Remove only the missing intermediate bar: a missing row is not a zero return.
    data = reader(frame.drop(frame.index[2]))
    result = tracker(data, horizons=(1, 3))
    result.advance(event(data, "2020-01-07"))
    mature = result.results
    result.finish("2099-01-01")
    assert result.results[0] == mature[0]
    assert len(result.results) == 2
    assert result.results[1]["net_pnl"] is None
    assert result.results[1]["status"] == "censored_missing_source_evidence"
    assert result.results[1]["available_at"] == iso("2020-01-07")
    assert result.results == result.revisions


def test_asof_string_without_record_proof_is_not_enough_to_mature_labels():
    data = reader()
    result = tracker(data, horizons=(1,))
    market = event(data, "2020-01-03")
    market.histories["X"].attrs.pop("temporal_source_versions")
    result.advance(market)
    result.finish(None)
    assert result.results[0]["status"] == "censored_missing_source_evidence"
    assert result.results[0]["training_eligible"] is False


@pytest.mark.parametrize("change_hash", [False, True])
def test_source_hash_tampering_and_reused_revision_claims_are_rejected(change_hash):
    data = reader()
    result = tracker(data, horizons=(1,))
    market = event(data, "2020-01-03")
    envelope = market.histories["X"].attrs["temporal_source_versions"][iso("2020-01-01")]
    envelope["record"]["data"]["volume"] = 500.
    market.histories["X"].loc["2020-01-01", "volume"] = 500.
    if change_hash:
        envelope["record_sha256"] = fingerprint(envelope["record"])
    with pytest.raises(TemporalLabelError, match="identity changed" if change_hash else "hash mismatch"):
        result.advance(market)


def test_visible_frame_mutation_and_conflicting_candidate_identity_are_rejected():
    data = reader()
    result = tracker(data)
    with pytest.raises(TemporalLabelError, match="candidate identity changed"):
        result.add(replace(candidate(), reference_price=150.))
    market = event(data, "2020-01-03")
    market.histories["X"].loc["2020-01-02", "close"] = 1.
    with pytest.raises(TemporalLabelError, match="OHLCV differs"):
        result.advance(market)


@pytest.mark.parametrize("knowledge,expected", [("local", "2020-01-07"), ("published", "2020-01-03")])
def test_local_observation_and_published_knowledge_have_different_availability(knowledge, expected):
    frame = documented()
    frame.loc[frame.index[1], "observed_at"] = pd.Timestamp("2020-01-07", tz="UTC")
    data = reader(frame, knowledge)
    result = tracker(data, horizons=(1,))
    for at in ("2020-01-03", "2020-01-07"):
        result.advance(event(data, at))
    assert result.results[0]["available_at"] == iso(expected)


@pytest.mark.parametrize("account", ["spot_margin", "perpetual"])
def test_financing_accounts_without_versioned_cost_evidence_never_train(account):
    data = reader()
    result = tracker(data, horizons=(1,), costs=ObservationCosts(account_mode=account))
    result.advance(event(data, "2020-01-07"))
    result.finish(None)
    assert result.results[0]["status"] == "censored_unversioned_financing_evidence"
    assert not result.results[0]["training_eligible"]
    assert result.results[0]["net_pnl"] is None


def test_delayed_candidate_uses_next_possible_open_but_is_not_v1_training_eligible():
    data = reader()
    result = VersionedOutcomeTracker(ObservationPolicy(horizons=(1,)), ObservationCosts())
    result.advance(event(data, "2020-01-02T00:00:03Z"))
    c = candidate()
    result.add(replace(c, context=replace(c.context, available_at=iso("2020-01-02T00:00:03Z"))))
    result.advance(event(data, "2020-01-04"))
    assert result.results[0]["entry_time"] == iso("2020-01-03")
    assert result.results[0]["status"] == "matured" and not result.results[0]["training_eligible"]


def test_prefix_replay_produces_identical_immutable_facts():
    data = reader()
    revision(data, available="2020-01-06", open=101., low=100.)
    full, prefix = tracker(data, horizons=(1, 3)), tracker(data, horizons=(1, 3))
    for day in range(3, 8):
        full.advance(event(data, f"2020-01-{day:02d}"))
        if day < 6:
            prefix.advance(event(data, f"2020-01-{day:02d}"))
    assert [r for r in full.revisions if pd.Timestamp(r["available_at"]) < pd.Timestamp("2020-01-06", tz="UTC")] == prefix.revisions


def test_observer_initial_marker_strict_routing_and_legacy_payload_shape():
    state = SimpleNamespace(get_states=lambda frame: frame.__setitem__("market_state", MarketState.TREND_UP))
    def observer(mode):
        return SignalObserver(policy=ObservationPolicy(enabled=True, horizons=(1,)), costs=ObservationCosts(),
            strategies={"Test": DailyRaw()}, state_machine=state, temporal_policy=mode)
    strict = observer("strict")
    assert strict.export()["temporal_label_protocol"]["schema"] == LABEL_PROTOCOL
    assert strict.export()["outcome_revisions"] == []
    legacy = observer(None)
    assert "temporal_label_protocol" not in legacy.export()
    assert isinstance(legacy.outcomes, ForwardOutcomeTracker)
    data = reader()
    first = event(data, "2020-01-02")
    strict.advance(first)
    from core.portfolio import Portfolio
    from core.risk import RiskManager
    strict.observe(first, "X", portfolio=Portfolio(10000), risk_manager=RiskManager(),
                   router=SimpleNamespace(regime_map={}), audit={})
    assert not strict.errors and len(strict.candidates) == 1
    strict.advance(event(data, "2020-01-03"))
    strict.finish()
    payload = strict.export()
    assert payload["outcomes"] == payload["outcome_revisions"]
    assert payload["outcomes"][0]["entry_reference"] == 102.
    assert payload["outcomes"][0]["training_eligible"]


def test_producer_payload_passes_training_contract_and_latest_invalid_revision_blocks_old_label():
    from core.signal_meta_layer import _validate_input
    from core.signal_label_versions import OutcomeRevisionBook
    from core.portfolio import Portfolio
    from core.risk import RiskManager

    state = SimpleNamespace(get_states=lambda frame: frame.__setitem__("market_state", MarketState.TREND_UP))
    observer = SignalObserver(policy=ObservationPolicy(enabled=True, horizons=(1, 20)), costs=ObservationCosts(),
        strategies={"Test": DailyRaw()}, state_machine=state, temporal_policy="strict")
    data = reader()
    first = event(data, "2020-01-02")
    observer.advance(first)
    observer.observe(first, "X", portfolio=Portfolio(10000), risk_manager=RiskManager(),
        router=SimpleNamespace(regime_map={}), audit={})
    observer.advance(event(data, "2020-01-03"))
    revision(data, high=1.)
    observer.advance(event(data, "2020-01-08"))
    observer.finish()
    payload = observer.export()
    horizons, candidates, _, outcomes = _validate_input(payload)
    book = OutcomeRevisionBook(payload, candidates, horizons, outcomes)
    cid = observer.candidates[0].candidate_id
    assert (cid, 1) not in book.as_of("2020-01-03")
    assert book.eligible(book.as_of("2020-01-04")[(cid, 1)])
    assert not book.eligible(book.as_of("2020-01-09")[(cid, 1)])
    assert book.as_of("2020-01-09")[(cid, 20)]["status"] == "censored_end_of_data"


def test_unknown_entry_availability_can_only_mature_after_documented_revision_arrives():
    frame = documented()
    frame.loc[frame.index[1], "available_at"] = pd.NaT
    data = reader(frame)
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-07"))
    assert result.results == []
    revision(data)
    result.advance(event(data, "2020-01-08"))
    assert result.results[0]["available_at"] == iso("2020-01-08")
    assert result.results[0]["training_eligible"]


def test_label_correction_outside_indicator_lookback_is_still_observed_from_raw_version_proof():
    data = reader()
    result = tracker(data, horizons=(1,))
    result.advance(event(data, "2020-01-03"))
    revision(data, open=101., low=100.)
    market = event(data, "2020-01-08")
    shortened = market.histories["X"].tail(1).copy()
    market = replace(market, histories={"X": shortened})
    assert pd.Timestamp("2020-01-02") not in shortened.index
    result.advance(market)
    assert len(result.revisions) == 2
    assert result.results[0]["entry_reference"] == 101.


def test_indicator_computation_strips_heavy_proof_but_historical_and_live_events_restore_it(monkeypatch):
    from core.market_data import HistoricalMarketDataAdapter, LiveMarketDataAdapter
    from core.temporal_data import strategy_market_view

    calls = []
    def calculate(frame):
        assert "temporal_source_versions" not in frame.attrs
        calls.append(len(frame))
        frame["fixture_mean"] = frame.close.rolling(2, min_periods=1).mean()
    monkeypatch.setattr("core.market_data.Indicators.calculate_all", calculate)
    historical = HistoricalMarketDataAdapter({"X": documented()}, timeframe="1d", temporal_policy="strict")
    events = list(historical.stream())
    assert calls and all(e.histories["X"].attrs["temporal_source_versions"] for e in events)
    view = strategy_market_view(events[-1])
    assert "temporal_source_versions" not in view.histories["X"].attrs
    assert view.histories["X"].attrs["temporal_audit"]["mode"] == "strict"
    assert events[-1].histories["X"].attrs["temporal_source_versions"]
    source = SimpleNamespace(fetch_ccxt=lambda *args, **kwargs: documented())
    live = LiveMarketDataAdapter(["X"], source, timeframe="1d", temporal_policy="strict",
        clock=lambda: pd.Timestamp("2020-01-08T00:00:03Z"))
    observed = live.poll(pd.Timestamp("2020-01-08T00:00:03Z"))
    assert observed and all(e.histories["X"].attrs["temporal_source_versions"] for e in observed)
