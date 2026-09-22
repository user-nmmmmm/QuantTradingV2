"""Historical streams preserve row semantics without retaining per-bar objects."""

from __future__ import annotations

import gc
import tracemalloc

import numpy as np
import pandas as pd
import pytest

from core.market_data import HistoricalMarketDataAdapter, normalize_market_frame


def _reference_normalize(frame):
    """Pre-optimization boundary behavior, including duplicate precedence."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result.loc[~pd.isna(result.index)].copy()
    if result.empty:
        return result
    if result.index.tz is not None:
        result.index = result.index.tz_convert("UTC").tz_localize(None)
    return result[~result.index.duplicated(keep="last")].sort_index()


@pytest.mark.parametrize("kind", ["float", "mixed", "extension"])
@pytest.mark.parametrize("alignment", ["union", "intersection"])
@pytest.mark.parametrize("start_at", [None, "2024-01-02T12:00:00+08:00"])
def test_sparse_stream_matches_original_rows_and_positions(kind, alignment, start_at):
    dates = pd.date_range("2024-01-01", periods=9, freq="12h", tz="UTC")
    values = {"close": np.arange(9, dtype=np.float32), "volume": np.arange(9)}
    if kind == "float":
        values["volume"] = np.arange(9, dtype=np.float32)
    elif kind == "mixed":
        values["label"] = list("abcdefghi")
    else:
        values["optional"] = pd.array([1, None, 3, 4, None, 6, 7, 8, 9], dtype="Int64")
        values["category"] = pd.Categorical(list("abcabcabc"))
    source = pd.DataFrame(values, index=dates)
    frames = {"A": source.iloc[[7, 0, 3, 3, 8]], "B": source.iloc[[1, 3, 5, 7]],
              "empty": source.iloc[:0]}
    adapter = HistoricalMarketDataAdapter(
        frames, alignment_mode=alignment, calculate_indicators=False, timeframe="12h"
    )
    # Exercise multiple boundaries with a small fixture.
    adapter._POSITION_CHUNK_SIZE = 2
    prepared = {name: _reference_normalize(frame) for name, frame in frames.items()
                if not frame.empty}
    timeline = prepared["A"].index
    timeline = getattr(timeline, alignment)(prepared["B"].index).sort_values()
    if start_at is not None:
        point = pd.Timestamp(start_at).tz_convert("UTC").tz_localize(None)
        timeline = timeline[timeline >= point]
    events = list(adapter.stream(start_at=start_at))
    assert [event.timestamp for event in events] == list(timeline)
    for event in events:
        expected_positions = {
            name: int(frame.index.get_loc(event.timestamp))
            for name, frame in prepared.items() if event.timestamp in frame.index
        }
        assert event.positions == expected_positions
        assert set(event.bars) == set(expected_positions)
        assert event.timeframe == "12h"
        assert event.source == "historical"
        for name, position in expected_positions.items():
            assert isinstance(event.positions[name], int)
            pd.testing.assert_series_equal(event.bars[name], prepared[name].iloc[position])
            assert event.histories[name] is adapter.data_map[name]


@pytest.mark.parametrize("index", [
    pd.date_range("2024-01-01", periods=4),
    pd.date_range("2024-01-01", periods=4, tz="Asia/Singapore"),
    pd.Index(["2024-01-02", "invalid", "2024-01-01", "2024-01-02"]),
    pd.DatetimeIndex([pd.NaT, pd.NaT, pd.NaT, pd.NaT]),
    pd.DatetimeIndex(["2024-01-02", "2024-01-01", "2024-01-01", "2024-01-03"]),
])
def test_normalization_equivalence_and_source_isolation(index):
    frame = pd.DataFrame({"close": np.arange(4, dtype=float)}, index=index)
    original = frame.copy(deep=True)
    result = normalize_market_frame(frame)
    pd.testing.assert_frame_equal(result, _reference_normalize(frame))
    if not result.empty:
        result.iloc[0, 0] = 999.0
        result["strategy_column"] = 1.0
    pd.testing.assert_frame_equal(frame, original)


def test_concurrent_streams_see_strategy_columns_without_reusing_bars():
    dates = pd.date_range("2024-01-01", periods=9)
    source = pd.DataFrame({"close": np.arange(9, dtype=float)}, index=dates)
    adapter = HistoricalMarketDataAdapter({"A": source}, calculate_indicators=False)
    adapter._POSITION_CHUNK_SIZE = 2
    early = adapter.stream()
    late = adapter.stream(start_at=dates[4].tz_localize("Asia/Singapore"))
    first = next(early)
    retained = first.bars["A"].copy(deep=True)
    assert next(late).positions == {"A": 4}
    adapter.data_map["A"]["signal"] = np.arange(9) * 10
    for stream, positions in [(early, range(1, 9)), (late, range(5, 9))]:
        for position, event in zip(positions, stream):
            assert event.positions == {"A": position}
            pd.testing.assert_series_equal(
                event.bars["A"], adapter.data_map["A"].iloc[position]
            )
            assert event.bars["A"] is not first.bars["A"]
    pd.testing.assert_series_equal(first.bars["A"], retained)
    assert len(list(adapter.stream())) == 9
    assert list(adapter.stream(start_at="2025-01-01")) == []
    assert list(HistoricalMarketDataAdapter({}, calculate_indicators=False).stream()) == []


def test_constructor_memory_is_close_to_frame_storage():
    dates = pd.date_range("2024-01-01", periods=20_000, freq="min")
    frames = {str(i): pd.DataFrame({"close": np.arange(len(dates), dtype=float)},
                                 index=dates) for i in range(6)}
    frame_bytes = sum(frame.memory_usage(index=True, deep=True).sum()
                      for frame in frames.values())
    gc.collect()
    tracemalloc.start()
    try:
        adapter = HistoricalMarketDataAdapter(frames, calculate_indicators=False)
        retained, peak = tracemalloc.get_traced_memory()
        assert len(adapter.data_map) == len(frames)
        # A Python Timestamp/int mapping costs many times the source frame;
        # include headroom for pandas metadata and normalization intermediates.
        assert retained < frame_bytes * 2 + 1_000_000
        assert peak < frame_bytes * 3 + 1_000_000
    finally:
        tracemalloc.stop()


def test_sparse_stream_position_memory_does_not_scale_as_full_universe_matrix():
    symbols, rows = 12, 20_000
    union = pd.date_range("2024-01-01", periods=symbols * rows, freq="min")
    frames = {str(i): pd.DataFrame({"close": np.arange(rows, dtype=float)},
                                 index=union[i::symbols]) for i in range(symbols)}
    adapter = HistoricalMarketDataAdapter(frames, calculate_indicators=False)
    gc.collect()
    tracemalloc.start()
    try:
        stream = adapter.stream()
        event = next(stream)
        retained, _ = tracemalloc.get_traced_memory()
        assert event.positions == {"0": 0}
        # The union timeline is allowed; a full union x symbols position matrix
        # would alone exceed 23 MB here. Chunks and one event stay well below it.
        assert retained < union.nbytes * 3 + 4_000_000
        stream.close()
    finally:
        tracemalloc.stop()
