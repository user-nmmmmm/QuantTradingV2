# ADR 0040: Private disclosure and reviewable repository governance

Status: accepted. Date: 2026-10-15.

## Context

The public [#54 review](https://github.com/user-nmmmmm/QuantTradingV2/pull/54#discussion_r4237097138)
identified a disclosure policy without a usable route. Current public metadata
and the Advisories page establish that private reporting is now enabled; they
do not establish notification delivery. D1 needs a current per-object inventory,
and seven public quota notifications still lack corresponding review results.

## Decision

Link the private form directly from `SECURITY.md`; offer a contact-only public
request if the form is unavailable. Preserve the existing scope and credential
rules. Never publish a guessed mailbox or vulnerability details in that request.

Provide `scripts/repository_governance.py` to inventory an explicit resolved base,
refs/SHAs, ancestry and worktree state using read-only Git operations. Fail on
shallow history, missing objects or changed ref/worktree records. Keep receipts
in new ignored `outputs/` files. The tool does not fetch, approve or delete.

Maintain [owner steps and public evidence](../repository_governance.md) separately
from private receipts. Owner notification checks, cleanup approval/execution and
complete historical reviews stay open. No CI result substitutes for them.

## Validation and limitations

Synthetic Git regressions cover divergent/reachable refs, checked-out/dirty/
detached/locked/prunable worktrees, unusual paths, failed reads, shallow history,
snapshot drift and output privacy. The same tests run on Linux/Windows and
Python 3.11/3.13. This is a point-in-time inventory, not an atomic deletion lease;
remote-tracking refs require separate refresh and server verification. Worktree
contents can change after inspection and must be rechecked before any cleanup.
