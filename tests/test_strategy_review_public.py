"""Official-source collection reports unavailable evidence instead of inventing it."""
from __future__ import annotations

import json

import pytest
import requests

from scripts.collect_strategy_review_public import (
    ENDPOINTS, PublicClient, PublicSourceError, TIMEFRAMES, fetch_stream,
    gap_intervals, immutable_write, normalize_timestamp_ms, parse_candle,
    utc_ms, year_windows,
)


START = utc_ms("2024-01-01")
DAY = TIMEFRAMES["1d"]


def _row(timestamp=START, *, venue="binance", confirmed="1"):
    if venue == "binance":
        return [timestamp, "100", "110", "90", "105", "2", timestamp + DAY - 1]
    return [str(timestamp), "100", "110", "90", "105", "2", "200", "200", confirmed]


@pytest.mark.parametrize("factor", [1, 1000])
def test_millisecond_and_microsecond_timestamps_normalize_without_future_bars(factor):
    row = _row()
    row[0] *= factor
    row[6] = row[6] * factor + (factor - 1)
    parsed = parse_candle("binance", row, "1d", START, START + DAY, START + DAY)
    assert parsed[0] == START
    assert normalize_timestamp_ms(row[0]) == START
    assert parse_candle("binance", row, "1d", START, START + DAY, START + DAY - 1) is None


def test_seconds_timestamp_is_rejected_and_utc_alignment_is_required():
    with pytest.raises(ValueError, match="unit"):
        normalize_timestamp_ms(START // 1000)
    with pytest.raises(ValueError, match="UTC"):
        parse_candle("binance", _row(START + 3600000), "1d", START, START + 2*DAY, START + 2*DAY)


def test_okx_unconfirmed_bar_and_out_of_window_bar_are_not_used():
    assert parse_candle("okx", _row(venue="okx", confirmed="0"), "1d", START, START+DAY, START+DAY) is None
    assert parse_candle("okx", _row(venue="okx"), "1d", START+DAY, START+2*DAY, START+2*DAY) is None


@pytest.mark.parametrize("index,value", [(1, "nan"), (2, "99"), (3, "106"), (4, "0"), (5, "-2")])
def test_bad_ohlcv_is_not_filled_or_coerced_to_zero(index, value):
    row = _row()
    row[index] = value
    with pytest.raises(ValueError):
        parse_candle("binance", row, "1d", START, START + DAY, START + DAY)


def test_year_windows_partition_contiguously_at_utc_january_boundaries():
    start, end = utc_ms("2021-09-01"), utc_ms("2026-09-19")
    windows = list(year_windows(start, end))
    assert len(windows) == 6
    assert windows[0] == (start, utc_ms("2022-01-01"))
    assert windows[-1] == (utc_ms("2026-01-01"), end)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))


def test_gaps_preserve_missingness_and_compress_only_adjacent_bars():
    gaps = gap_intervals([START + DAY, START + 4*DAY], START, START + 6*DAY, DAY)
    assert [gap["missing_bars"] for gap in gaps] == [1, 2, 1]
    assert gaps[1]["start"] == "2024-01-03T00:00:00+00:00"


class _PagingClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def get(self, venue, parameters):
        self.calls.append((venue, parameters))
        if venue == "binance":
            rows = [r for r in self.rows if parameters["startTime"] <= r[0] <= parameters["endTime"]][:2]
            return rows, {"url": ENDPOINTS[venue]}
        rows = [r for r in reversed(self.rows) if parameters["before"] < int(r[0]) < parameters["after"]][:2]
        return {"code": "0", "data": rows}, {"url": ENDPOINTS[venue]}


@pytest.mark.parametrize("venue", ["binance", "okx"])
def test_pagination_has_no_missing_or_duplicate_boundaries(venue):
    client = _PagingClient([_row(START + i*DAY, venue=venue) for i in range(5)])
    frame, records, failures = fetch_stream(client, venue, "BTC/USDT", "1d", START, START+5*DAY, START+5*DAY)
    assert failures == [] and len(frame) == 5 and len(records) == 3
    assert frame["timestamp"].is_unique and frame["timestamp"].is_monotonic_increasing
    assert {call[0] for call in client.calls} == {venue}
    if venue == "okx":
        assert all(call[1]["bar"] == "1Dutc" for call in client.calls)


def test_source_failure_does_not_try_alternative_venue_or_fill_bars():
    class Broken:
        def __init__(self):
            self.calls = []

        def get(self, venue, parameters):
            self.calls.append((venue, parameters))
            raise PublicSourceError("HTTP 403")

    client = Broken()
    frame, records, failures = fetch_stream(client, "okx", "SOL/USDT", "1d", START, START+DAY, START+DAY)
    assert frame.empty and records == []
    assert "HTTP 403" in failures[0]["reason"]
    assert len(client.calls) == 1 and client.calls[0][0] == "okx"


def test_shortened_official_bar_is_rejected_without_discarding_following_valid_bars():
    rows = [_row(START + i*DAY) for i in range(4)]
    rows[1][6] -= 3600000
    client = _PagingClient(rows)
    frame, _, failures = fetch_stream(client, "binance", "BTC/USDT", "1d", START, START+4*DAY, START+4*DAY)
    assert len(frame) == 3
    assert [utc_ms(value) for value in frame["timestamp"]] == [START, START+2*DAY, START+3*DAY]
    assert len(failures) == 1
    assert failures[0]["rejected_candle_timestamp"] == START + DAY


def test_raw_response_is_cached_and_hash_verified(tmp_path, monkeypatch):
    client = PublicClient(tmp_path)
    calls = []

    class Response:
        status_code = 200
        content = json.dumps([_row()]).encode()

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(client.session, "get", get)
    monkeypatch.setattr("scripts.collect_strategy_review_public.time.sleep", lambda _: None)
    first, record = client.get("binance", {"symbol": "BTCUSDT"})
    second, _ = client.get("binance", {"symbol": "BTCUSDT"})
    assert first == second and len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    (tmp_path / record["body_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        client.get("binance", {"symbol": "BTCUSDT"})


def test_network_denial_is_saved_as_specific_evidence(tmp_path, monkeypatch):
    client = PublicClient(tmp_path)

    def fail(*args, **kwargs):
        raise requests.ConnectionError("network denied")

    monkeypatch.setattr(client.session, "get", fail)
    monkeypatch.setattr("scripts.collect_strategy_review_public.time.sleep", lambda _: None)
    with pytest.raises(PublicSourceError, match="network denied"):
        client.get("okx", {"instId": "ETH-USDT"})
    errors = list((tmp_path / "raw/okx").glob("*.json"))
    assert len(errors) == 3
    assert all("network denied" in json.loads(path.read_text())["error"] for path in errors)


def test_immutable_files_cannot_be_replaced(tmp_path):
    path = tmp_path / "evidence.json"
    immutable_write(path, b"first")
    immutable_write(path, b"first")
    with pytest.raises(ValueError, match="immutable"):
        immutable_write(path, b"second")
    assert path.read_bytes() == b"first"
