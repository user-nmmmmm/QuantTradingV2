"""Adoption drafts bind original bytes without approving, replacing or registering data."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess

import pytest

from scripts import data_adoption_review as review
from scripts import data_inputs as inputs


NAME = "TEST_USDT.csv"
HEADER = b"timestamp,open,high,low,close,volume\n"
CSV = HEADER + b"2024-09-09,10,12,9,11,100\n2024-09-10,11,13,10,12,150\n"
EVENT = {"market": "binance-spot", "trading_stopped_at": "2024-09-10T03:00:00Z",
         "source_url": "https://www.binance.com/en/support/announcement/detail/public-example"}


def evidence(data=CSV, remote=None):
    local = inputs._bars_from_bytes(data, NAME)
    remote = local if remote is None else remote
    block = inputs.compare_bars(local, remote)
    facts = inputs._describe_bytes(data, NAME)
    block.update(comparison_policy=inputs.COMPARISON_POLICY, for_sha256=facts["sha256"])
    item = {"snapshot": facts, "corroboration": block,
            "remote_first": min(remote) if remote else None, "remote_last": max(remote) if remote else None}
    if block["status"] == "match_except_final_bar":
        item["tail_witness"] = {"date": max(local), "local": list(map(str, local[max(local)])),
                                "remote": list(map(str, remote[max(local)]))}
    return item


def bundle(item=None, data=CSV):
    item = evidence(data) if item is None else item
    manifest = {"files": {NAME: {**item["snapshot"], "symbol": "TEST/USDT",
                                 "provenance": {"status": "unverified"}}}}
    raw = json.dumps(manifest).encode()
    document = {"schema": "data-corroboration-review/v1", "source_commit": "a" * 40,
                "comparison_policy": inputs.COMPARISON_POLICY, "input_manifest_sha256": review.digest(raw),
                "files": {NAME: item}, "status_counts": {item["corroboration"]["status"]: 1}}
    return document, raw, {NAME: data}


def partial():
    remote = inputs._bars_from_bytes(CSV.replace(b",13,10,12,150", b",14,9,13,200"), NAME)
    return evidence(remote=remote)


def delisted():
    data = CSV + b"2025-01-01,12,14,11,13,200\n2025-01-02,13,15,12,14,250\n"
    item = evidence(data, inputs._bars_from_bytes(CSV, NAME))
    item.update(unmatched_dates=["2025-01-01", "2025-01-02"], market_lifecycle=deepcopy(EVENT))
    return data, item


def test_full_match_retains_bytes_and_never_upgrades_provenance_or_approves():
    draft, candidates = review.prepare(*bundle())
    assert candidates == {NAME: CSV}
    assert draft["files"][NAME]["proposal"]["action"] == "retain_bytes"
    assert draft["files"][NAME]["provenance_status"] == "unverified"
    assert draft["files"][NAME]["point_in_time"] == "unknown"
    assert not draft["adopted"] and not draft["A5_executed"]
    assert draft["approved_by"] is draft["approved_at"] is draft["applied_commit"] is None
    assert all(value is None for value in draft["decisions_required"].values())


def test_partial_tail_proposes_prefix_without_replacing_it_with_current_remote_values():
    proposed, candidate = review.proposal(NAME, CSV, partial())
    assert proposed["action"] == "trim_final_bar"
    assert proposed["removed_boundary_rows"] == 1 and proposed["removed_unmatched_rows"] == 0
    assert candidate == HEADER + b"2024-09-09,10,12,9,11,100\n"
    assert proposed["candidate"] == inputs._describe_bytes(candidate, NAME)


@pytest.mark.parametrize("field,value", [("date", "2024-09-09"), ("local", ["0"] * 5),
                                         ("remote", ["12", "14", "9", "13", "200"]),
                                         ("remote", ["11", "14", "9", "13", "-1"]),
                                         ("remote", ["NaN"] * 5), ("remote", ["11"] * 4)])
def test_invalid_tail_witness_is_rejected(field, value):
    item = partial()
    item["tail_witness"][field] = value
    with pytest.raises(ValueError, match="tail|partial"):
        review.proposal(NAME, CSV, item)


@pytest.mark.parametrize("key,value", [("comparison_policy", "legacy"), ("for_sha256", "0" * 64),
                                       ("rows_local", 3), ("rows_matching", 100), ("rows_remote", -1),
                                       ("only_local_count", True), ("status", "approved")])
def test_stale_or_inconsistent_corroboration_is_rejected(key, value):
    item = evidence()
    item["corroboration"][key] = value
    with pytest.raises(ValueError):
        review.proposal(NAME, CSV, item)


def test_delisted_prefix_distinguishes_unmatched_rows_from_matching_terminal_day():
    data, item = delisted()
    proposed, candidate = review.proposal(NAME, data, item)
    assert proposed["action"] == "trim_unmatched_and_delisting_day"
    assert proposed["removed_unmatched_rows"] == 2 and proposed["removed_boundary_rows"] == 1
    assert proposed["removed_rows"] == 3
    assert candidate == HEADER + b"2024-09-09,10,12,9,11,100\n"


@pytest.mark.parametrize("mutation", ["missing_date", "nonprefix", "difference", "no_event", "wrong_start"])
def test_unexplained_mismatch_is_quarantined_instead_of_silently_trimmed(mutation):
    data, item = delisted()
    if mutation == "missing_date":
        item["unmatched_dates"].pop()
    elif mutation == "nonprefix":
        item["corroboration"].update(rows_remote=1, rows_matching=1)
    elif mutation == "difference":
        item["corroboration"]["difference_count"] = 1
    elif mutation == "wrong_start":
        item["remote_first"] = "2024-09-10"
    else:
        item.pop("market_lifecycle")
    proposed, candidate = review.proposal(NAME, data, item)
    assert proposed["action"] == "quarantine_pending_review" and candidate is None


@pytest.mark.parametrize("stamp", ["2024-09-11T03:00:00Z", "2024-09-10T03:00:00"])
def test_lifecycle_boundary_needs_exact_utc_day_and_explicit_timezone(stamp):
    data, item = delisted()
    item["market_lifecycle"]["trading_stopped_at"] = stamp
    with pytest.raises(ValueError, match="timezone|delisting day"):
        review.proposal(NAME, data, item)


def test_empty_response_cannot_become_an_adoption_candidate():
    proposed, candidate = review.proposal(NAME, CSV, evidence(remote={}))
    assert proposed["action"] == "quarantine_pending_review" and candidate is None


def test_crlf_and_lf_make_the_same_candidate_and_draft():
    args = bundle(partial())
    expected = review.prepare(*args)
    assert review.prepare(args[0], args[1].replace(b"\n", b"\r\n"),
                          {NAME: CSV.replace(b"\n", b"\r\n")}) == expected


@pytest.mark.parametrize("mutation", ["input", "manifest", "file_set", "counts", "policy"])
def test_review_must_cover_exact_digest_bound_frozen_inputs(mutation):
    document, manifest, sources = bundle()
    if mutation == "input":
        sources[NAME] = CSV.replace(b",100", b",101")
    elif mutation == "manifest":
        manifest += b" "
    elif mutation == "file_set":
        sources["EXTRA_USDT.csv"] = CSV
    elif mutation == "counts":
        document["status_counts"] = {"match": 2}
    else:
        document["comparison_policy"] = "v1"
    with pytest.raises(ValueError):
        review.prepare(document, manifest, sources)


def cli_fixture(tmp_path, monkeypatch, item=None):
    document, manifest, sources = bundle(item)
    monkeypatch.setattr(review, "ROOT", tmp_path)
    monkeypatch.setattr(review, "source_snapshot", lambda root, doc: (manifest, sources))
    (tmp_path / "review.json").write_text(json.dumps(document), encoding="utf-8")
    return ["--review", "review.json", "--draft", "draft.json"]


@pytest.mark.parametrize("mutation", ["adopted", "approved_by", "candidate", "owner_decision", "A5_executed"])
def test_cli_rejects_approval_or_candidate_edits_and_does_not_write_inputs(tmp_path, monkeypatch, mutation):
    args = cli_fixture(tmp_path, monkeypatch)
    assert review.main([*args, "--write-draft"]) == 0
    path = tmp_path / "draft.json"
    document = json.loads(path.read_bytes())
    if mutation == "candidate":
        document["files"][NAME]["proposal"]["candidate"]["sha256"] = "0" * 64
    elif mutation == "owner_decision":
        document["files"][NAME]["proposal"]["owner_decision"] = "accept"
    else:
        document[mutation] = "owner" if mutation == "approved_by" else True
    raw = json.dumps(document).encode()
    path.write_bytes(raw)
    assert review.main([*args, "--check"]) == 1 and path.read_bytes() == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["draft.json", "review.json"]


def test_cli_creates_once_checks_and_never_overwrites_existing_draft(tmp_path, monkeypatch):
    args = cli_fixture(tmp_path, monkeypatch)
    assert review.main([*args, "--write-draft"]) == review.main([*args, "--check"]) == 0
    raw = (tmp_path / "draft.json").read_bytes()
    assert review.main([*args, "--write-draft"]) == 1
    assert (tmp_path / "draft.json").read_bytes() == raw


def test_cli_malformed_decimal_is_a_reported_failure(tmp_path, monkeypatch, capsys):
    item = partial()
    item["tail_witness"]["remote"][0] = "not-a-number"
    args = cli_fixture(tmp_path, monkeypatch, item)
    assert review.main([*args, "--write-draft"]) == 1
    assert "ERROR:" in capsys.readouterr().err and not (tmp_path / "draft.json").exists()


def test_snapshot_reads_selected_commit_even_when_workspace_data_changes(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True).stdout.decode().strip()

    git("init", "-q")
    folder = tmp_path / "data/binance/1d"
    folder.mkdir(parents=True)
    manifest = bundle()[1]
    (folder / "_frozen_inputs.json").write_bytes(manifest)
    (folder / NAME).write_bytes(CSV)
    git("-c", "core.autocrlf=false", "add", "data")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "input")
    commit = git("rev-parse", "HEAD")
    (folder / NAME).write_bytes(b"workspace was replaced")
    (folder / "_frozen_inputs.json").unlink()
    assert review.source_snapshot(tmp_path, {"source_commit": commit}) == (manifest, {NAME: CSV})
    with pytest.raises(subprocess.CalledProcessError):
        review.source_snapshot(tmp_path, {"source_commit": "0" * 40})
    with pytest.raises(ValueError, match="full source commit"):
        review.source_snapshot(tmp_path, {"source_commit": "HEAD"})


def test_committed_real_review_and_draft_reproduce_and_do_not_touch_frozen_data():
    raw = (review.ROOT / review.REVIEW).read_bytes()
    document = json.loads(raw)
    manifest, sources = review.source_snapshot(review.ROOT, document)
    folder = review.ROOT / "data/binance/1d"
    before = {name: (folder / name).read_bytes() for name in ["_frozen_inputs.json", *sources]}
    draft, candidates = review.prepare(document, manifest, sources)
    draft["review_sha256"] = review.digest(raw)
    assert draft == json.loads((review.ROOT / review.DRAFT).read_bytes())
    assert document["status_counts"] == {"match": 2, "match_except_final_bar": 27, "mismatch": 1}
    assert len(candidates) == 30 and all(item["point_in_time"] == "unknown" for item in draft["files"].values())
    matic = draft["files"]["MATIC_USDT.csv"]
    assert matic["source"]["rows"] == 2048 and matic["proposal"]["candidate"]["rows"] == 1964
    assert matic["proposal"]["removed_unmatched_rows"] == 83
    assert matic["proposal"]["removed_boundary_rows"] == 1
    assert all((folder / name).read_bytes() == data for name, data in before.items())
