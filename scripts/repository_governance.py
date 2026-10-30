"""Read-only D1 inventory; write a new local receipt under the target repo's ignored outputs/.

Refresh remote-tracking refs yourself before running. This tool never fetches,
prunes, removes worktrees, deletes branches or approves a cleanup. Reachability
is a review aid; it does not establish that a branch is safe to delete.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


class InventoryError(ValueError):
    """The requested inventory cannot be established from complete local facts."""


def git(repo: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
                            capture_output=True, env=env, check=False)
    if result.returncode:
        # Do not echo Git stderr: remote configuration and local paths can be private.
        raise InventoryError(f"git {args[0]} failed (exit {result.returncode}); check the repository and refs")
    return result.stdout


def text(repo: Path, *args: str) -> str:
    # Git adds one LF; all preceding whitespace may belong to the path itself.
    return git(repo, *args).decode("utf-8").removesuffix("\n")


def refs(repo: Path) -> list[dict]:
    result = []
    for line in git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)",
                    "refs/heads/", "refs/remotes/").decode("utf-8").splitlines():
        name, sha, symbolic = line.split("\t")
        if not symbolic:  # origin/HEAD is an alias, not an extra branch
            result.append({"ref": name, "sha": sha})
    return result


def parse_worktrees(raw: bytes) -> list[dict]:
    """Use NUL delimiters so spaces, quotes and newlines in a path remain data."""
    result = []
    for block in raw.decode("utf-8").split("\0\0"):
        if not block:
            continue
        fields = {}
        for item in block.split("\0"):
            if item:
                key, _, value = item.partition(" ")
                fields[key] = value
        if "worktree" not in fields or "HEAD" not in fields:
            raise InventoryError("invalid worktree inventory")
        result.append({"path": fields["worktree"], "head": fields["HEAD"],
                       "branch": fields.get("branch"), "detached": "detached" in fields,
                       "locked": "locked" in fields, "lock_reason": fields.get("locked"),
                       "prunable": "prunable" in fields, "prune_reason": fields.get("prunable")})
    return result


def worktree_status(repo: Path, item: dict, common_dir: Path) -> str:
    path = Path(item["path"])
    if item["prunable"] or not path.is_dir():
        return "unavailable"
    try:
        actual_root = Path(text(path, "rev-parse", "--show-toplevel")).resolve()
        actual_common = Path(text(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if actual_root != path.resolve() or actual_common != common_dir:
            return "unavailable"
        status = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none")
        return "dirty" if status else "clean"
    except (InventoryError, OSError):
        return "unavailable"


def snapshot(repo: Path, base: str) -> dict:
    repo = Path(text(repo.resolve(), "rev-parse", "--show-toplevel")).resolve()
    if text(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise InventoryError("complete history required; shallow repositories cannot establish reachability")
    base_sha = text(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
    started = datetime.now(timezone.utc).isoformat()
    common_dir = Path(text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    before_refs = refs(repo)
    before_worktrees = git(repo, "worktree", "list", "--porcelain", "-z")
    worktrees = parse_worktrees(before_worktrees)
    for item in worktrees:
        item["status"] = worktree_status(repo, item, common_dir)
    branches = []
    for ref in before_refs:
        behind, ahead = map(int, text(repo, "rev-list", "--left-right", "--count",
                                     base_sha + "..." + ref["sha"]).split())
        attached = [item["path"] for item in worktrees if item["branch"] == ref["ref"]]
        protected = ref["ref"].split("/")[-1] == "main" or ref["sha"] == base_sha
        review = ("keep_base" if protected else "review_worktree" if attached
                  else "review_unique_commits" if ahead else "review_reachable")
        branches.append({**ref, "ahead": ahead, "behind": behind, "reachable_from_base": ahead == 0,
                         "worktrees": attached, "review": review})
    if (before_refs != refs(repo) or before_worktrees != git(repo, "worktree", "list", "--porcelain", "-z")
            or base_sha != text(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")):
        raise InventoryError("refs or worktrees changed during inventory; retry with a fresh snapshot")
    return {"schema": "repository-governance/v1", "started_at_utc": started,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(), "repository": str(repo),
            "base_ref": base, "base_sha": base_sha, "remote_state": "local_tracking_refs_only",
            "owner_approval": "required_per_ref_sha_and_worktree", "branches": branches,
            "worktrees": worktrees}


def write_snapshot(repo: Path, output: Path, receipt: dict) -> Path:
    repo = Path(text(repo.resolve(), "rev-parse", "--show-toplevel")).resolve()
    output = output.resolve() if output.is_absolute() else (repo / output).resolve()
    if not output.is_relative_to(repo / "outputs"):
        raise InventoryError("receipt must remain under the repository's ignored outputs/ directory")
    relative = output.relative_to(repo).as_posix()
    # A tracked file can still match an ignore pattern. Check both facts.
    if git(repo, "ls-files", "-z", "--", relative):
        raise InventoryError("refusing to overwrite a tracked file")
    # Only the exit status is needed; printed paths may be quoted or contain newlines.
    git(repo, "check-ignore", "--quiet", "--no-index", "--", relative)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True, help="explicit local ref or full commit SHA; fetch first if needed")
    parser.add_argument("--output", type=Path, required=True, help="new ignored outputs/...json; never committed")
    args = parser.parse_args(argv)
    try:
        receipt = snapshot(args.repo, args.base)
        output = write_snapshot(args.repo, args.output, receipt)
    except (InventoryError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote local inventory: {output.name}; owner review required, no cleanup performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
