"""Fail on new broken relative links in tracked Markdown files.

Scope and rules
---------------
* Only tracked ``*.md`` files are scanned. Frozen snapshots under
  ``docs/archive/2026-08*`` and ``docs/archive/2026-09*`` are skipped: they are
  byte-preserving evidence whose links may legitimately point at moved files.
* Link targets under ``reports/``, ``outputs/``, ``data/`` or ``tmp/`` are not
  checked: they are generated or ignored locally and absent from a clean checkout.
* Anchors (``#...``) and external URLs are not checked.
* Inline links/images and full, collapsed and shortcut references are checked.
  Reference labels use the first definition, ignoring case and repeated whitespace.
  Unbound bracketed text is literal Markdown, not a link. Fenced/inline code and HTML comments are ignored.
* A broken link whose target appears in ``docs/doc_index.json`` ("moved") is reported
  with the new location.
* ``docs/link_check_baseline.json`` lists links that were already broken when this
  check was introduced. They are tolerated; any other broken link fails. Fixing a
  baseline entry is encouraged (``--update-baseline`` rewrites the file).
"""
from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "link_check_baseline.json"
DOC_INDEX = ROOT / "docs" / "doc_index.json"
SKIP_SOURCES = ("docs/archive/2026-08", "docs/archive/2026-09")
GENERATED_TARGETS = ("reports/", "outputs/", "data/", "tmp/")
LABEL = r"(?:\\.|[^\[\]\\])*"
DESTINATION = r"(?:\\.|[^\s<>\\()]|\((?:\\.|[^\\()\n])*\))+"
LINK = re.compile(r"(?<!\\)\[" + LABEL + r"\]\([ \t\n]*(<[^<>\n]*>|" + DESTINATION
                  + r")?[ \t\n]*(?:\"[^\"\n]*\"|'[^'\n]*'|\([^\n)]*\))?[ \t\n]*\)")
DEFINITION = re.compile(r"^ {0,3}\[(" + LABEL + r")\]:[ \t]*(?:\n[ \t]*)?(<[^<>\n]*>|(?:\\.|[^\s<>])+)"
                        r"[ \t]*(?:\"[^\"\n]*\"|'[^'\n]*'|\([^\n)]*\))?[ \t]*(?:\n|$)", re.MULTILINE)
REFERENCE = re.compile(r"(?<!\\)\[(" + LABEL + r")\](?:[ \t]*\[(" + LABEL + r")\])?")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CODE = re.compile(r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [p.decode("utf-8") for p in out.split(bytes([0])) if p]


def _mask(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _prose(text: str) -> str:
    """Mask fenced code/comments without changing offsets or line numbers."""
    fence = None
    lines = []
    for line in text.splitlines(keepends=True):
        marker = FENCE.match(line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            lines.append(_mask(line))
        elif marker:
            fence = marker[1]
            lines.append(_mask(line))
        else:
            lines.append(line)
    return re.sub(r"<!--.*?-->", lambda m: _mask(m[0]), "".join(lines), flags=re.DOTALL)


def _unescape(text: str) -> str:
    return html.unescape(re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", text))


def _label(text: str) -> str:
    return " ".join(_unescape(text).split()).casefold()


def iter_links(text: str):
    """Yield source line and destination, including forward reference definitions."""
    prose = _prose(text)
    code_ranges = [match.span() for match in CODE.finditer(prose)]

    def in_code(offset):
        return any(start <= offset < end for start, end in code_ranges)

    definitions = {}
    for match in DEFINITION.finditer(prose):
        if not in_code(match.start()):
            definitions.setdefault(_label(match[1]), match[2])
    prose = DEFINITION.sub(lambda m: _mask(m[0]), prose)
    for match in LINK.finditer(prose):
        if match[1] and not in_code(match.start()):
            yield prose.count("\n", 0, match.start()) + 1, _unescape(match[1].strip("<>"))
    prose = LINK.sub(lambda m: _mask(m[0]), prose)
    for match in REFERENCE.finditer(prose):
        label = _label(match[2] or match[1])
        if label in definitions and not in_code(match.start()):
            yield prose.count("\n", 0, match.start()) + 1, _unescape(definitions[label].strip("<>"))


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
            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
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
