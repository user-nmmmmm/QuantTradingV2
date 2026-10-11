"""Behavioral source identity (infra 0.5): comments/layout are cosmetic, code is not."""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from core.source_identity import (Scope, build_manifest, compare, identify, identity_digest, normalize_python,
                                  receipt, verify_receipt)
from scripts import identity_report


def norm_hash(source: str) -> str:
    return identify("m.py", source.encode()).norm


BASE = textwrap.dedent('''
    """Module doc."""
    import math


    def area(radius, scale=1.0):
        # compute the area
        text = "a # not a comment"
        return math.pi * radius ** 2 * scale
''')


@pytest.mark.parametrize("variant", [
    BASE.replace("# compute the area", "# totally different words"),
    BASE.replace("        # compute the area\n", ""),
    BASE + "\n\n\n# trailing comment\n",
    BASE.replace("radius ** 2 * scale", "radius**2*scale"),
    BASE.replace("\n", "\r\n"),
    BASE.replace("    ", "\t"),
    BASE.replace("def area(radius, scale=1.0):", "def area(\n    radius,\n    scale=1.0,\n):").replace("1.0,\n)", "1.0\n)"),
    BASE.rstrip("\n"),
])
def test_comment_and_layout_edits_keep_the_identity(variant):
    assert norm_hash(variant) == norm_hash(BASE)


@pytest.mark.parametrize("variant", [
    BASE.replace("radius ** 2", "radius ** 3"),
    BASE.replace("scale=1.0", "scale=2.0"),
    BASE.replace("a # not a comment", "a # not a comment!"),      # string content is behavior
    BASE.replace('"""Module doc."""', '"""Other doc."""'),          # docstrings are readable at runtime
    BASE.replace("return math.pi", "return -math.pi"),
    BASE + "\nEXTRA = 1\n",
])
def test_code_and_string_edits_change_the_identity(variant):
    assert norm_hash(variant) != norm_hash(BASE)


def test_multiline_strings_keep_blank_lines_and_fstrings_are_stable():
    first = 'x = """a\n\n\nb"""\ny = f"{x!r:>{10}} # {1 + 1}"\n'
    second = 'x = """a\n\nb"""\ny = f"{x!r:>{10}} # {1 + 1}"\n'
    assert norm_hash(first) != norm_hash(second)
    assert norm_hash(first) == norm_hash(first.replace("y = ", "y   =   "))


GOLDEN_SOURCE = '''
import os  # comment
name = "n"
a = f"{name!r:>{10}} # {1 + 1} {{literal}}"
b = f"""multi
{name}
  line # not a comment"""
c = rf"\\d{name}" + F'{ {"k": 1}["k"] }' + f'{name:%Y-%m-%d}'
d = (lambda x: x)(1) if (n := 2) else b"bytes"
class K:
    """doc"""
    def m(self, *a, **k):
        return f"{self.__class__.__name__}: {a!s}"
'''
# Recorded on Python 3.12.12 and 3.13.2 (identical). Python 3.12 changed how f-strings are tokenized;
# if this fails on another interpreter, the identity is not portable across versions.
GOLDEN_SHA256 = "66b3bbe00d83ccc8417d727829ea9ea1d68f9c2c0948c3cf14e09399c58d202d"


def test_normalized_form_is_identical_across_python_versions():
    import hashlib
    assert hashlib.sha256(normalize_python(GOLDEN_SOURCE).encode()).hexdigest() == GOLDEN_SHA256


def test_unreadable_python_is_hashed_strictly():
    broken = b"x = (1,\n"  # tokenizer rejects an unclosed bracket at EOF
    one, two = identify("b.py", broken), identify("b.py", broken + b"# c\n")
    assert one.scheme == "bytes/v1" and one.norm != two.norm


def test_text_files_normalize_line_endings_but_json_is_byte_exact():
    assert identify("c.yaml", b"a: 1\r\n").norm == identify("c.yaml", b"a: 1\n").norm
    assert identify("c.json", b"{}\r\n").norm != identify("c.json", b"{}\n").norm


