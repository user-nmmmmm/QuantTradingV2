"""Verify the owner-approved adoption against historical v2 evidence and exact Git bytes.

The historical review/draft remain unchanged. This checker is offline and never
adopts files, grants approval, creates release tags or executes A5.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys

from scripts import data_adoption_review as review
from scripts import data_inputs as inputs

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = Path("docs/data_review/20261016_adoption_receipt.json")
POLICY = "adopted-research-window/v1"
POLICY_FILE = "_research_policy.json"
COMMON_END = "2026-08-26"
MATIC_END = "2024-09-09"


def render(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def corroboration(item: dict, facts: dict, review_sha: str, applied_at: str) -> dict:
    """The approved prefix was fully matched in the saved v2 review, not newly downloaded."""
    return {"status": "match", "comparison_policy": inputs.COMPARISON_POLICY,
            "for_sha256": facts["sha256"], "source": item["corroboration"]["source"],
            "checked_at": applied_at, "source_checked_at": item["corroboration"]["checked_at"],
            "evidence_review_sha256": review_sha, "basis": "retained_prefix_of_saved_v2_review",
            "rows_local": facts["rows"], "rows_remote": facts["rows"], "rows_matching": facts["rows"],
            "difference_count": 0, "only_local_count": 0, "only_remote_count": 0,
            "invalid_local_count": 0, "invalid_remote_count": 0,
            "examples": {key: [] for key in ("differences", "only_local", "only_remote", "invalid_local", "invalid_remote")}}


def prepare(review_bytes: bytes, draft_bytes: bytes, original_manifest: bytes, sources: dict[str, bytes], *,
            base_commit: str, applied_commit: str, applied_at: str, approval_recorded_at: str) -> tuple:
    document = json.loads(review_bytes)
    expected_draft, candidates = review.prepare(document, original_manifest, sources)
    expected_draft["review_sha256"] = review.digest(review_bytes)
    review.require(json.loads(draft_bytes) == expected_draft, "historical draft differs from v2 review")
    review.require(set(candidates) == set(sources), "every input needs an approved prefix")
    for commit in (base_commit, applied_commit):
        review.require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) is not None,
                       "adoption needs full base/applied commits")
    for value in (applied_at, approval_recorded_at):
        review.require(datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None,
                       "adoption times need explicit timezone")
    manifest = deepcopy(json.loads(original_manifest))
    files, limits = {}, {}
    for name, data in sorted(candidates.items()):
        facts = inputs._describe_bytes(data, name)
        proposed = expected_draft["files"][name]
        cutoff = MATIC_END if name == "MATIC_USDT.csv" else COMMON_END
        bars = inputs._bars_from_bytes(data, name)
        eligible = sum(day <= cutoff for day in bars)
        review.require(eligible > 0 and cutoff in bars, f"{name}: research cutoff must be covered")
        entry = manifest["files"][name]
        entry.update(facts)
        entry["corroboration"] = corroboration(document["files"][name], facts, expected_draft["review_sha256"], applied_at)
        files[name] = {"symbol": entry["symbol"], "source": proposed["source"], "adopted": facts,
                       "action": proposed["proposal"]["action"],
                       "removed_unmatched_rows": proposed["proposal"]["removed_unmatched_rows"],
                       "removed_boundary_rows": proposed["proposal"]["removed_boundary_rows"],
                       "research_end_inclusive": cutoff,
                       "research_end_exclusive": (date.fromisoformat(cutoff) + timedelta(days=1)).isoformat(),
                       "research_rows": eligible, "retained_rows_outside_research": facts["rows"] - eligible,
                       "provenance_status": entry["provenance"]["status"], "point_in_time": entry["point_in_time"]}
        limits[name] = {"sha256": facts["sha256"], "research_end_inclusive": cutoff}
    manifest_bytes = inputs.render(manifest).encode("utf-8")
    receipt = {"schema": "data-adoption-receipt/v1", "status": "applied_to_candidate",
               "approved_by": "repository_owner", "approval_basis": "explicit owner instruction selecting the recommended policies",
               "approval_recorded_at": approval_recorded_at, "applied_at": applied_at,
               "base_commit": base_commit, "applied_commit": applied_commit,
               "source_commit": document["source_commit"], "review_sha256": expected_draft["review_sha256"],
               "historical_draft_sha256": review.digest(draft_bytes),
               "source_manifest_sha256": review.digest(original_manifest),
               "adopted_manifest_sha256": review.digest(manifest_bytes),
               "owner_decisions": {"tail_policy": "trim_final_bar", "MATIC_policy": "retain_pre_delisting_prefix",
                                   "experiment_horizon": COMMON_END},
               "research_policy": POLICY, "common_research_end_inclusive": COMMON_END,
               "A5_executed": False, "release_tag_created": False, "files": files,
               "limitations": ["Saved public v2 evidence was replayed; no new download or historical availability is claimed.",
                               "Raw responses and the full audit remain local; old CSVs/manifests remain in source Git history.",
                               "Applied to this candidate; main takes these inputs when the containing PR is merged.",
                               "CI results belong to the final PR head containing this receipt, separately from the data commit."]}
    receipt_bytes = render(receipt)
    policy = {"schema": POLICY, "timeframe": "1d", "receipt": RECEIPT.as_posix(),
              "receipt_sha256": review.digest(receipt_bytes), "common_research_end_inclusive": COMMON_END, "files": limits}
    return candidates, manifest_bytes, receipt_bytes, render(policy)


def check(root: Path = ROOT) -> int:
    receipt_bytes = (root / RECEIPT).read_bytes()
    receipt = json.loads(receipt_bytes)
    review_bytes, draft_bytes = (root / review.REVIEW).read_bytes(), (root / review.DRAFT).read_bytes()
    manifest, sources = review.source_snapshot(root, json.loads(review_bytes))
    candidates, adopted_manifest, expected_receipt, policy = prepare(
        review_bytes, draft_bytes, manifest, sources, **{key: receipt[key] for key in
        ("base_commit", "applied_commit", "applied_at", "approval_recorded_at")})
    review.require(review.digest(receipt_bytes) == review.digest(expected_receipt), "adoption receipt differs from approved derivation")
    expected = {"data/binance/1d/" + name: data for name, data in candidates.items()}
    expected["data/binance/1d/_frozen_inputs.json"] = adopted_manifest
    for path, data in expected.items():
        review.require(review.digest((root / path).read_bytes()) == review.digest(data), f"{path}: adopted bytes differ")
        committed = subprocess.run(["git", "cat-file", "blob", f"{receipt['applied_commit']}:{path}"], cwd=root,
                                   check=True, capture_output=True).stdout
        review.require(review.digest(committed) == review.digest(data), f"{path}: applied commit differs")
    original_cache = subprocess.run(["git", "cat-file", "blob", f"{receipt['source_commit']}:data/binance/1d/_manifest.json"],
                                    cwd=root, check=True, capture_output=True).stdout
    review.require(review.digest((root / "data/binance/1d/_manifest.json").read_bytes()) == review.digest(original_cache),
                   "original download cache must remain unchanged")
    tracked = subprocess.run(["git", "ls-files", "-z", "--", "data/binance/1d/*.csv"], cwd=root,
                             check=True, capture_output=True).stdout
    actual_names = {Path(name.decode()).name for name in tracked.split(b"\0") if name}
    review.require(actual_names == set(candidates), "adoption must cover exactly the tracked CSV set")
    review.require(review.digest((root / "data/binance/1d" / POLICY_FILE).read_bytes()) == review.digest(policy),
                   "research policy differs from approved receipt")
    subprocess.run(["git", "merge-base", "--is-ancestor", receipt["base_commit"], receipt["applied_commit"]],
                   cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "merge-base", "--is-ancestor", receipt["applied_commit"], "HEAD"],
                   cwd=root, check=True, capture_output=True)
    return len(candidates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        count = check(ROOT)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Approved data adoption verified: {count} inputs; research ends {COMMON_END}, MATIC {MATIC_END}; A5 not executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
