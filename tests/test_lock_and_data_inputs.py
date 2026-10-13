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