# ---- git-backed comparison ---------------------------------------------------------------

def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "a.py").write_text(BASE, encoding="utf-8")
    (tmp_path / "core" / "b.py").write_text("B = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("doc\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


SCOPE = Scope({"core/": [".py"]}, [])


def test_compare_classifies_behavioral_cosmetic_added_removed(repo: Path):
    base = build_manifest(repo, SCOPE, "HEAD")
    (repo / "core" / "a.py").write_text(BASE.replace("# compute the area", "# new words"), encoding="utf-8")
    (repo / "core" / "b.py").write_text("B = 2\n", encoding="utf-8")
    (repo / "core" / "c.py").write_text("C = 1\n", encoding="utf-8")
    (repo / "docs" / "x.md").write_text("changed doc\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change")
    impact = compare(base, build_manifest(repo, SCOPE, "HEAD"))
    assert impact.cosmetic == ["core/a.py"]
    assert impact.behavioral == ["core/b.py"]
    assert impact.added == ["core/c.py"]
    assert impact.changes_identity

    git(repo, "rm", "-q", "core/b.py")
    git(repo, "commit", "-qm", "drop")
    assert compare(base, build_manifest(repo, SCOPE, "HEAD")).removed == ["core/b.py"]


def test_docs_and_comment_only_changes_do_not_change_identity(repo: Path):
    base = build_manifest(repo, SCOPE, "HEAD")
    (repo / "core" / "a.py").write_text("# header\n" + BASE, encoding="utf-8")
    (repo / "docs" / "x.md").write_text("another doc\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cosmetic")
    impact = compare(base, build_manifest(repo, SCOPE, "HEAD"))
    assert not impact.changes_identity and impact.cosmetic == ["core/a.py"]
    assert identity_digest(base) == identity_digest(build_manifest(repo, SCOPE, "HEAD"))


def test_worktree_with_crlf_matches_the_committed_blob(repo: Path):
    base = build_manifest(repo, SCOPE, "HEAD")
    (repo / "core" / "a.py").write_bytes(BASE.replace("\n", "\r\n").encode())
    assert not compare(base, build_manifest(repo, SCOPE, None)).changes_identity


def test_receipt_roundtrip_detects_behavior_changes(repo: Path):
    saved = json.loads(json.dumps(receipt(build_manifest(repo, SCOPE, "HEAD"), ref_sha="abc")))
    assert not verify_receipt(saved, build_manifest(repo, SCOPE, "HEAD")).changes_identity
    (repo / "core" / "b.py").write_text("B = 3\n", encoding="utf-8")
    assert verify_receipt(saved, build_manifest(repo, SCOPE, None)).behavioral == ["core/b.py"]
    saved["scheme"] = "other"
    with pytest.raises(ValueError):
        verify_receipt(saved, build_manifest(repo, SCOPE, None))


def test_report_wording():
    from core.source_identity import Impact
    changed = identity_report.render(Impact(behavioral=["core/a.py"]), base_label="b", head_label="h", scope_files=3)
    same = identity_report.render(Impact(cosmetic=["core/a.py"]), base_label="b", head_label="h", scope_files=3)
    assert "**CHANGED**" in changed and "core/a.py" in changed
    assert "unchanged" in same and "Only comments or layout changed" in same


def test_repository_scope_file_is_valid_and_covers_the_money_path():
    root = Path(__file__).resolve().parents[1]
    scope = Scope.load(root / "config" / "frozen_scope.json")
    for path in ("core/runtime.py", "backtest/engine.py", "live_trading/engine.py", "strategies/base.py",
                 "config/params.yaml", "requirements.lock.txt", "main.py"):
        assert scope.contains(path), path
    for path in ("tests/test_packaging.py", "docs/README.md", "scripts/identity_report.py", "analysis/optimize.py"):
        assert not scope.contains(path), path
