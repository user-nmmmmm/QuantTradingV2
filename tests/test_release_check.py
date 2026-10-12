"""Release convention (infra A3) and release-bound identity (infra A4)."""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from core.source_identity import BINDING_SCHEMA, Scope, bind_release, build_manifest, scope_sha256, verify_binding
from scripts import identity_report, release_check
from scripts.release_check import (check_changelog, check_release, pyproject_version, tag_to_version,
                                   verify_tag)

ROOT = Path(__file__).resolve().parents[1]

CHANGELOG = textwrap.dedent("""\
    # Changelog

    ## [Unreleased]

    ## [1.2.0] - 2026-10-20

    ### Added
    - x

    ## [1.1.0rc1] - 2026-10-10

    ## [1.0.0] - 2026-10-01
""")
PYPROJECT = '[project]\nname = "x"\nversion = "1.2.0"\n'


@pytest.mark.parametrize("tag, version", [("v3.0.0", "3.0.0"), ("v1.2.3-rc.4", "1.2.3rc4"), ("v10.0.1", "10.0.1")])
def test_tag_to_version(tag, version):
    assert tag_to_version(tag) == version


@pytest.mark.parametrize("tag", ["3.0.0", "v3.0", "v3.0.0.1", "v3.0.0-rc", "release-1", "v3.0.0-beta.1", "vx.y.z"])
def test_malformed_tags_are_rejected(tag):
    assert tag_to_version(tag) is None
    assert check_release(tag, PYPROJECT, CHANGELOG)


def test_a_consistent_release_has_no_problems():
    assert check_release("v1.2.0", PYPROJECT, CHANGELOG) == []
    assert pyproject_version(PYPROJECT) == "1.2.0"


def test_version_and_changelog_mismatches_are_reported():
    assert any("pyproject.toml version" in p for p in check_release("v1.3.0", PYPROJECT, CHANGELOG))
    assert any("no '## [1.3.0]" in p for p in check_release("v1.3.0", PYPROJECT.replace("1.2.0", "1.3.0"), CHANGELOG))
    assert any("Unreleased" in p for p in check_changelog(CHANGELOG.replace("[Unreleased]", "[Next]")))
    assert any("newest first" in p for p in check_changelog(CHANGELOG.replace("1.2.0", "0.9.0")))
    assert any("YYYY-MM-DD" in p for p in check_changelog(CHANGELOG.replace("[1.2.0] - 2026-10-20", "[1.2.0]")))
    assert any("twice" in p for p in check_changelog(CHANGELOG + "\n## [1.0.0] - 2026-09-01\n"))


def test_a_release_candidate_sorts_before_its_final_release():
    assert check_changelog("## [Unreleased]\n\n## [1.0.0] - 2026-10-02\n\n## [1.0.0rc1] - 2026-10-01\n") == []
    assert check_changelog("## [Unreleased]\n\n## [1.0.0rc1] - 2026-10-02\n\n## [1.0.0] - 2026-10-01\n")


def test_repository_changelog_is_well_formed_and_not_ahead_of_the_package_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert check_changelog(text) == []
    newest = next(v for v, d in release_check.changelog_headings(text) if d)
    declared = pyproject_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared is not None
    key = release_check._sort_key
    assert key(declared) >= key(newest), (declared, newest)


# ---- git-backed: tags and bindings ---------------------------------------------------------

def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


SCOPE_JSON = json.dumps({"schema": "frozen-scope/v1", "directories": [{"prefix": "core/", "suffixes": [".py"]}],
                         "files": []})


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "core").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "core" / "a.py").write_text("import os\nA = 1\n", encoding="utf-8")
    (tmp_path / "config" / "frozen_scope.json").write_text(SCOPE_JSON, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "release")
    return tmp_path


def test_verify_tag_accepts_an_annotated_consistent_tag_on_the_main_line(repo: Path):
    git(repo, "tag", "-a", "v1.2.0", "-m", "Release 1.2.0")
    assert verify_tag("v1.2.0", root=repo, main_ref="main") == []


