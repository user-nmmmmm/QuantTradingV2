"""AUD-03: bind comparisons to consumed bytes and keep cache identity portable."""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd
import pytest

from scripts import data_inputs, fetch_binance_data as download, run_backtest_matrix as matrix


CSV = (b"timestamp,open,high,low,close,volume\n"
       b"2020-01-01,1,2,0.5,1.5,10\n2020-01-02,1,2,0.5,1.5,10\n")
BAR = tuple(Decimal(value) for value in ("1", "2", "0.5", "1.5", "10"))


@pytest.fixture
def frozen(tmp_path):
    for name in ("BTC_USDT.csv", "ETH_USDT.csv"):
        (tmp_path / name).write_bytes(CSV)
    path = tmp_path / data_inputs.FROZEN_MANIFEST
    path.write_text(data_inputs.render(data_inputs.build(tmp_path)), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("mutation", [
    lambda data: data.replace(b"1.5", b"1.6"),
    lambda data: data + b"2020-01-03,1,2,0.5,1.5,10\n",
    lambda data: data.replace(b"2020-01-01", b"2019-12-31"),
    lambda data: data.replace(b"2020-01-02", b"2020-01-03"),
])
def test_corroboration_refuses_stale_registered_bytes_before_fetch(frozen, mutation):
    path, manifest = frozen / "BTC_USDT.csv", frozen / data_inputs.FROZEN_MANIFEST
    original = manifest.read_bytes()
    path.write_bytes(mutation(CSV))
    fetch = Mock(return_value={"2020-01-01": BAR, "2020-01-02": BAR})
    with pytest.raises(ValueError, match="does not match frozen input"):
        data_inputs.corroborate(frozen, [path.name], fetch=fetch)
    fetch.assert_not_called()
    path.write_bytes(CSV)  # restoring old CSV must not leave a result computed from the changed bytes
    assert manifest.read_bytes() == original and data_inputs.verify(frozen) == []


@pytest.mark.parametrize("mutation", ["selected_file", "previous_file", "manifest"])
def test_corroboration_refuses_changes_during_fetch_and_never_publishes_them(frozen, mutation):
    manifest = frozen / data_inputs.FROZEN_MANIFEST
    original = manifest.read_bytes()

    def fetch(symbol, first, last):
        if mutation == "selected_file":
            (frozen / (symbol[:3] + "_USDT.csv")).write_bytes(CSV.replace(b"1.5", b"1.6"))
        elif symbol == "ETHUSDT":
            if mutation == "previous_file":
                (frozen / "BTC_USDT.csv").write_bytes(CSV.replace(b"1.5", b"1.6"))
            else:
                manifest.write_bytes(original + b"\n")
        return {"2020-01-01": BAR, "2020-01-02": BAR}

    with pytest.raises(ValueError, match="changed during corroboration"):
        data_inputs.corroborate(frozen, fetch=fetch)
    assert b"comparison_policy" not in manifest.read_bytes()


def test_corroboration_parses_and_hashes_one_snapshot_per_file(frozen, monkeypatch):
    original_read = data_inputs.normalized_bytes
    calls = []

    def read(path):
        calls.append(path.name)
        return original_read(path)

    monkeypatch.setattr(data_inputs, "normalized_bytes", read)
    result = data_inputs.corroborate(frozen, ["BTC_USDT.csv"], fetch=lambda *args: {
        "2020-01-01": BAR, "2020-01-02": BAR})
    block = result["files"]["BTC_USDT.csv"]["corroboration"]
    assert calls == ["BTC_USDT.csv"]
    assert block["for_sha256"] == hashlib.sha256(CSV).hexdigest()
    assert block["comparison_policy"] == "binance-daily-comparison/v2" and block["status"] == "match"
    assert "corroboration" not in result["files"]["ETH_USDT.csv"]


@pytest.mark.parametrize("names", [["missing.csv"], ["BTC_USDT.csv", "missing.csv"], ["../BTC_USDT.csv"]])
def test_unknown_filenames_fail_before_any_fetch(frozen, names):
    fetch = Mock()
    with pytest.raises(ValueError, match="unknown corroboration filenames"):
        data_inputs.corroborate(frozen, names, fetch=fetch)
    fetch.assert_not_called()


def test_unknown_cli_filename_is_nonzero_and_does_not_rewrite_manifest(frozen, monkeypatch, capsys):
    monkeypatch.setattr(data_inputs, "DATA_DIR", frozen)
    manifest = frozen / data_inputs.FROZEN_MANIFEST
    before = manifest.read_bytes()
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(data_inputs, "fetch_binance_daily", fetch)
    assert data_inputs.main(["corroborate", "BTC_USDT.csv", "typo.csv"]) == 2
    assert "typo.csv" in capsys.readouterr().err and manifest.read_bytes() == before
    fetch.assert_not_called()


def test_partial_tail_requires_same_open_contained_range_and_nonincreasing_volume():
    partial = tuple(Decimal(value) for value in ("1", "1.8", "0.7", "1.7", "3"))
    result = data_inputs.compare_bars({"a": BAR, "b": partial}, {"a": BAR, "b": BAR})
    assert result["status"] == "match_except_final_bar" and result["rows_matching"] == 1


@pytest.mark.parametrize("index, value", [
    (0, "1.1"), (1, "2.1"), (2, "0.4"), (3, "3"), (4, "11"),
    (0, "0"), (3, "-1"), (4, "-1"), (1, "NaN"), (4, "Infinity"), (3, "sNaN"),
])
def test_a_corrupt_tail_is_a_mismatch_instead_of_a_partial_bar(index, value):
    tail = list(BAR)
    tail[index] = Decimal(value)
    result = data_inputs.compare_bars({"a": BAR, "b": tuple(tail)}, {"a": BAR, "b": BAR})
    assert result["status"] == "mismatch" and result["rows_matching"] == 1


def test_invalid_equal_bars_are_not_counted_as_matches():
    invalid = BAR[:4] + (Decimal("-1"),)
    result = data_inputs.compare_bars({"a": invalid}, {"a": invalid})
    assert result["status"] == "mismatch" and result["rows_matching"] == 0
    assert result["invalid_local_count"] == result["invalid_remote_count"] == 1


@pytest.mark.parametrize("body, reason", [
    (CSV.split(b"2020-01-01")[0], "empty local"),
    (CSV + b"2020-01-02,1,2,0.5,1.5,10\n", "duplicate date"),
    (CSV.replace(b"2020-01-01", b"2020-01-03"), "strictly increasing"),
    (CSV.replace(b"1.5", b"bad"), "invalid daily row"),
])
def test_invalid_local_inputs_are_rejected_before_network(frozen, body, reason):
    (frozen / "BTC_USDT.csv").write_bytes(body)
    manifest = data_inputs.build(frozen)
    (frozen / data_inputs.FROZEN_MANIFEST).write_text(data_inputs.render(manifest), encoding="utf-8")
    fetch = Mock()
    with pytest.raises(ValueError, match=reason):
        data_inputs.corroborate(frozen, ["BTC_USDT.csv"], fetch=fetch)
    fetch.assert_not_called()


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_successful_empty_response_is_an_empty_comparison_not_an_unknown_symbol(frozen, monkeypatch):
    bars = data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02",
                                          opener=lambda *args, **kwargs: Response([]))
    assert bars == {}
    manifest = data_inputs.corroborate(frozen, ["BTC_USDT.csv"], fetch=lambda *args: bars)
    block = manifest["files"]["BTC_USDT.csv"]["corroboration"]
    assert block["status"] == "mismatch" and block["only_local_count"] == 2
    monkeypatch.setattr(data_inputs, "DATA_DIR", frozen)
    monkeypatch.setattr(data_inputs, "fetch_binance_daily", lambda *args: bars)
    assert data_inputs.main(["corroborate", "BTC_USDT.csv"]) == 0
    saved = json.loads((frozen / data_inputs.FROZEN_MANIFEST).read_text())
    assert saved["files"]["BTC_USDT.csv"]["corroboration"]["status"] == "mismatch"


