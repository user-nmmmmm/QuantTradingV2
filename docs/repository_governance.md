# Repository governance: owner checklist

This is an operational checklist, not an approval record. CI tests the inventory
tool on synthetic repositories; it cannot certify GitHub settings, notification
delivery, historical review coverage or a cleanup decision. Keep raw inventories,
notification evidence and private reports in ignored local storage.

## Private vulnerability reporting

The supported route is the [private report form](https://github.com/user-nmmmmm/QuantTradingV2/security/advisories/new),
also linked from [Security advisories](https://github.com/user-nmmmmm/QuantTradingV2/security/advisories).
The [reporter policy](../SECURITY.md) describes a contact-only fallback when the
form is unavailable. Never put vulnerability details in that public request.

On 2026-10-15 at 07:28 UTC, an unauthenticated GET to
`https://api.github.com/repos/user-nmmmmm/QuantTradingV2/private-vulnerability-reporting`
returned `{"enabled":true}` and the public Advisories page displayed **Report a
vulnerability**. This establishes the setting and public entry point at that time;
it does not establish notification delivery. A connector rejecting this endpoint
is an access limitation, not evidence of a disabled setting.

Owner steps:

1. Open the repository's **Settings → Security and quality → Advanced Security**.
   Confirm **Private vulnerability reporting** is enabled. If disabled, choose
   **Enable**, then recheck the public Advisories entry point. Do not create a
   public advisory to test private reporting.
2. With a signed-in account that lacks repository admin/security permissions,
   confirm the private report form opens. An administrator's ability to create
   draft advisories does not establish the reporter experience. Stop before
   submitting a report; any delivery exercise requires the owner's explicit plan.
3. Choose **Watch → Custom → Security alerts** (or **All Activity**) and confirm
   personal notification delivery settings. Assign a maintainer who will monitor
   reports. Keep the identity, private advisory URLs and delivery receipts local;
   redact account information before sharing any evidence.
4. If an alternate private mailbox is wanted, the owner must supply and verify a
   monitored address before adding it to `SECURITY.md`. Do not infer one from
   author metadata or commit history. There is no promised response SLA here.

Sources: [GitHub configuration and notification instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository),
[reporter instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately),
[GET endpoint](https://docs.github.com/en/enterprise-cloud@latest/rest/repos/repos#check-if-private-vulnerability-reporting-is-enabled-for-a-repository).

## D1: fresh branch and worktree inventory

Run from the full local repository whose branches/worktrees you intend to review,
not a fresh CI checkout. Refresh each relevant remote yourself, without pruning,
then run the tool from a checkout containing it:

```powershell
git fetch origin
python scripts/repository_governance.py --repo . --base origin/main --output outputs/governance/d1-new.json
```

If other remotes matter, refresh those by name as well. For a shallow clone, fetch
the complete history first. The tool performs no network operation. Its receipt
labels remote branches as **local tracking refs only**; it cannot prove that they
still exist on the server. Compare against a fresh `git ls-remote --heads origin`
for server-side decisions. Record that result locally with its collection time.

The new JSON includes the resolved base SHA, every local/tracking branch ref and
SHA, ahead/behind counts, exact worktree paths/HEADs/branches, detached/locked/
prunable flags and clean/dirty/unavailable status. Spaces and newlines in paths
remain data. Missing history, an invalid base or changing refs/worktree records
fail the inventory. Receipts can only be written to new, ignored `outputs/` files.

Current plan (2026-10-17): retain all branches and worktrees. Maintain the
inventory and retention receipts locally; no cleanup is scheduled. Any later
change of plan requires a fresh inventory and separate per-object approval.
Missing or prunable registrations and object-store dependencies remain reasons
to preserve evidence, not permission to remove it.

For each proposed cleanup, the owner must:

1. Review the exact ref/SHA or worktree path/HEAD from a fresh receipt. Preserve
   unique commits or evidence in a named retained ref before considering removal.
   A branch not reachable from main can have squash/rebase equivalents; inspect
   its diff and provenance rather than assuming it is redundant.
2. Review checked-out, detached, dirty, locked, missing and prunable worktrees
   individually. `prunable` describes a Git registration; it is not permission to
   delete a directory. Do not use recursive filesystem deletion for this task.
3. Record explicit approval against the reviewed snapshot and each object.
   Immediately before any later cleanup, compare current server/local SHAs and
   worktree state with the approved objects; regenerate on any change. Main and
   in-progress branches remain retained. Reachability alone is never approval.
4. Execute only that separately approved cleanup and retain before/after receipts
   locally. This PR provides no deletion command or automated cleanup mode.

## Historical public review evidence

Public API checks on 2026-10-15 found no submitted reviews or inline review
comments for these seven merged PRs. Each conversation still contains only the
linked quota notification. The table below preserves that dated public snapshot
and the original heads; it is not a current inventory of unimplemented fixes.
The selector/archive integrity and design-guidance changes in
[#111](https://github.com/user-nmmmmm/QuantTradingV2/pull/111) are now merged
([ADR 0042](adr/0042-captured-selector-bytes-and-retained-archive-identities.md)).
Their implementation and CI are separate from independent review receipts for
the original diffs. Local audit coverage and findings remain local; neither a
quota message nor a later passing PR establishes independent review approval.

| PR | Original head SHA | Public quota notification | Public status on 2026-10-15 |
|---|---|---|---|
| [#47](https://github.com/user-nmmmmm/QuantTradingV2/pull/47/files) | `66503a176d624281032661395288be054f6bd26a` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/47#issuecomment-5979562444) | Review pending |
| [#48](https://github.com/user-nmmmmm/QuantTradingV2/pull/48/files) | `c37259e33b4cdbab5a9d66300b33b13767ecd878` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/48#issuecomment-5982106151) | Review pending |
| [#49](https://github.com/user-nmmmmm/QuantTradingV2/pull/49/files) | `b0b3262f1b0d140c3ade9b8ded74a3e65e2d2bfd` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/49#issuecomment-5982233993) | Review pending |
| [#50](https://github.com/user-nmmmmm/QuantTradingV2/pull/50/files) | `672536959df684c781501f848535244f3ee3603f` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/50#issuecomment-5985243812) | Review pending |
| [#51](https://github.com/user-nmmmmm/QuantTradingV2/pull/51/files) | `0f2a124de06b56ac631cc76ce13ac9dcb42f432a` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/51#issuecomment-5986953201) | Review pending |
| [#61](https://github.com/user-nmmmmm/QuantTradingV2/pull/61/files) | `06e148f70a868b2220630989c950b04ae4f36eaf` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/61#issuecomment-6098833487) | Review pending |
| [#62](https://github.com/user-nmmmmm/QuantTradingV2/pull/62/files) | `d030f17dfde8428cdc4652ca94d13e28b30ffbb6` | [notification](https://github.com/user-nmmmmm/QuantTradingV2/pull/62#issuecomment-6098953129) | Review pending |

Owner steps: restore the reviewing service's quota or assign a human reviewer;
request review of each original PR diff and record any excluded files/ranges.
For each result, retain reviewer, reviewed SHA/range, result URL, findings,
resolution commits and the corresponding tests. Map findings to their current
main paths before deciding whether they remain applicable. A large PR may need
several bounded reviews; list uncovered ranges until coverage is complete. Do
not relabel the quota notification as approval or post private audit material to
obtain a review. No review request is sent by this tooling.

## Follow-up order and completion

1. After #105, AUD-06 implementation is merged at `e778c56b4639f9cd5e3bf192687e194ace0271ba`.
   AUD-07 adds direct disclosure guidance, the read-only D1 tool and this checklist.
2. Ordinary-reporter access, owner notification delivery and independent review
   receipts remain separate external evidence items. The enabled disclosure
   setting and #111 engineering fixes do not establish those results. D1 follows
   the retain-all plan; the actual inventory and retention evidence stay local.
3. #111 engineering fixes and #112/#113 dependency updates are merged. Continue
   the [repair baseline/release work](repair_release_baseline.md) after selecting
   and checking the final main commit; tags, bindings and A5 remain deferred.
   Engineering CI does not create a release tag, register an observation window
   or establish strategy admission. Existing historical evidence is preserved.