def test_verify_tag_rejects_missing_lightweight_inconsistent_and_off_main_tags(repo: Path):
    assert verify_tag("v1.2.0", root=repo, main_ref="main") == ["tag v1.2.0 does not exist"]
    git(repo, "tag", "v1.2.0")
    assert any("lightweight" in p for p in verify_tag("v1.2.0", root=repo, main_ref="main"))
    git(repo, "tag", "-a", "v1.9.0", "-m", "wrong")
    assert any("pyproject.toml version" in p for p in verify_tag("v1.9.0", root=repo, main_ref="main"))
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "core" / "b.py").write_text("B = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "side")
    git(repo, "tag", "-a", "v1.2.1", "-m", "side")
    assert any("not reachable" in p for p in verify_tag("v1.2.1", root=repo, main_ref="main"))


def current(repo: Path, ref=None):
    scope_bytes = (repo / "config" / "frozen_scope.json").read_bytes()
    return build_manifest(repo, Scope.from_json(scope_bytes.decode()), ref), scope_sha256(scope_bytes)


def test_binding_roundtrip_and_what_it_detects(repo: Path):
    git(repo, "tag", "-a", "v1.2.0", "-m", "Release 1.2.0")
    saved = json.loads(json.dumps(bind_release(repo, "v1.2.0")))
    assert saved["schema"] == BINDING_SCHEMA and saved["tag"] == "v1.2.0"
    assert saved["tag_commit"] == git(repo, "rev-parse", "v1.2.0^{commit}")

    head, scope_sha = current(repo)
    problems, impact = verify_binding(saved, repo, head, scope_sha)
    assert problems == [] and not impact.changes_identity

    # comment-only edit: identity holds; behavior edit: it does not
    (repo / "core" / "a.py").write_text("# note\nimport os\nA = 1\n", encoding="utf-8")
    assert not verify_binding(saved, repo, *current(repo))[1].changes_identity
    (repo / "core" / "a.py").write_text("import os\nA = 2\n", encoding="utf-8")
    assert verify_binding(saved, repo, *current(repo))[1].behavioral == ["core/a.py"]

    # a pure move is visible as a move, not as a deletion plus an addition
    (repo / "core" / "a.py").write_text("import os\nA = 1\n", encoding="utf-8")
    (repo / "core" / "sub").mkdir()
    (repo / "core" / "a.py").rename(repo / "core" / "sub" / "a.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "move")
    problems, impact = verify_binding(saved, repo, *current(repo, "HEAD"))
    assert problems == [] and impact.moved == [("core/a.py", "core/sub/a.py")] and not impact.changes_behavior


def test_binding_detects_a_moved_tag_and_a_changed_scope_declaration(repo: Path):
    git(repo, "tag", "-a", "v1.2.0", "-m", "Release 1.2.0")
    saved = bind_release(repo, "v1.2.0")
    (repo / "core" / "b.py").write_text("B = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more")
    git(repo, "tag", "-fa", "v1.2.0", "-m", "moved")
    head, scope_sha = current(repo, "HEAD")
    problems, impact = verify_binding(saved, repo, head, scope_sha)
    assert any("now points to" in p for p in problems) and impact.added == ["core/b.py"]

    problems, _ = verify_binding(saved, repo, head, scope_sha256(b"{}"))
    assert any("scope declaration differs" in p for p in problems)

    git(repo, "tag", "-d", "v1.2.0")
    assert any("does not exist" in p for p in verify_binding(saved, repo, head, scope_sha)[0])


def test_binding_refuses_missing_and_lightweight_tags_and_unknown_schemas(repo: Path):
    with pytest.raises(ValueError, match="does not exist"):
        bind_release(repo, "v9.9.9")
    git(repo, "tag", "v1.2.0")
    with pytest.raises(ValueError, match="lightweight"):
        bind_release(repo, "v1.2.0")
    head, scope_sha = current(repo)
    with pytest.raises(ValueError):
        verify_binding({"schema": "other"}, repo, head, scope_sha)


def test_cli_writes_a_binding_once_and_verifies_it(repo: Path, monkeypatch, tmp_path: Path, capsys):
    git(repo, "tag", "-a", "v1.2.0", "-m", "Release 1.2.0")
    monkeypatch.setattr(identity_report, "ROOT", repo)
    monkeypatch.setattr(identity_report, "SCOPE_FILE", repo / "config" / "frozen_scope.json")
    out = tmp_path / "binding.json"
    assert identity_report.main(["--bind-release", "v1.2.0", "--write-binding", str(out)]) == 0
    assert identity_report.main(["--bind-release", "v1.2.0", "--write-binding", str(out)]) == 2   # no overwrite
    assert identity_report.main(["--verify-binding", str(out), "--head", "HEAD", "--fail-on-change",
                                 "--scope", str(repo / "config" / "frozen_scope.json")]) == 0
    (repo / "core" / "a.py").write_text("import os\nA = 5\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "behavior")
    assert identity_report.main(["--verify-binding", str(out), "--head", "HEAD", "--fail-on-change",
                                 "--scope", str(repo / "config" / "frozen_scope.json")]) == 1
    assert identity_report.main(["--bind-release", "v0.0.0", "--write-binding", str(tmp_path / "x.json")]) == 2