@pytest.mark.parametrize("body", [b'{"code":-1102}', b"{}", b"not-json", b"[]"])
def test_other_http_400_errors_are_not_relabelled_as_unknown_symbols(body):
    def fail(url, **kwargs):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))

    with pytest.raises(urllib.error.HTTPError):
        data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02", opener=fail)


def test_invalid_symbol_is_unavailable_only_for_confirmed_binance_code(frozen):
    def invalid(url, **kwargs):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b'{"code":-1121}'))

    result = data_inputs.fetch_binance_daily("NOPE", "2020-01-01", "2020-01-02", opener=invalid)
    assert result is None
    block = data_inputs.corroborate(frozen, ["BTC_USDT.csv"], fetch=lambda *args: result)
    assert block["files"]["BTC_USDT.csv"]["corroboration"]["status"] == "unavailable"


@pytest.mark.parametrize("payload", [{"code": -1121}, None, [[0]], [[0, "1", "2", "0.5", "1.5", "10"]]])
def test_malformed_success_payloads_cannot_become_valid_daily_bars(payload):
    with pytest.raises(ValueError):
        data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02",
                                       opener=lambda *args, **kwargs: Response(payload))


def test_duplicate_pages_fail_instead_of_looping(monkeypatch):
    monkeypatch.setattr(data_inputs.time, "sleep", lambda *args: None)
    stamp = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    page = [[stamp, "1", "2", "0.5", "1.5", "10"]]
    opener = Mock(side_effect=lambda *args, **kwargs: Response(page))
    with pytest.raises(ValueError, match="non-advancing"):
        data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02", opener=opener)
    assert opener.call_count == 2


