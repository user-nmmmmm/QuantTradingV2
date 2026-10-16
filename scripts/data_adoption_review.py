"""Prepare/check an unsigned data-adoption draft from a versioned corroboration review.

Reads the original CSVs from the review's Git commit, so later data adoption
does not rewrite this historical review. No network, CSV replacement, approval
or A5 registration is performed. Raw HTTP evidence is retained separately.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation

from scripts import data_inputs as inputs

ROOT = Path(__file__).resolve().parents[1]
REVIEW = Path("docs/data_review/20261015_binance_v2.json")
DRAFT = Path("docs/data_review/20261015_adoption_draft.json")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def source_snapshot(root: Path, review: dict) -> tuple[bytes, dict[str, bytes]]:
    commit = review.get("source_commit", "")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) is not None,
            "review must name a full source commit")

    def read(path: str) -> bytes:
        return subprocess.run(["git", "cat-file", "blob", f"{commit}:{path}"], cwd=root,
                              check=True, capture_output=True).stdout

    manifest = read("data/binance/1d/_frozen_inputs.json")
    names = json.loads(manifest)["files"]
    require(all(Path(name).name == name and "/" not in name and "\\" not in name
                and "\n" not in name and "\r" not in name and name.endswith(".csv") for name in names),
            "input names must be CSV basenames")
    return manifest, {name: read("data/binance/1d/" + name) for name in names}


def prefix_bytes(data: bytes, before: str) -> bytes:
    lines = data.replace(b"\r\n", b"\n").splitlines()
    kept = [line for line in lines[1:] if line.strip()
            and next(csv.reader([line.decode("utf-8")]))[0] < before]
    require(bool(kept), "proposed prefix would be empty")
    return b"\n".join([lines[0], *kept]) + b"\n"


def proposal(name: str, data: bytes, item: dict) -> tuple[dict, bytes | None]:
    data = data.replace(b"\r\n", b"\n")
    bars = inputs._bars_from_bytes(data, name)
    facts = inputs._describe_bytes(data, name)
    require(facts == item["snapshot"], f"{name}: snapshot facts differ")
    block = item["corroboration"]
    require(block.get("comparison_policy") == inputs.COMPARISON_POLICY, f"{name}: v2 review required")
    require(block.get("for_sha256") == facts["sha256"], f"{name}: corroboration digest differs")
    require(block.get("status") in inputs.CORROBORATION_STATUSES, f"{name}: unknown status")
    require(block.get("rows_local") == len(bars), f"{name}: local row count differs")
    for key in ("rows_remote", "rows_matching", "difference_count", "only_local_count", "only_remote_count",
                "invalid_local_count", "invalid_remote_count"):
        require(type(block.get(key)) is int and block[key] >= 0, f"{name}: invalid {key}")
    require(block["rows_matching"] <= min(len(bars) - block["only_local_count"],
                                         block["rows_remote"] - block["only_remote_count"]),
            f"{name}: impossible matching count")
    valid = (all(inputs._valid_bar(bar) for bar in bars.values())
             and block["invalid_local_count"] == block["invalid_remote_count"] == 0)
    complete = block["only_local_count"] == block["only_remote_count"] == 0
    status = block["status"]
    removed_unmatched = removed_boundary = 0
    candidate = None
    action = "quarantine_pending_review"
    if status == "match":
        require(valid and complete and block["rows_remote"] == block["rows_matching"] == len(bars)
                and block["difference_count"] == 0,
                f"{name}: inconsistent match")
        candidate, action = data, "retain_bytes"
    elif status == "match_except_final_bar":
        witness = item["tail_witness"]
        last = max(bars)
        remote = tuple(Decimal(value) for value in witness["remote"])
        require(witness["date"] == last and tuple(Decimal(x) for x in witness["local"]) == bars[last],
                f"{name}: tail witness differs from input")
        require(valid and complete and inputs._valid_bar(remote)
                and block["rows_remote"] == len(bars) and block["rows_matching"] == len(bars) - 1
                and block["difference_count"] == sum(a != b for a, b in zip(bars[last], remote)) > 0
                and inputs._compatible_partial(bars[last], remote), f"{name}: invalid partial-tail evidence")
        candidate, action = prefix_bytes(data, last), "trim_final_bar"
        removed_boundary = 1
    elif status == "mismatch" and valid:
        unknown = item.get("unmatched_dates", [])
        end = item.get("remote_last")
        event = item.get("market_lifecycle")
        prefix_match = (isinstance(end, str) and block["difference_count"] == 0
                        and block["only_remote_count"] == 0 and block["rows_remote"] == block["rows_matching"]
                        and block["rows_matching"] > 0 and block["only_local_count"] > 0
                        and end in bars and item.get("remote_first") == min(bars)
                        and block["rows_matching"] == sum(day <= end for day in bars)
                        and unknown == sorted(day for day in bars if day > end)
                        and len(unknown) == block["only_local_count"])
        if prefix_match and event:
            require(event.get("market") == "binance-spot" and event.get("source_url", "").startswith("https://www.binance.com/"),
                    f"{name}: lifecycle evidence required")
            stopped = datetime.fromisoformat(event["trading_stopped_at"].replace("Z", "+00:00"))
            require(stopped.tzinfo is not None, f"{name}: lifecycle time needs timezone")
            cutoff = stopped.astimezone(timezone.utc).date().isoformat()
            require(cutoff == end, f"{name}: delisting day differs from source endpoint boundary")
            candidate, action = prefix_bytes(data, cutoff), "trim_unmatched_and_delisting_day"
            removed_unmatched, removed_boundary = len(unknown), sum(day >= cutoff and day not in unknown for day in bars)
    result = {"action": action, "owner_decision": None, "removed_unmatched_rows": removed_unmatched,
              "removed_boundary_rows": removed_boundary, "candidate": None}
    if candidate is not None:
        result["candidate"] = inputs._describe_bytes(candidate, name)
        result["removed_rows"] = len(bars) - result["candidate"]["rows"]
    return result, candidate


def prepare(review: dict, manifest_bytes: bytes, sources: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    require(review.get("schema") == "data-corroboration-review/v1", "unsupported review schema")
    require(review.get("comparison_policy") == inputs.COMPARISON_POLICY, "v2 comparison policy required")
    require(digest(manifest_bytes) == review["input_manifest_sha256"], "source manifest digest differs")
    manifest = json.loads(manifest_bytes)
    require(set(review["files"]) == set(manifest["files"]) == set(sources), "review must cover the exact frozen file set")
    counts = Counter(item["corroboration"]["status"] for item in review["files"].values())
    require(dict(counts) == review["status_counts"], "review status counts differ")
    files, candidates = {}, {}
    for name, entry in sorted(manifest["files"].items()):
        data = sources[name].replace(b"\r\n", b"\n")
        facts = {key: entry[key] for key in ("sha256", "rows", "first", "last")}
        require(facts == review["files"][name]["snapshot"], f"{name}: frozen manifest facts differ")
        proposed, candidate = proposal(name, data, review["files"][name])
        files[name] = {"symbol": entry["symbol"], "source": facts, "proposal": proposed,
                       "provenance_status": entry["provenance"]["status"], "point_in_time": "unknown"}
        if candidate is not None:
            candidates[name] = candidate
    draft = {"schema": "data-adoption-draft/v1", "status": "draft_pending_owner", "adopted": False,
             "approved_by": None, "approved_at": None, "applied_commit": None, "A5_executed": False,
             "source_commit": review["source_commit"], "input_manifest_sha256": review["input_manifest_sha256"],
             "comparison_policy": inputs.COMPARISON_POLICY, "files": files,
             "decisions_required": {"tail_policy": None, "MATIC_policy": None, "experiment_horizon": None},
             "limitations": ["Corroboration is current agreement, not original provenance or historical availability.",
                             "Candidates are proposed byte prefixes; no existing CSV or receipt is replaced.",
                             "Keep per-symbol coverage explicit; do not forward-fill delisted symbols or silently extend the study horizon."]}
    return draft, candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=REVIEW)
    parser.add_argument("--draft", type=Path, default=DRAFT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write-draft", action="store_true", help="create a new draft JSON, never overwrites or approves")
    args = parser.parse_args(argv)
    try:
        review_path, draft_path = ROOT / args.review, ROOT / args.draft
        review_bytes = review_path.read_bytes()
        review = json.loads(review_bytes)
        manifest, sources = source_snapshot(ROOT, review)
        draft, _ = prepare(review, manifest, sources)
        draft["review_sha256"] = digest(review_bytes)
        if args.check:
            require(json.loads(draft_path.read_bytes()) == draft, "draft differs from source review; approvals cannot be added to this draft")
        else:
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            with draft_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(draft, indent=2, sort_keys=True) + "\n")
    except (ValueError, KeyError, TypeError, OSError, InvalidOperation, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Data adoption draft checked: {len(draft['files'])} inputs; owner decisions pending; A5 not executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
