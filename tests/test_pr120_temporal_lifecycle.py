"""PR120: strict selection must retain cutoff-qualified lifecycle entry guards."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.foundation.runtime import MarketDataSlice
from core.planning.allocation import EntryCandidate
from core.market.temporal_data import TemporalOHLCV
from research.ml_selection.dataset import build_dataset, feature_snapshot
from research.ml_selection.selector import ResearchSelector
from signal_meta.selection_context import SelectionTemporalContext


SYMBOL = "A/USDT"
FLAGS = ("entry_blocked", "scheduled_exit")


def source_frame():
    index = pd.date_range("2020-01-01", periods=80, freq="D", tz="UTC")
    close = 100.0 + np.arange(len(index)) * 0.2
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 10000.0,
            "entry_blocked": False,
            "scheduled_exit": False,
        },
        index=index,
    )
    frame["available_at"] = frame.index + pd.Timedelta(days=1)
    frame["observed_at"] = frame["available_at"]
    frame["revision_id"] = "original"
    frame.attrs["availability_evidence"] = {
        "kind": "source_publication",
        "reference": "synthetic:pr120-lifecycle",
    }
    return frame


def membership():
    return {
        SYMBOL: {
            "listed_at": "2019-01-01T00:00:00Z",
            "available_at": "2019-01-01T00:00:00Z",
            "observed_at": "2019-01-01T00:00:00Z",
            "source": "synthetic:pr120-listing",
        }
    }


def store_for(frame):
    store = TemporalOHLCV("synthetic:pr120:A", timeframe="1d", policy="strict")
    store.ingest(frame)
    return store


def decision_row(source, context):
    result = build_dataset({SYMBOL: source}, temporal_contexts=[context], membership=membership())
    assert len(result) == 1
    return result.iloc[0]


def assert_guard(row, blocked):
    assert bool(row.eligible) is (not blocked)
    assert row.exclusion_reason == ("lifecycle_entry_blocked" if blocked else "")
    assert bool(row.history_available)


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("kind", ["frame", "store", "visible_frame"])
def test_temporal_entry_guard_matches_existing_non_temporal_exclusion(flag, kind):
    frame = source_frame()
    frame.loc[frame.index[-1], flag] = True
    cutoff = frame.index[-1] + pd.Timedelta(days=1)
    ordinary = build_dataset({SYMBOL: frame}, membership=membership()).iloc[-1]
    assert_guard(ordinary, True)
    context = SelectionTemporalContext(cutoff)
    source = frame if kind == "frame" else store_for(frame)
    if kind == "visible_frame":
        source = context.visible(source, symbol=SYMBOL)
    snapshot = feature_snapshot(source, temporal_context=context)
    assert snapshot[flag] is True
    assert snapshot[FLAGS[1 - FLAGS.index(flag)]] is False
    assert_guard(decision_row(source, context), True)


@pytest.mark.parametrize("kind", ["frame", "store", "visible_frame"])
def test_explicitly_unblocked_visible_candidate_remains_eligible(kind):
    frame = source_frame()
    context = SelectionTemporalContext(frame.index[-1] + pd.Timedelta(days=1))
    source = frame if kind == "frame" else store_for(frame)
    if kind == "visible_frame":
        source = context.visible(source, symbol=SYMBOL)
    snapshot = feature_snapshot(source, temporal_context=context)
    assert snapshot["entry_blocked"] is False
    assert snapshot["scheduled_exit"] is False
    assert_guard(decision_row(source, context), False)


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("initially_blocked", [False, True])
def test_flag_only_revision_changes_eligibility_only_when_available(flag, initially_blocked):
    frame = source_frame()
    frame.loc[frame.index[-1], flag] = initially_blocked
    store = store_for(frame)
    close = frame.index[-1] + pd.Timedelta(days=1)
    release = close + pd.Timedelta(hours=6)
    before = SelectionTemporalContext(release - pd.Timedelta(nanoseconds=1))
    prior = decision_row(store, before)
    assert_guard(prior, initially_blocked)
    correction = frame.iloc[[-1]].copy()
    correction[flag] = not initially_blocked
    correction["revision_id"] = "lifecycle-correction"
    correction["available_at"] = correction["observed_at"] = release
    store.ingest(correction)
    unchanged = decision_row(store, before)
    assert_guard(unchanged, initially_blocked)
    assert unchanged.feature_snapshot_sha256 == prior.feature_snapshot_sha256
    assert unchanged.source_versions_sha256 == prior.source_versions_sha256
    current = decision_row(store, SelectionTemporalContext(release))
    assert_guard(current, not initially_blocked)
    assert current.source_versions_sha256 != prior.source_versions_sha256
    assert current.feature_snapshot_sha256 != prior.feature_snapshot_sha256


@pytest.mark.parametrize("flag", FLAGS)
def test_published_guard_release_does_not_anticipate_local_observation(flag):
    frame = source_frame()
    frame.loc[frame.index[-1], flag] = True
    store = store_for(frame)
    close = frame.index[-1] + pd.Timedelta(days=1)
    published = close + pd.Timedelta(hours=1)
    observed = published + pd.Timedelta(hours=1)
    correction = frame.iloc[[-1]].copy()
    correction[flag] = False
    correction["revision_id"] = "published-release"
    correction["available_at"] = published
    correction["observed_at"] = observed
    store.ingest(correction)
    local = SelectionTemporalContext(published, {"mode": "strict", "knowledge": "local"})
    public = SelectionTemporalContext(published, {"mode": "strict", "knowledge": "published"})
    assert_guard(decision_row(store, local), True)
    assert_guard(decision_row(store, public), False)
    assert_guard(decision_row(store, SelectionTemporalContext(observed)), False)


@pytest.mark.parametrize("flag", FLAGS)
def test_future_candle_guard_does_not_change_visible_candidate(flag):
    frame = source_frame()
    cutoff = frame.index[-2] + pd.Timedelta(days=1)
    context = SelectionTemporalContext(cutoff)
    prior = decision_row(frame, context)
    changed = frame.copy()
    changed.loc[changed.index[-1], flag] = True
    actual = decision_row(changed, context)
    assert_guard(prior, False)
    assert_guard(actual, False)
    assert actual.feature_snapshot_sha256 == prior.feature_snapshot_sha256
    assert actual.source_versions_sha256 == prior.source_versions_sha256


@pytest.mark.parametrize("flag", FLAGS)
def test_guard_added_to_legacy_frozen_frame_cannot_be_silently_discarded(flag):
    frame = source_frame()
    legacy = frame.drop(columns=list(FLAGS))
    annotated = store_for(legacy).annotate(legacy.copy())
    annotated[flag] = False
    annotated.loc[annotated.index[-1], flag] = True
    context = SelectionTemporalContext(frame.index[-1] + pd.Timedelta(days=1))
    with pytest.raises(ValueError, match="lifecycle|frozen|frame.*versions"):
        decision_row(annotated, context)


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("blocked", [False, True])
def test_actual_selector_respects_lifecycle_guard_in_strict_serving(flag, blocked):
    frame = source_frame()
    frame.loc[frame.index[-1], flag] = blocked
    context = SelectionTemporalContext(frame.index[-1] + pd.Timedelta(days=1))
    dataset = build_dataset({SYMBOL: frame}, temporal_contexts=[context], membership=membership())
    view = context.visible(frame, symbol=SYMBOL)
    event = MarketDataSlice(
        frame.index[-1],
        {SYMBOL: view.iloc[-1]},
        {SYMBOL: view},
        timeframe="1d",
        source="historical_strict",
        positions={SYMBOL: len(view) - 1},
    )
    candidate = EntryCandidate(
        symbol=SYMBOL,
        strategy=SimpleNamespace(name="OriginalStrategy"),
        bar_index=len(frame) - 1,
        frame=frame,
        state="trend",
        signal={"action": "buy", "stop_loss": 90.0, "requested_qty": 2.0},
        score=1.5,
    )
    portfolio = SimpleNamespace(cash=10000.0, positions={}, get_total_value=lambda prices: 10000.0)
    selector = ResearchSelector(dataset, mode="qualified_native")
    selected = selector.select(
        [candidate],
        event=event,
        portfolio=portfolio,
        broker=SimpleNamespace(orders={}),
        risk_manager=SimpleNamespace(max_leverage=1.0),
        current_prices={SYMBOL: float(view.close.iloc[-1])},
    )
    assert len(selected) == (0 if blocked else 1)
    assert selector.audit[0]["selected"] is (not blocked)
    if blocked:
        assert selector.audit[0]["reason"] == "lifecycle_entry_blocked"


@pytest.mark.parametrize("flag", FLAGS)
def test_price_revision_missing_prior_guard_cannot_reenable_selection(flag):
    frame = source_frame()
    frame.loc[frame.index[-1], flag] = True
    store = store_for(frame)
    close = frame.index[-1] + pd.Timedelta(days=1)
    before = SelectionTemporalContext(close)
    prior = decision_row(store, before)
    assert_guard(prior, True)
    revision = frame.iloc[[-1]].drop(columns=list(FLAGS)).copy()
    revision[["open", "high", "low", "close"]] = [120.0, 121.0, 119.0, 120.0]
    revision["revision_id"] = "price-only-amendment"
    revision["available_at"] = revision["observed_at"] = close + pd.Timedelta(hours=1)
    store.ingest(revision)
    unchanged = decision_row(store, before)
    assert_guard(unchanged, True)
    assert unchanged.feature_snapshot_sha256 == prior.feature_snapshot_sha256
    try:
        current = decision_row(store, SelectionTemporalContext(close + pd.Timedelta(hours=1)))
    except ValueError as error:
        assert "lifecycle" in str(error)
    else:
        assert_guard(current, True)