@pytest.mark.parametrize("error_kind", ["invalid_symbol", "empty_page", "temporary_network"])
def test_later_empty_pages_errors_and_retries_do_not_erase_valid_rows(monkeypatch, error_kind):
    monkeypatch.setattr(data_inputs.time, "sleep", lambda *args: None)
    stamp = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    first = Response([[stamp, "1", "2", "0.5", "1.5", "10"]])
    if error_kind == "invalid_symbol":
        error = urllib.error.HTTPError("https://example.test", 400, "Bad Request", {}, io.BytesIO(b'{"code":-1121}'))
        opener = Mock(side_effect=[first, error])
        with pytest.raises(urllib.error.HTTPError):
            data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02", opener=opener)
    else:
        pages = ([urllib.error.URLError("temporary failure")] if error_kind == "temporary_network" else [])
        opener = Mock(side_effect=[first, *pages, Response([])])
        bars = data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02", opener=opener)
        assert bars == {"2020-01-01": BAR}
        assert data_inputs.compare_bars({"2020-01-01": BAR, "2020-01-02": BAR}, bars)["status"] == "mismatch"


def test_nonfinite_remote_payloads_and_inverted_ranges_fail_before_comparison():
    stamp = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    with pytest.raises(ValueError, match="OHLCV"):
        data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-02", opener=lambda *args, **kwargs:
                                       Response([[stamp, "1", "2", "0.5", "NaN", "10"]]))
    opener = Mock()
    with pytest.raises(ValueError, match="first date"):
        data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-02", "2020-01-01", opener=opener)
    opener.assert_not_called()


def test_cli_fetch_failure_preserves_the_original_manifest(frozen, monkeypatch, capsys):
    monkeypatch.setattr(data_inputs, "DATA_DIR", frozen)
    manifest = frozen / data_inputs.FROZEN_MANIFEST
    before = manifest.read_bytes()
    fetch = Mock(side_effect=[{"2020-01-01": BAR, "2020-01-02": BAR}, ValueError("bad response")])
    monkeypatch.setattr(data_inputs, "fetch_binance_daily", fetch)
    assert data_inputs.main(["corroborate"]) == 2
    assert "bad response" in capsys.readouterr().err and manifest.read_bytes() == before


