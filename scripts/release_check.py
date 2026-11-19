"""Check that a release tag, the ``pyproject.toml`` version and ``CHANGELOG.md`` agree.

Examples
--------
  # before tagging: is the checked-out tree ready to be released as v3.0.0?
  python scripts/release_check.py v3.0.0

  # after tagging: is the tag annotated, consistent, and on the main line?
  python scripts/release_check.py v3.0.0 --verify-tag

Tag format is ``vMAJOR.MINOR.PATCH`` or ``vMAJOR.MINOR.PATCH-rc.N``; the matching
``pyproject.toml`` version is ``MAJOR.MINOR.PATCH`` / ``MAJOR.MINOR.PATCHrcN`` (PEP 440).
See docs/releasing.md. This script only reads; it never creates or pushes a tag.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$", re.MULTILINE)


def tag_to_version(tag: str) -> Optional[str]:
    """PEP 440 version for a release tag, or ``None`` if the tag is not in the release format."""
    match = TAG_RE.match(tag)
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    return f"{major}.{minor}.{patch}" + (f"rc{rc}" if rc is not None else "")


def pyproject_version(text: str) -> Optional[str]:
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def changelog_headings(text: str) -> List[Tuple[str, Optional[str]]]:
    """``(version, date)`` for every ``## [..]`` heading, in file order; ``Unreleased`` has no date."""
    return [(m.group("version"), m.group("date")) for m in HEADING_RE.finditer(text)]


def _sort_key(tag: str) -> Tuple[int, int, int, int, int]:
    major, minor, patch, rc = TAG_RE.match("v" + tag.replace("rc", "-rc.")).groups()
    # a release candidate sorts before its final release
    return (int(major), int(minor), int(patch), 0 if rc is not None else 1, int(rc or 0))


def check_changelog(text: str) -> List[str]:
    problems: List[str] = []
    headings = changelog_headings(text)
    if not headings or headings[0][0] != "Unreleased":
        problems.append("CHANGELOG.md must start its entries with '## [Unreleased]'")
    releases = [(v, d) for v, d in headings if v != "Unreleased"]
    for version, date in releases:
        if date is None or tag_to_version("v" + version.replace("rc", "-rc.")) != version:
            problems.append(f"CHANGELOG.md heading [{version}] must read '## [X.Y.Z] - YYYY-MM-DD'")
    valid = [v for v, d in releases if d is not None and tag_to_version("v" + v.replace("rc", "-rc.")) == v]
    if valid != sorted(valid, key=_sort_key, reverse=True):
        problems.append("CHANGELOG.md releases must be listed newest first")
    if len(set(valid)) != len(valid):
        problems.append("CHANGELOG.md lists a version twice")
    return problems


def check_release(tag: str, pyproject_text: str, changelog_text: str) -> List[str]:
    version = tag_to_version(tag)
    if version is None:
        return [f"tag {tag!r} does not match vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N"]
    problems = []
    declared = pyproject_version(pyproject_text)
    if declared != version:
        problems.append(f"pyproject.toml version is {declared!r}, tag {tag} needs {version!r}")
    problems.extend(check_changelog(changelog_text))
    if not any(v == version and d for v, d in changelog_headings(changelog_text)):
        problems.append(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section")
    return problems


def _git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _show(ref: str, path: str, root: Path) -> str:
    return subprocess.run(["git", "cat-file", "blob", f"{ref}:{path}"], cwd=root, check=True,
                          capture_output=True).stdout.decode("utf-8")


def verify_tag(tag: str, *, root: Path = ROOT, main_ref: str = "origin/main") -> List[str]:
    """Problems with an existing tag: not annotated, inconsistent content, or not on the main line."""
    try:
        kind = _git("cat-file", "-t", f"refs/tags/{tag}", root=root)
    except subprocess.CalledProcessError:
        return [f"tag {tag} does not exist"]
    problems = [] if kind == "tag" else [f"tag {tag} is lightweight; create it with 'git tag -a'"]
    try:
        problems += check_release(tag, _show(f"{tag}^{{commit}}", "pyproject.toml", root),
                                  _show(f"{tag}^{{commit}}", "CHANGELOG.md", root))
    except subprocess.CalledProcessError:
        problems.append(f"tag {tag} does not contain pyproject.toml and CHANGELOG.md")
    reference = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "--end-of-options",
                                f"{main_ref}^{{commit}}"], cwd=root, capture_output=True, text=True)
    if reference.returncode:
        problems.append(f"required main reference is missing or not a commit: {main_ref}")
    else:
        ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", f"{tag}^{{commit}}",
                                   reference.stdout.strip()], cwd=root, capture_output=True)
        if ancestry.returncode == 1:
            problems.append(f"tag {tag} is not reachable from {main_ref}")
        elif ancestry.returncode:
            problems.append(f"cannot verify tag {tag} ancestry against {main_ref}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="release tag, e.g. v3.0.0")
    parser.add_argument("--verify-tag", action="store_true",
                        help="check the existing tag instead of the working tree")
    parser.add_argument("--main-ref", default="origin/main", help="ref the tag must be reachable from")
    args = parser.parse_args(argv)
    if args.verify_tag:
        problems = verify_tag(args.tag, main_ref=args.main_ref)
    else:
        problems = check_release(args.tag, (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                                 (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if not problems:
        print(f"release {args.tag}: consistent")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
