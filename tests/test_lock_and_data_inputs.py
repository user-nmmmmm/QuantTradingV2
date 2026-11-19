"""D2/D3: the runtime lock is reproducible and consistent, and tracked market data is pinned by a manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import data_inputs, regenerate_lock

ROOT = Path(__file__).resolve().parents[1]
CRLF, LF = bytes([13, 10]), bytes([10])


# ---- D3: lock -------------------------------------------------------------------------------------------------

def test_the_committed_lock_is_canonical_and_covers_the_direct_pins():
    assert regenerate_lock.check() == []


def test_render_lock_sorts_by_normalized_name_and_drops_installer_tooling():
    text = regenerate_lock.render_lock([("pip", "25.0"), ("Zeta_pkg", "1"), ("alpha", "2"), ("setuptools", "70"),
                                         ("Beta-pkg", "3")])
    assert text == regenerate_lock.HEADER + "alpha==2\nBeta-pkg==3\nZeta_pkg==1\n"
    assert regenerate_lock.render_lock(regenerate_lock.parse_pins(text)) == text     # fixed point


def test_render_lock_rejects_two_versions_of_one_distribution():
    with pytest.raises(ValueError, match="two versions"):
        regenerate_lock.render_lock([("Foo_Bar", "1"), ("foo-bar", "2")])


def test_parse_pins_rejects_ranges():
    with pytest.raises(ValueError, match="exact pin"):
        regenerate_lock.parse_pins("requests>=2\n")


def _lock_files(tmp_path: Path, lock_pins: str, direct: str, *, digest=None):
    lock = tmp_path / "requirements.lock.txt"
    text = regenerate_lock.render_lock(regenerate_lock.parse_pins(lock_pins))
    lock.write_bytes(text.encode("utf-8"))
    sha = tmp_path / "requirements.lock.sha256"
    sha.write_text(digest or regenerate_lock.digest_line(text), encoding="ascii")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(direct, encoding="utf-8")
    return lock, sha, requirements


def test_check_flags_a_missing_or_different_direct_pin_and_a_stale_digest(tmp_path):
    lock, sha, direct = _lock_files(tmp_path, "a==1\nb==2\n", "a==1\n")
    assert regenerate_lock.check(lock, sha, direct) == []
    direct.write_text("a==1\nb==3\nc==1\n", encoding="utf-8")
    problems = regenerate_lock.check(lock, sha, direct)
    assert any("b: requirements.txt pins 3, the lock has 2" in p for p in problems)
    assert any("c==1" in p and "missing from the lock" in p for p in problems)
    lock.write_bytes(lock.read_bytes() + b"x==1\n")
    assert any("does not match" in p for p in regenerate_lock.check(lock, sha, direct))


def test_check_flags_a_hand_edited_lock_that_is_not_canonical(tmp_path):
    lock, sha, direct = _lock_files(tmp_path, "a==1\n", "a==1\n")
    unsorted = "b==2\na==1\n"
    lock.write_bytes(unsorted.encode())
    sha.write_text(hashlib.sha256(unsorted.encode()).hexdigest() + "  " + lock.name + "\n", encoding="ascii")
    assert any("canonical form" in p for p in regenerate_lock.check(lock, sha, direct))


# ---- D2: data inputs ------------------------------------------------------------------------------------------

def _csv(path: Path, rows, *, newline=LF):
    lines = ["timestamp,open,high,low,close,volume"] + [f"{day},1,2,0.5,1.5,10" for day in rows]
    path.write_bytes(newline.join(line.encode() for line in lines) + newline)


@pytest.fixture
def data_dir(tmp_path):
    folder = tmp_path / "1d"
    folder.mkdir()
    _csv(folder / "BTC_USDT.csv", ["2020-01-01", "2020-01-02", "2020-01-03"])
    _csv(folder / "ETH_USDT.csv", ["2020-01-01", "2020-01-02"])
    return folder


def _register(folder: Path):
    (folder / data_inputs.FROZEN_MANIFEST).write_text(data_inputs.render(data_inputs.build(folder)), encoding="utf-8")


def test_describe_counts_data_rows_and_range(data_dir):
    facts = data_inputs.describe(data_dir / "BTC_USDT.csv")
    assert (facts["rows"], facts["first"], facts["last"]) == (3, "2020-01-01", "2020-01-03")


def test_the_digest_is_the_same_for_crlf_and_lf_checkouts(data_dir):
    lf = data_inputs.describe(data_dir / "BTC_USDT.csv")["sha256"]
    _csv(data_dir / "BTC_USDT.csv", ["2020-01-01", "2020-01-02", "2020-01-03"], newline=CRLF)
    assert data_inputs.describe(data_dir / "BTC_USDT.csv")["sha256"] == lf


def test_verify_passes_after_build_and_fails_on_replaced_or_missing_files(data_dir):
    _register(data_dir)
    assert data_inputs.verify(data_dir) == []
    _csv(data_dir / "ETH_USDT.csv", ["2020-01-01", "2020-01-02"])
    assert data_inputs.verify(data_dir) == []                                    # same content, rewritten
    (data_dir / "ETH_USDT.csv").write_bytes((data_dir / "ETH_USDT.csv").read_bytes().replace(b"1.5", b"1.6"))
    problems = data_inputs.verify(data_dir)
    assert len(problems) == 1 and problems[0].startswith("ETH_USDT.csv: content changed")
    (data_dir / "BTC_USDT.csv").unlink()
    assert "BTC_USDT.csv: registered input is missing" in data_inputs.verify(data_dir)


def test_verify_flags_a_row_removed_even_when_nothing_else_changed(data_dir):
    _register(data_dir)
    _csv(data_dir / "BTC_USDT.csv", ["2020-01-01", "2020-01-02"])
    problems = data_inputs.verify(data_dir)
    assert any("rows changed (3 -> 2)" in p for p in problems) and any("content changed" in p for p in problems)


def test_unregistered_tracked_inputs_fail_but_untracked_cache_files_only_in_strict_mode(data_dir):
    _register(data_dir)
    _csv(data_dir / "SOL_USDT.csv", ["2020-01-01"])
    assert data_inputs.verify(data_dir, tracked=["BTC_USDT.csv", "ETH_USDT.csv"]) == []
    assert data_inputs.verify(data_dir, tracked=["BTC_USDT.csv", "SOL_USDT.csv"]) == [
        "SOL_USDT.csv: input is not registered in _frozen_inputs.json"]
    assert data_inputs.verify(data_dir, strict_dir=True) == ["SOL_USDT.csv: input is not registered in _frozen_inputs.json"]


def test_a_missing_manifest_is_an_error(data_dir):
    assert data_inputs.verify(data_dir) == ["_frozen_inputs.json is missing"]


def test_provenance_is_download_manifest_only_when_the_cache_manifest_recorded_the_same_bytes(data_dir):
    raw = (data_dir / "BTC_USDT.csv").read_bytes()
    crlf_digest = hashlib.sha256(raw.replace(LF, CRLF)).hexdigest()           # a Windows download hashes CRLF bytes
    cache = {"generated_at": "2026-09-20T00:00:00+00:00", "symbols": {
        "BTC/USDT": {"file": "BTC_USDT.csv", "rows": 3, "sha256": crlf_digest, "market_type": "spot", "timeframe": "1d"},
        "ETH/USDT": {"file": "ETH_USDT.csv", "rows": 2, "sha256": "0" * 64}}}
    (data_dir / data_inputs.CACHE_MANIFEST).write_text(json.dumps(cache), encoding="utf-8")
    files = data_inputs.build(data_dir)["files"]
    assert files["BTC_USDT.csv"]["provenance"]["status"] == "download_manifest"
    assert files["ETH_USDT.csv"]["provenance"]["status"] == "unverified"        # recorded hash differs
    assert all(entry["point_in_time"] == "unknown" for entry in files.values())


def test_the_committed_manifest_covers_every_tracked_csv_and_matches_the_files():
    tracked = data_inputs._tracked_csvs(data_inputs.DATA_DIR)
    if tracked is None:
        pytest.skip("not a git checkout")
    assert data_inputs.verify(tracked=tracked) == []
    registered = json.loads((data_inputs.DATA_DIR / data_inputs.FROZEN_MANIFEST).read_text(encoding="utf-8"))["files"]
    assert set(registered) == set(tracked)
    assert data_inputs.main(["build", "--check"]) == 0


# ---- D2 tail: independent corroboration against Binance's public klines -----------------------------------------

from datetime import datetime, timezone                                  # noqa: E402
from decimal import Decimal                                              # noqa: E402


def _bars(days, volume="10", close="1.5"):
    return {day: (Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal(close), Decimal(volume)) for day in days}


def test_compare_bars_ignores_decimal_formatting_and_reports_a_clean_match():
    local = {"2020-01-01": (Decimal("51.43"), Decimal("59.0"), Decimal("42.747"), Decimal("42.999"), Decimal("98648.87"))}
    remote = {"2020-01-01": tuple(Decimal(v) for v in ("51.43000000", "59.00000000", "42.74700000", "42.99900000", "98648.87000000"))}
    result = data_inputs.compare_bars(local, remote)
    assert result["status"] == "match" and result["rows_matching"] == 1 and result["difference_count"] == 0


def test_a_difference_only_on_the_last_local_day_is_a_partial_final_bar_not_a_conflict():
    days = ["2026-08-25", "2026-08-26", "2026-08-27"]
    local = {**_bars(days), "2026-08-27": _bars(["x"], volume="3")["x"]}              # captured before the day closed
    result = data_inputs.compare_bars(local, _bars(days, volume="10"))
    assert result["status"] == "match_except_final_bar" and result["difference_count"] == 1
    assert result["examples"]["differences"][0] == {"date": "2026-08-27", "field": "volume", "local": "3", "remote": "10"}


@pytest.mark.parametrize("local_days, remote_days, remote_override, what", [
    (["a1", "a2", "a3"], ["a1", "a2", "a3"], {"a2": _bars(["x"], close="9")["x"]}, "a difference on an earlier day"),
    (["a1", "a2", "a3"], ["a1", "a2"], {}, "local rows the source does not have"),
    (["a1", "a3"], ["a1", "a2", "a3"], {}, "source rows missing locally"),
])
def test_other_differences_are_mismatches(local_days, remote_days, remote_override, what):
    local = _bars(local_days)
    remote = {**_bars(remote_days), **remote_override}
    result = data_inputs.compare_bars(local, remote)
    assert result["status"] == "mismatch", what


def test_remote_rows_outside_the_local_range_are_not_counted_against_the_file():
    local = _bars(["2020-01-02", "2020-01-03"])
    remote = _bars(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"])
    assert data_inputs.compare_bars(local, remote)["status"] == "match"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _kline(day_ms, close="1.5"):
    return [day_ms, "1", "2", "0.5", close, "10", day_ms + 86_399_999, "0", 1, "0", "0", "0"]


def test_fetch_pages_through_the_range_and_stops_when_the_source_is_empty(monkeypatch):
    monkeypatch.setattr(data_inputs.time, "sleep", lambda s: None)
    day0 = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    pages = [[_kline(day0), _kline(day0 + 86_400_000)], [_kline(day0 + 2 * 86_400_000)]]
    seen = []

    def opener(url, timeout):
        seen.append(url)
        return _Response(pages.pop(0) if pages else [])
    bars = data_inputs.fetch_binance_daily("BTCUSDT", "2020-01-01", "2020-01-03", opener=opener)
    assert sorted(bars) == ["2020-01-01", "2020-01-02", "2020-01-03"] and bars["2020-01-02"][3] == Decimal("1.5")
    assert "symbol=BTCUSDT" in seen[0] and "startTime=%d" % day0 in seen[0] and len(seen) == 2


def test_an_unknown_symbol_is_reported_as_unavailable_not_as_an_error(monkeypatch):
    import io
    import urllib.error
    monkeypatch.setattr(data_inputs.time, "sleep", lambda s: None)

    def opener(url, timeout):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b'{"code":-1121,"msg":"Invalid symbol."}'))
    assert data_inputs.fetch_binance_daily("NOPEUSDT", "2020-01-01", "2020-01-02", opener=opener) is None


def test_corroboration_is_bound_to_the_file_digest_and_dropped_when_the_file_changes(data_dir):
    _register(data_dir)
    manifest = data_inputs.corroborate(
        data_dir, fetch=lambda symbol, first, last: data_inputs.read_bars(data_dir / (symbol[:3] + "_USDT.csv")),
        now=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert {e["corroboration"]["status"] for e in manifest["files"].values()} == {"match"}
    (data_dir / data_inputs.FROZEN_MANIFEST).write_text(data_inputs.render(manifest), encoding="utf-8")
    assert data_inputs.verify(data_dir) == []
    kept = data_inputs.build(data_dir, previous=manifest)["files"]
    assert all("corroboration" in entry for entry in kept.values())          # unchanged files keep their evidence

    _csv(data_dir / "ETH_USDT.csv", ["2020-01-01", "2020-01-02", "2020-01-09"])
    rebuilt = data_inputs.build(data_dir, previous=manifest)["files"]
    assert "corroboration" in rebuilt["BTC_USDT.csv"] and "corroboration" not in rebuilt["ETH_USDT.csv"]


def test_verify_rejects_evidence_that_belongs_to_another_version_or_has_an_unknown_status(data_dir):
    _register(data_dir)
    path = data_dir / data_inputs.FROZEN_MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"]["BTC_USDT.csv"]["corroboration"] = {"status": "match", "for_sha256": "0" * 64}
    manifest["files"]["ETH_USDT.csv"]["corroboration"] = {"status": "great", "for_sha256": manifest["files"]["ETH_USDT.csv"]["sha256"]}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = data_inputs.verify(data_dir)
    assert any("BTC_USDT.csv: corroboration belongs to a different version" in p for p in problems)
    assert any("ETH_USDT.csv: corroboration status must be one of" in p for p in problems)