def cache_fixture(tmp_path, monkeypatch, recorded_newline, current_newline, policy=None):
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    directory = tmp_path / "data/binance/4h"
    directory.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=12, freq="4h", name="timestamp")
    frame = pd.DataFrame({"open": 10., "high": 12., "low": 9., "close": 11., "volume": 100.}, index=index)
    payload = frame.to_csv(lineterminator="\n").encode()
    path = directory / "BTC_USDT.csv"
    path.write_bytes(payload.replace(b"\n", current_newline))
    record = {"provider": "binance", "timeframe": "4h", "market_type": "spot", "symbol": "BTC-USDT",
              "file": path.name, "requested_start": "2024-01-01", "requested_end": "2024-01-02",
              "first": index.min().isoformat(), "last": index.max().isoformat(),
              "sha256": hashlib.sha256(payload.replace(b"\n", recorded_newline)).hexdigest(),
              "coverage": download.validate_coverage(frame, "2024-01-01", "2024-01-02", "4h")}
    if policy is not None:
        record["hash_policy"] = policy
    manifest = {"schema_version": "binance-cache/v2", "provider": "binance", "market_type": "spot",
                "timeframe": "4h", "failures": [], "symbols": {"BTC/USDT": record}}
    (directory / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest, frame


@pytest.mark.parametrize("recorded_newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("current_newline", [b"\n", b"\r\n"])
def test_legacy_cache_accepts_only_newline_equivalence(tmp_path, monkeypatch, recorded_newline, current_newline):
    path, manifest, _ = cache_fixture(tmp_path, monkeypatch, recorded_newline, current_newline)
    assert matrix.verify_cache(["BTC/USDT"], "4h", start="2024-01-01", end="2024-01-02") == manifest
    path.write_bytes(path.read_bytes().replace(b"11.0", b"11.5"))
    with pytest.raises(ValueError, match="hash mismatch"):
        matrix.verify_cache(["BTC/USDT"], "4h")


@pytest.mark.parametrize("policy", ["sha256-lf/v1", "unsupported-policy"])
def test_declared_cache_policy_is_enforced_without_legacy_fallback(tmp_path, monkeypatch, policy):
    _, manifest, _ = cache_fixture(tmp_path, monkeypatch, b"\n", b"\r\n", policy=policy)
    if policy == "sha256-lf/v1":
        assert matrix.verify_cache(["BTC/USDT"], "4h") == manifest
    else:
        with pytest.raises(ValueError, match="hash mismatch"):
            matrix.verify_cache(["BTC/USDT"], "4h")


@pytest.mark.parametrize("digest", [None, [], "not-a-digest"])
def test_malformed_cache_digests_are_rejected_as_hash_mismatches(tmp_path, monkeypatch, digest):
    path, manifest, _ = cache_fixture(tmp_path, monkeypatch, b"\n", b"\n")
    manifest["symbols"]["BTC/USDT"]["sha256"] = digest
    (path.parent / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        matrix.verify_cache(["BTC/USDT"], "4h")


@pytest.mark.parametrize("recorded_newline,current_newline", [(b"\n", b"\r\n"), (b"\r\n", b"\n")])
def test_incremental_refresh_accepts_legacy_newlines_and_writes_an_explicit_lf_identity(
        tmp_path, monkeypatch, recorded_newline, current_newline):
    path, _, frame = cache_fixture(tmp_path, monkeypatch, recorded_newline, current_newline)
    fetch = Mock(return_value=frame.iloc[-1:])
    monkeypatch.setattr(download, "fetch_ohlcv", fetch)
    assert download.main(["--symbols", "BTC/USDT", "--timeframe", "4h", "--start", "2024-01-01",
                          "--end", "2024-01-02", "--out-root", str(tmp_path / "data/binance")]) == 0
    assert fetch.call_args.args[3] == "2024-01-02"  # trusted old rows advance the incremental cursor
    manifest = json.loads((path.parent / "_manifest.json").read_text())
    record = manifest["symbols"]["BTC/USDT"]
    assert record["hash_policy"] == "sha256-lf/v1"
    assert record["sha256"] == hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert matrix.verify_cache(["BTC/USDT"], "4h") == manifest
