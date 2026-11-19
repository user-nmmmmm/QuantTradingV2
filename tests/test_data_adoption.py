"""Approved prefix adoption and the research horizon have separate, enforced identities."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import subprocess

import pytest

from cli import data as local_data
from scripts import data_adoption as adoption
from scripts import data_adoption_review as review
from scripts import data_inputs as inputs


HEADER = b"timestamp,open,high,low,close,volume\n"


def csv(days):
    return HEADER + b"".join(f"{day},10,12,9,11,100\n".encode() for day in days)


@pytest.fixture
def evidence(tmp_path):
    sources = {"BTC_USDT.csv": csv([f"2026-08-{day}" for day in (25, 26, 27, 28)]),
               "ALT_USDT.csv": csv([f"2026-08-{day}" for day in (25, 26, 27)]),
               "MATIC_USDT.csv": csv(["2024-09-08", "2024-09-09", "2024-09-10", "2025-01-01"])}
    for name, data in sources.items():
        (tmp_path / name).write_bytes(data)
    cache = {"generated_at": "2026-09-20T00:00:00Z", "symbols": {"BTC/USDT": {
        "file": "BTC_USDT.csv", "rows": 4, "sha256": review.digest(sources["BTC_USDT.csv"]),
        "market_type": "spot", "timeframe": "1d"}}}
    cache_bytes = adoption.render(cache)
    (tmp_path / inputs.CACHE_MANIFEST).write_bytes(cache_bytes)
    original = inputs.render(inputs.build(tmp_path)).encode()
    files = {}
    for name, data in sources.items():
        bars = inputs._bars_from_bytes(data, name)
        remote = deepcopy(bars)
        if name == "ALT_USDT.csv":
            remote[max(remote)] = (*remote[max(remote)][:4], remote[max(remote)][4] * 2)
        elif name == "MATIC_USDT.csv":
            remote.pop("2025-01-01")
        facts = inputs._describe_bytes(data, name)
        block = {**inputs.compare_bars(bars, remote), "comparison_policy": inputs.COMPARISON_POLICY,
                 "for_sha256": facts["sha256"], "source": "saved public test source", "checked_at": "2026-10-15T09:10:20Z"}
        item = {"snapshot": facts, "corroboration": block, "remote_first": min(remote), "remote_last": max(remote)}
        if name == "ALT_USDT.csv":
            item["tail_witness"] = {"date": max(bars), "local": list(map(str, bars[max(bars)])),
                                    "remote": list(map(str, remote[max(remote)]))}
        elif name == "MATIC_USDT.csv":
            item.update(unmatched_dates=["2025-01-01"], market_lifecycle={"market": "binance-spot",
                "trading_stopped_at": "2024-09-10T03:00:00Z", "source_url": "https://www.binance.com/public-test"})
        files[name] = item
    document = {"schema": "data-corroboration-review/v1", "source_commit": "a" * 40,
                "comparison_policy": inputs.COMPARISON_POLICY, "input_manifest_sha256": review.digest(original),
                "files": files, "status_counts": dict(Counter(item["corroboration"]["status"] for item in files.values()))}
    review_bytes = adoption.render(document)
    draft, _ = review.prepare(document, original, sources)
    draft["review_sha256"] = review.digest(review_bytes)
    seed = {"base_commit": "b" * 40, "applied_commit": "c" * 40,
            "applied_at": "2026-10-16T06:00:00Z", "approval_recorded_at": "2026-10-16T06:00:00Z"}
    return review_bytes, adoption.render(draft), original, sources, seed, cache_bytes


def prepared(evidence):
    return adoption.prepare(*evidence[:4], **evidence[4])


def test_approved_prefixes_separate_matching_delisting_day_from_unmatched_rows(evidence):
    candidates, manifest_raw, receipt_raw, _ = prepared(evidence)
    receipt = json.loads(receipt_raw)
    assert candidates["BTC_USDT.csv"] == evidence[3]["BTC_USDT.csv"]
    assert candidates["ALT_USDT.csv"] == csv(["2026-08-25", "2026-08-26"])
    assert candidates["MATIC_USDT.csv"] == csv(["2024-09-08", "2024-09-09"])
    matic = receipt["files"]["MATIC_USDT.csv"]
    assert matic["removed_unmatched_rows"] == matic["removed_boundary_rows"] == 1
    assert matic["research_end_exclusive"] == "2024-09-10"
    assert json.loads(manifest_raw)["files"]["BTC_USDT.csv"]["provenance"]["status"] == "download_manifest"
    assert receipt["files"]["ALT_USDT.csv"]["provenance_status"] == "unverified"
    assert all(item["point_in_time"] == "unknown" for item in receipt["files"].values())
    assert receipt["A5_executed"] is receipt["release_tag_created"] is False


def test_new_evidence_binds_adopted_bytes_without_claiming_a_new_download(evidence, tmp_path):
    candidates, manifest_raw, receipt_raw, policy_raw = prepared(evidence)
    for name, data in candidates.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / inputs.CACHE_MANIFEST).write_bytes(evidence[5])
    manifest = json.loads(manifest_raw)
    assert inputs.render(inputs.build(tmp_path, previous=manifest)).encode() == manifest_raw
    for name, item in manifest["files"].items():
        block = item["corroboration"]
        assert block["for_sha256"] == review.digest(candidates[name])
        assert block["status"] == "match" and block["rows_matching"] == item["rows"]
        assert block["comparison_policy"] == inputs.COMPARISON_POLICY
        assert block["checked_at"] == evidence[4]["applied_at"]
        assert block["source_checked_at"] == "2026-10-15T09:10:20Z"
        assert block["basis"] == "retained_prefix_of_saved_v2_review"
    assert json.loads(policy_raw)["receipt_sha256"] == review.digest(receipt_raw)


def test_downloaded_tail_is_retained_but_excluded_from_the_research_window(evidence):
    _, _, receipt_raw, policy_raw = prepared(evidence)
    receipt, policy = json.loads(receipt_raw), json.loads(policy_raw)
    btc = receipt["files"]["BTC_USDT.csv"]
    assert btc["adopted"]["last"] == "2026-08-28" and btc["research_rows"] == 2
    assert btc["retained_rows_outside_research"] == 2 and btc["research_end_inclusive"] == "2026-08-26"
    assert all(item["research_end_inclusive"] == "2026-08-26" for name, item in policy["files"].items()
               if name != "MATIC_USDT.csv")


@pytest.mark.parametrize("mutation", ["approved_draft", "edited_input", "stale_manifest", "invalid_time", "short_commit", "naive_time"])
def test_adoption_requires_the_exact_reviewed_inputs_and_explicit_metadata(evidence, mutation):
    review_bytes, draft_bytes, manifest, sources, seed, _ = deepcopy(evidence)
    if mutation == "approved_draft":
        value = json.loads(draft_bytes)
        value["adopted"] = True
        draft_bytes = adoption.render(value)
    elif mutation == "edited_input":
        sources["ALT_USDT.csv"] += b"2026-08-28,10,12,9,11,100\n"
    elif mutation == "stale_manifest":
        manifest += b" "
    elif mutation == "invalid_time":
        seed["applied_at"] = "invalid"
    elif mutation == "short_commit":
        seed["applied_commit"] = "HEAD"
    else:
        seed["applied_at"] = "2026-10-16T06:00:00"
    with pytest.raises(ValueError):
        adoption.prepare(review_bytes, draft_bytes, manifest, sources, **seed)


def test_research_cutoff_must_be_an_actual_day_in_each_candidate(evidence, monkeypatch):
    monkeypatch.setattr(adoption, "COMMON_END", "2026-08-29")
    with pytest.raises(ValueError, match="research cutoff must be covered"):
        prepared(evidence)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def installed(tmp_path, evidence):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    folder = root / "data/binance/1d"
    folder.mkdir(parents=True)
    for name, data in evidence[3].items():
        (folder / name).write_bytes(data)
    (folder / inputs.FROZEN_MANIFEST).write_bytes(evidence[2])
    (folder / inputs.CACHE_MANIFEST).write_bytes(evidence[5])
    git(root, "add", "data")
    git(root, "commit", "-qm", "source")
    seed = {**evidence[4], "base_commit": git(root, "rev-parse", "HEAD")}
    document = json.loads(evidence[0])
    document["source_commit"] = seed["base_commit"]
    review_bytes = adoption.render(document)
    draft, _ = review.prepare(document, evidence[2], evidence[3])
    draft["review_sha256"] = review.digest(review_bytes)
    draft_bytes = adoption.render(draft)
    args = (review_bytes, draft_bytes, evidence[2], evidence[3])
    candidates, manifest, _, _ = adoption.prepare(*args, **seed)
    for name, data in candidates.items():
        (folder / name).write_bytes(data)
    (folder / inputs.FROZEN_MANIFEST).write_bytes(manifest)
    git(root, "add", "data")
    git(root, "commit", "-qm", "adopted data")
    seed["applied_commit"] = git(root, "rev-parse", "HEAD")
    _, _, receipt, policy = adoption.prepare(*args, **seed)
    (root / adoption.RECEIPT).parent.mkdir(parents=True)
    (root / adoption.RECEIPT).write_bytes(receipt)
    (root / review.REVIEW).write_bytes(review_bytes)
    (root / review.DRAFT).write_bytes(draft_bytes)
    (folder / adoption.POLICY_FILE).write_bytes(policy)
    return root


def test_checker_binds_real_git_data_commit_and_is_read_only(installed):
    before = {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert adoption.check(installed) == 3
    assert all((installed / name).read_bytes() == data for name, data in before.items())


@pytest.mark.parametrize("mutation", ["csv", "manifest", "policy", "approval", "A5", "commit", "cache", "extra", "draft"])
def test_checker_rejects_tampering_and_tracked_input_drift(installed, mutation):
    folder = installed / "data/binance/1d"
    if mutation == "csv":
        path = folder / "ALT_USDT.csv"
        path.write_bytes(path.read_bytes().replace(b",100", b",101"))
    elif mutation == "cache":
        path = folder / inputs.CACHE_MANIFEST
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "extra":
        (folder / "EXTRA_USDT.csv").write_bytes(csv(["2026-08-26"]))
        git(installed, "add", "data")
    else:
        path = (folder / inputs.FROZEN_MANIFEST if mutation == "manifest" else folder / adoption.POLICY_FILE
                if mutation == "policy" else installed / review.DRAFT if mutation == "draft" else installed / adoption.RECEIPT)
        value = json.loads(path.read_bytes())
        if mutation == "manifest":
            value["files"]["ALT_USDT.csv"]["provenance"]["status"] = "download_manifest"
        elif mutation == "policy":
            value["files"]["BTC_USDT.csv"]["research_end_inclusive"] = "2026-08-28"
        elif mutation == "approval":
            value["approved_by"] = "someone_else"
        elif mutation == "A5":
            value["A5_executed"] = True
        elif mutation == "draft":
            value["adopted"] = True
        else:
            value["applied_commit"] = value["base_commit"]
        path.write_bytes(adoption.render(value))
    with pytest.raises(ValueError):
        adoption.check(installed)


def test_checker_accepts_only_newline_equivalence(installed):
    for path in installed.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert adoption.check(installed) == 3


def test_local_loader_enforces_the_window_and_binds_the_same_bytes(installed):
    folder = installed / "data/binance/1d"
    frame = local_data.get_data("BTC/USDT", "2026-08-25", "2026-08-26", source="local", data_dir=str(folder))
    assert frame.index.max().date().isoformat() == "2026-08-26" and len(frame) == 2
    assert frame.attrs["adopted_research_window"]["research_end_inclusive"] == "2026-08-26"
    with pytest.raises(ValueError, match="exceeds adopted research end"):
        local_data.get_data("BTC/USDT", "2026-08-25", "2026-08-27", source="local", data_dir=str(folder))
    with pytest.raises(ValueError, match="2024-09-09"):
        local_data.get_data("MATIC/USDT", "2024-09-08", "2026-08-26", source="local", data_dir=str(folder))
    path = folder / "BTC_USDT.csv"
    path.write_bytes(path.read_bytes().replace(b",100", b",101"))
    with pytest.raises(ValueError, match="bytes differ"):
        local_data.get_data("BTC/USDT", "2026-08-25", "2026-08-26", source="local", data_dir=str(folder))


def test_research_loader_accepts_crlf_but_rejects_unknown_files_and_timeframe(installed):
    folder = installed / "data/binance/1d"
    path = folder / "BTC_USDT.csv"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert len(local_data._load_local_ohlcv("BTC/USDT", "2026-08-25", "2026-08-26", str(folder))) == 2
    with pytest.raises(ValueError, match="timeframe"):
        local_data._load_local_ohlcv("BTC/USDT", "2026-08-25", "2026-08-26", str(folder), timeframe="1h")
    (folder / "EXTRA_USDT.csv").write_bytes(csv(["2026-08-26"]))
    with pytest.raises(ValueError, match="not registered"):
        local_data._load_local_ohlcv("EXTRA/USDT", "2026-08-25", "2026-08-26", str(folder))


def test_version_store_cannot_bypass_the_adopted_policy(installed):
    folder = installed / "data/binance/1d"
    policy = {"mode": "retrospective", "store_path": str(installed / "versions")}
    frame = local_data.get_data("BTC/USDT", "2026-08-25", "2026-08-26", source="local", data_dir=str(folder), temporal_policy=policy)
    assert len(frame) == 2 and frame.attrs["adopted_research_window"]["research_end_inclusive"] == "2026-08-26"
    assert frame.attrs["temporal_raw_file_snapshot"]
    with pytest.raises(ValueError, match="exceeds adopted research end"):
        local_data.get_data("BTC/USDT", "2026-08-25", "2026-08-27", source="local", data_dir=str(folder), temporal_policy=policy)


def test_committed_adoption_has_exact_horizons_and_retains_download_history():
    assert adoption.check() == 30
    receipt = json.loads((adoption.ROOT / adoption.RECEIPT).read_bytes())
    files = receipt["files"]
    assert sum(item["adopted"]["rows"] for item in files.values()) == 67266
    assert sum(item["research_rows"] for item in files.values()) == 67218
    assert Counter(item["research_end_inclusive"] for item in files.values()) == {"2026-08-26": 29, "2024-09-09": 1}
    assert files["MATIC_USDT.csv"]["adopted"]["rows"] == 1964
    assert files["MATIC_USDT.csv"]["removed_unmatched_rows"] == 83
    assert files["MATIC_USDT.csv"]["removed_boundary_rows"] == 1
    for name in ("BTC_USDT.csv", "ETH_USDT.csv"):
        assert files[name]["source"] == files[name]["adopted"]
        assert files[name]["retained_rows_outside_research"] == 24
    assert json.loads((adoption.ROOT / review.DRAFT).read_bytes())["adopted"] is False
