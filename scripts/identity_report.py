"""Report whether a change alters the behavioral identity of the frozen scope.

Examples
--------
  # PR impact (what CI runs): base branch vs HEAD
  python scripts/identity_report.py --base origin/main --head HEAD

  # Save the identity of a commit as a receipt, and verify a tree against it later
  python scripts/identity_report.py --head HEAD --write-receipt receipt.json
  python scripts/identity_report.py --head WORKTREE --verify-receipt receipt.json --fail-on-change

  # Bind an annotated release tag to its identity, and verify against that binding later (docs/releasing.md)
  python scripts/identity_report.py --bind-release v3.0.0 --write-binding identity_binding.json
  python scripts/identity_report.py --verify-binding identity_binding.json --head WORKTREE --fail-on-change

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

from core.foundation.source_identity import (Impact, Scope, SCHEME, bind_release, build_manifest, compare, identity_digest,
                                  receipt, scope_sha256, verify_binding, verify_receipt)

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
    if not impact.changes_identity:
        verdict = "unchanged"
    elif impact.changes_behavior:
        verdict = "**CHANGED**"
    else:
        verdict = "**CHANGED** (moves / import paths only; behavior unchanged)"
    lines = [f"### Frozen-scope behavioral identity: {verdict}",
             "",
             f"`{base_label}` -> `{head_label}` | scheme `{SCHEME}` | {scope_files} files in scope", ""]
    if impact.moved:
        lines.append(f"Moved {len(impact.moved)} files, file content unchanged apart from import paths.")
        lines.append("")
    sections = (("Behavioral changes", impact.behavioral), ("Added", impact.added),
                ("Removed", impact.removed),
                ("Moved (old -> new)", [f"{old}` -> `{new}" for old, new in impact.moved]),
                ("Only import statements changed", impact.import_only),
                ("Cosmetic only (comments / layout)", impact.cosmetic))
    for title, paths in sections:
        if paths:
            lines.append(f"**{title}** ({len(paths)})")
            lines.extend(f"- `{p}`" for p in paths[:50])
            if len(paths) > 50:
                lines.append(f"- ... and {len(paths) - 50} more")
            lines.append("")
    if impact.changes_identity and not impact.changes_behavior:
        lines.append("The identity digest changed, but only through file locations and import paths. "
                     "Import targets are not verified by this report: the layering rules and the test suite "
                     "cover them. Candidates frozen against the previous identity still need re-registration "
                     "under their protocol (see docs/identity.md).")
    elif impact.changes_identity:
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
    parser.add_argument("--bind-release", metavar="TAG", help="record an annotated release tag with its identity")
    parser.add_argument("--write-binding", type=Path, help="where --bind-release writes the binding (never overwrites)")
    parser.add_argument("--verify-binding", type=Path, help="compare --head with a saved release binding")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of markdown")
    args = parser.parse_args(argv)

    if args.bind_release:
        if not args.write_binding:
            parser.error("--bind-release needs --write-binding")
        if args.write_binding.exists():
            print(f"refusing to overwrite {args.write_binding}", file=sys.stderr)
            return 2
        try:
            data = bind_release(ROOT, args.bind_release)
        except ValueError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        args.write_binding.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{args.bind_release} -> {data['tag_commit'][:12]} identity {data['receipt']['identity_sha256']} "
              f"({len(data['receipt']['files'])} files) -> {args.write_binding}")
        return 0

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

    problems = []
    if args.verify_binding:
        saved = json.loads(args.verify_binding.read_text(encoding="utf-8"))
        problems, impact = verify_binding(saved, ROOT, head, scope_sha256(args.scope.read_bytes()))
        base_label = f"{saved.get('tag')}@{(saved.get('tag_commit') or 'unknown')[:10]}"
    elif args.verify_receipt:
        saved = json.loads(args.verify_receipt.read_text(encoding="utf-8"))
        impact = verify_receipt(saved, head)
        base_label = f"receipt@{(saved.get('git_sha') or 'unknown')[:10]}"
    elif args.base:
        impact = compare(build_manifest(ROOT, scope, _ref(args.base)), head)
        base_label = args.base
    else:
        parser.error("one of --base, --verify-receipt, --verify-binding, --write-receipt "
                     "or --bind-release is required")
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)

    if args.json:
        print(json.dumps({"changes_identity": impact.changes_identity, "changes_behavior": impact.changes_behavior,
                          "behavioral": impact.behavioral, "added": impact.added, "removed": impact.removed,
                          "moved": [list(pair) for pair in impact.moved], "import_only": impact.import_only,
                          "cosmetic": impact.cosmetic,
                          "head_identity_sha256": identity_digest(head)}, indent=2))
    else:
        text = render(impact, base_label=base_label, head_label=args.head, scope_files=len(head))
        print(text)
        if args.summary_file:
            with args.summary_file.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    if problems:
        return 1
    return 1 if (args.fail_on_change and impact.changes_identity) else 0


if __name__ == "__main__":
    raise SystemExit(main())
