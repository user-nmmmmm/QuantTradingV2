"""Report whether a change alters the behavioral identity of the frozen scope.

Examples
--------
  # PR impact (what CI runs): base branch vs HEAD
  python scripts/identity_report.py --base origin/main --head HEAD

  # Save the identity of a commit as a receipt, and verify a tree against it later
  python scripts/identity_report.py --head HEAD --write-receipt receipt.json
  python scripts/identity_report.py --head WORKTREE --verify-receipt receipt.json --fail-on-change

The scope is declared in ``config/frozen_scope.json``. Comment-only and layout-only
edits to Python files are reported as cosmetic and do not change the identity.
This report never relaxes the legacy byte-level verifiers; it informs reviewers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from core.source_identity import (Impact, Scope, SCHEME, build_manifest, compare, identity_digest,
                                  receipt, verify_receipt)

ROOT = Path(__file__).resolve().parents[1]
SCOPE_FILE = ROOT / "config" / "frozen_scope.json"


def _ref(value: str):
    return None if value.upper() == "WORKTREE" else value


def _sha(ref):
    if ref is None:
        return None
    return subprocess.run(["git", "rev-parse", ref], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()


def render(impact: Impact, *, base_label: str, head_label: str, scope_files: int) -> str:
    verdict = "**CHANGED**" if impact.changes_identity else "unchanged"
    lines = [f"### Frozen-scope behavioral identity: {verdict}",
             "",
             f"`{base_label}` -> `{head_label}` | scheme `{SCHEME}` | {scope_files} files in scope", ""]
    sections = (("Behavioral changes", impact.behavioral), ("Added", impact.added),
                ("Removed", impact.removed), ("Cosmetic only (comments / layout)", impact.cosmetic))
    for title, paths in sections:
        if paths:
            lines.append(f"**{title}** ({len(paths)})")
            lines.extend(f"- `{p}`" for p in paths[:50])
            if len(paths) > 50:
                lines.append(f"- ... and {len(paths) - 50} more")
            lines.append("")
    if impact.changes_identity:
        lines.append("Candidates frozen against the previous identity must be re-registered under their "
                     "protocol before this change is treated as the same candidate (see docs/identity.md).")
    elif impact.cosmetic:
        lines.append("Only comments or layout changed: the behavioral identity is the same.")
    else:
        lines.append("No file in the frozen scope changed.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="git ref to compare from (e.g. origin/main)")
    parser.add_argument("--head", default="HEAD", help="git ref or WORKTREE (default HEAD)")
    parser.add_argument("--scope", type=Path, default=SCOPE_FILE)
    parser.add_argument("--summary-file", type=Path, help="append the markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--fail-on-change", action="store_true", help="exit 1 when the behavioral identity changed")
    parser.add_argument("--write-receipt", type=Path, help="write the identity of --head as a JSON receipt")
    parser.add_argument("--verify-receipt", type=Path, help="compare --head with a saved receipt")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of markdown")
    args = parser.parse_args(argv)

    scope = Scope.load(args.scope)
    head_ref = _ref(args.head)
    head = build_manifest(ROOT, scope, head_ref)

    if args.write_receipt:
        data = receipt(head, ref_sha=_sha(head_ref))
        data["scope_sha256"] = hashlib.sha256(args.scope.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if args.write_receipt.exists():
            print(f"refusing to overwrite {args.write_receipt}", file=sys.stderr)
            return 2
        args.write_receipt.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"identity {data['identity_sha256']} ({len(head)} files) -> {args.write_receipt}")
        return 0

    if args.verify_receipt:
        saved = json.loads(args.verify_receipt.read_text(encoding="utf-8"))
        impact = verify_receipt(saved, head)
        base_label = f"receipt@{(saved.get('git_sha') or 'unknown')[:10]}"
    elif args.base:
        impact = compare(build_manifest(ROOT, scope, _ref(args.base)), head)
        base_label = args.base
    else:
        parser.error("one of --base, --verify-receipt or --write-receipt is required")

    if args.json:
        print(json.dumps({"changes_identity": impact.changes_identity, "behavioral": impact.behavioral,
                          "added": impact.added, "removed": impact.removed, "cosmetic": impact.cosmetic,
                          "head_identity_sha256": identity_digest(head)}, indent=2))
    else:
        text = render(impact, base_label=base_label, head_label=args.head, scope_files=len(head))
        print(text)
        if args.summary_file:
            with args.summary_file.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    return 1 if (args.fail_on_change and impact.changes_identity) else 0


if __name__ == "__main__":
    raise SystemExit(main())
