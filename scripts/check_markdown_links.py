"""Fail on new broken relative links in tracked Markdown files.

Scope and rules
---------------
* Only tracked ``*.md`` files are scanned. Frozen snapshots under
  ``docs/archive/2026-08*`` and ``docs/archive/2026-09*`` are skipped: they are
  byte-preserving evidence whose links may legitimately point at moved files.
* Link targets under ``reports/``, ``outputs/``, ``data/`` or ``tmp/`` are not
  checked: they are generated or ignored locally and absent from a clean checkout.
* Anchors (``#...``) and external URLs are not checked.
* A broken link whose target appears in ``docs/doc_index.json`` ("moved") is reported
  with the new location.
* ``docs/link_check_baseline.json`` lists links that were already broken when this
  check was introduced. They are tolerated; any other broken link fails. Fixing a
  baseline entry is encouraged (``--update-baseline`` rewrites the file).
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "link_check_baseline.json"
DOC_INDEX = ROOT / "docs" / "doc_index.json"
SKIP_SOURCES = ("docs/archive/2026-08", "docs/archive/2026-09")
GENERATED_TARGETS = ("reports/", "outputs/", "data/", "tmp/")
LINK = re.compile(r"\]\(([^)\s]+?)(?:#[^)\s]*)?(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [p.decode("utf-8") for p in out.split(bytes([0])) if p]


def iter_links(text: str):
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(re.sub(r"`[^`]*`", "", line)):  # ignore inline code
            yield number, match.group(1)


def find_broken(files: list[str]) -> list[dict]:
    known = set(files)
    directories = {posixpath.dirname(p) for p in files}
    for path in list(directories):
        while path:
            directories.add(path)
            path = posixpath.dirname(path)
    moved = json.loads(DOC_INDEX.read_text(encoding="utf-8"))["moved"] if DOC_INDEX.is_file() else {}
    broken = []
    for source in files:
        if not source.endswith(".md") or source.startswith(SKIP_SOURCES):
            continue
        text = (ROOT / source).read_text(encoding="utf-8", errors="replace")
        for line, target in iter_links(text):
            if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/|#)", target):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            if resolved.startswith(("..",)) or resolved.startswith(GENERATED_TARGETS):
                continue
            if resolved in known or resolved.rstrip("/") in directories:
                continue
            entry = {"source": source, "target": resolved}
            if resolved in moved:
                entry["moved_to"] = moved[resolved]
            entry["line"] = line
            broken.append(entry)
    return broken


def key(entry: dict) -> tuple[str, str]:
    return entry["source"], entry["target"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update-baseline", action="store_true", help="rewrite the baseline with the current broken links")
    args = parser.parse_args(argv)

    broken = find_broken(tracked_files())
    if args.update_baseline:
        entries = sorted(({"source": s, "target": t} for s, t in {key(e) for e in broken}), key=lambda e: (e["source"], e["target"]))
        BASELINE.write_text(json.dumps({"schema": "link-check-baseline/v1", "broken": entries}, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
        print(f"baseline updated: {len(entries)} known broken links")
        return 0

    baseline = {(e["source"], e["target"]) for e in json.loads(BASELINE.read_text(encoding="utf-8"))["broken"]} if BASELINE.is_file() else set()
    new = [e for e in broken if key(e) not in baseline]
    fixed = baseline - {key(e) for e in broken}
    for entry in new:
        hint = f" (moved to {entry['moved_to']})" if "moved_to" in entry else ""
        print(f"ERROR: {entry['source']}:{entry['line']}: broken link -> {entry['target']}{hint}")
    if fixed:
        print(f"note: {len(fixed)} baseline entries are fixed; run --update-baseline to shrink the baseline")
    if new:
        return 1
    print(f"Markdown link check passed ({len(broken)} tolerated by baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
