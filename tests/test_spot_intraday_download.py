"""Retrospective import identities, actual availability and bounded resume."""
from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from scripts.fetch_spot_intraday import download_intraday, HOUR_MS, retry_after_seconds, validate_page


START = "2020-01-01T00:00:00Z"
END = "2020-02-15T00:00:00Z"  # 1080 hours, two raw pages


class Client:
    status = 200
    retry_after = None

    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def fetch(self, params, timeout_seconds):
        self.calls.append(dict(params))
        if len(self.calls) == self.fail_at:
            raise TimeoutError("sensitive transport details must not be persisted")
        hours = min(1000, (params["endTime"]+1-params["startTime"])//HOUR_MS)
        return [[at, "100", "101", "99", "100", "2", at+HOUR_MS-1, "200", 10, "1", "100", "0"]
                for at in range(params["startTime"], params["startTime"]+hours*HOUR_MS, HOUR_MS)]


def test_full_range_immutable_pages_and_import_availability_are_current(tmp_path):
    client = Client()
    output = tmp_path/"download"
    result = download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=client)
    assert result["status"] == "completed" and result["request_count"] == 2
    row = result["symbols"]["BTCUSDT"]
    assert row["rows"] == 1080 and row["missing_hours"] == []
    frame = pd.read_csv(row["csv_path"])
    assert pd.to_datetime(frame.available_at, utc=True).min() > pd.Timestamp(END)
    assert frame.historical_pit_available_at.isna().all()
    files = {p: p.read_bytes() for p in output.rglob("*.json") if p.name != "checkpoint.json"}
    again = download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=client, resume=True)
    assert again["request_count"] == 0 and again["resumed_pages"] == 2
    assert all(path.read_bytes() == content for path, content in files.items())
    assert len(client.calls) == 2


def test_failure_keeps_raw_checkpoint_and_resume_only_fetches_missing_pages(tmp_path):
    client = Client(fail_at=2)
    output = tmp_path/"download"
    result = download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=client)
    assert result["status"] == "incomplete"
    assert result["error"]["exception_chain"] == ["TimeoutError"]
    assert "sensitive" not in json.dumps(result)
    assert len(list((output/"raw"/"BTCUSDT").glob("*.metadata.json"))) == 1
    other = Client()
    resumed = download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=other, resume=True)
    assert resumed["status"] == "completed" and resumed["resumed_pages"] == 1
    assert len(other.calls) == 1
    assert other.calls[0]["startTime"] == client.calls[1]["startTime"]


def test_tampered_raw_page_and_changed_resume_protocol_fail_closed(tmp_path):
    output = tmp_path/"download"
    download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=Client())
    with pytest.raises(ValueError, match="unchanged"):
        download_intraday(output, start=START, end_exclusive=END, symbols=("ETHUSDT",), client=Client(), resume=True)
    raw = next(p for p in (output/"raw"/"BTCUSDT").glob("*.json") if ".metadata." not in p.name)
    raw.write_text("[]", encoding="utf-8")
    result = download_intraday(output, start=START, end_exclusive=END, symbols=("BTCUSDT",), client=Client(), resume=True)
    assert result["status"] == "incomplete" and result["request_count"] == 0


def test_rate_limit_wait_is_bounded_and_403_never_switches_host_or_retries(tmp_path, monkeypatch):
    from scripts import fetch_spot_intraday as module
    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    class Limited(Client):
        def fetch(self, params, timeout_seconds):
            if not self.calls:
                self.calls.append(dict(params))
                self.status, self.retry_after = 429, "2"
                raise RuntimeError("venue rate limited")
            self.status = 200
            return super().fetch(params, timeout_seconds)
    client = Limited()
    result = download_intraday(tmp_path/"limited", start=START, end_exclusive=END, symbols=("BTCUSDT",), client=client)
    assert result["status"] == "completed" and waits == [2.]
    class Forbidden(Client):
        def fetch(self, params, timeout_seconds):
            self.calls.append(dict(params))
            self.status = 403
            raise RuntimeError("refused")
    denied = Forbidden()
    result = download_intraday(tmp_path/"denied", start=START, end_exclusive=END, symbols=("BTCUSDT",), client=denied)
    assert result["status"] == "incomplete" and result["http_status"] == 403 and len(denied.calls) == 1


def test_retry_after_and_candle_bounds_are_explicit():
    assert retry_after_seconds("5") == 5
    assert retry_after_seconds("Wed, 01 Jan 2020 00:00:02 GMT", datetime(2020, 1, 1, tzinfo=timezone.utc)) == 2
    with pytest.raises(ValueError):
        retry_after_seconds("nan")
    with pytest.raises(ValueError, match="candle time"):
        validate_page([[1, 100, 101, 99, 100, 2, HOUR_MS, 200, 10, 1, 100]], cursor=0, end_ms=HOUR_MS)


def test_real_maintenance_short_candle_is_preserved_and_flagged(tmp_path):
    class ShortHour(Client):
        def fetch(self, params, timeout_seconds):
            rows = super().fetch(params, timeout_seconds)
            rows[0][6] = rows[0][0]+1200000-1
            return rows
    result = download_intraday(tmp_path/"short", start=START, end_exclusive="2020-01-01T02:00:00Z",
                              symbols=("BTCUSDT",), client=ShortHour())
    frame = pd.read_csv(result["symbols"]["BTCUSDT"]["csv_path"])
    assert frame.bar_duration_ms.tolist() == [1200000, HOUR_MS]
    assert frame.is_full_hour.tolist() == [False, True]
    assert result["symbols"]["BTCUSDT"]["shortened_hours"] == ["2020-01-01T00:00:00+00:00"]
