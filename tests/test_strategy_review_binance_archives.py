"""Official archive checksums do not make incomplete candles acceptable."""
import io
from types import SimpleNamespace
import zipfile

import pytest

from scripts.verify_strategy_review_binance_archives import (
    ArchiveClient, _job, _ms, compare_rows, parse_archive, registered_archives,
    sha256, verify_checksum,
)


def _zip(job, rows):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(job["filename"][:-4] + ".csv", "\n".join(",".join(map(str, row)) for row in rows))
    return output.getvalue()


def _row(start, *, unit="ms", duration=86_400_000):
    scale = 1000 if unit == "us" else 1
    return [start * scale, "100.0", "110.00", "90", "101", "12.5",
            (start + duration) * scale - 1, "1234", "3", "4", "5", "0"]


@pytest.mark.parametrize("day,unit", [("2024-12-01", "ms"), ("2025-01-01", "us")])
def test_archive_ms_and_us_roundtrip_and_matching(tmp_path, day, unit):
    start = _ms(day)
    job = _job("BTCUSDT", "1d", "daily", day, start, start + 86_400_000)
    payload = _zip(job, [_row(start, unit=unit)])
    official = f"{sha256(payload)}  {job['filename']}\n".encode()
    assert verify_checksum(payload, official, job["filename"]) == sha256(payload)
    rows, parsed = parse_archive(payload, job, start + 86_400_000)
    assert parsed["rows_retained"] == 1 and parsed["timestamp_units"] == {unit: 1}
    comparison = compare_rows(rows, {start: (100, 110, 90, 101, 12.5)}, job)
    assert comparison["status"] == "matched" and comparison["exact_numeric_rows"] == 1


def test_checksum_is_bound_to_bytes_and_requested_filename():
    payload = b"some archive bytes"
    digest = sha256(payload)
    assert verify_checksum(payload, f"{digest} *a.zip".encode(), "a.zip") == digest
    with pytest.raises(ValueError, match="mismatch"):
        verify_checksum(payload + b"changed", f"{digest} a.zip".encode(), "a.zip")
    with pytest.raises(ValueError, match="filename"):
        verify_checksum(payload, f"{digest} b.zip".encode(), "a.zip")


def test_2021_september29_short_interval_remains_rejected():
    start = _ms("2021-09-29T04:00:00Z")
    job = _job("BTCUSDT", "4h", "daily", "2021-09-29", _ms("2021-09-29"), _ms("2021-09-30"))
    payload = _zip(job, [_row(start, duration=10_800_000)])  # Official odd 3-hour interval.
    assert verify_checksum(payload, f"{sha256(payload)} {job['filename']}".encode(), job["filename"])
    rows, parsed = parse_archive(payload, job, _ms("2021-09-30"))
    assert rows == {} and parsed["rows_retained"] == 0
    assert parsed["rejected_rows"][0]["reason"] == "Binance close time does not describe a complete interval"


def test_open_bar_and_duplicates_never_silently_become_retained_bars():
    start = _ms("2025-01-01")
    job = _job("BTCUSDT", "1d", "daily", "2025-01-01", start, start + 86_400_000)
    row = _row(start, unit="us")
    rows, parsed = parse_archive(_zip(job, [row]), job, start + 43_200_000)
    assert not rows and len(parsed["filtered_rows"]) == 1
    rows, parsed = parse_archive(_zip(job, [row, row, row]), job, start + 86_400_000)
    assert not rows and len(parsed["rejected_rows"]) == 2


def test_comparison_records_differences_and_missing_evidence_without_zero_filling():
    start = _ms("2025-01-01")
    job = _job("BTCUSDT", "1d", "monthly", "2025-01", start, _ms("2025-02-01"))
    comparison = compare_rows({start: (100, 110, 90, 101, 12.5)}, {start: (100, 110, 90, 102, 12.5)}, job)
    assert comparison["mismatched_rows"] == 1
    assert comparison["mismatches"][0]["fields"]["close"]["difference"] == -1
    assert compare_rows({}, None, job)["matched_rows"] is None
    assert compare_rows({}, {start: (100, 110, 90, 101, 12.5)}, job)["validated_only_timestamps"]


def test_fixed_archive_matrix_has_936_nonoverlapping_members():
    jobs = registered_archives()
    assert len(jobs) == 936 and len({row["url"] for row in jobs}) == 936
    stream = [row for row in jobs if row["symbol"] == "BTCUSDT" and row["timeframe"] == "4h"]
    assert len(stream) == 78
    assert stream[0]["start_ms"] == _ms("2021-09-01")
    assert stream[-1]["end_exclusive_ms"] == _ms("2026-09-19")
    assert all(a["end_exclusive_ms"] == b["start_ms"] for a, b in zip(stream, stream[1:]))


def test_permanent_404_is_cached_without_retry_or_substitution(tmp_path):
    client = ArchiveClient(tmp_path)
    calls = []

    def unavailable(url, **kwargs):
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return SimpleNamespace(status_code=404, content=b"not available", headers={})

    client.local.session = SimpleNamespace(get=unavailable)
    url = registered_archives()[0]["url"]
    _, first = client.get(url)
    _, cached = client.get(url)
    assert calls == [url]
    assert first == cached and first["http_status"] == 404
    with pytest.raises(ValueError, match="official"):
        client.get("https://unregistered.example/alternate.zip")
