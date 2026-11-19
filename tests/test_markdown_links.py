"""Markdown link guard and the moved-documents index (infra 0.5)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_markdown_links as links
from scripts import verify_roadmap_policy as policy

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch) -> Path:
    for rel, text in {
        "docs/a.md": "[ok](b.md) [bad](missing.md) [moved](old.md) [anchor](b.md#x) [web](https://x.y/z.md)\n"
                     "[gen](../reports/out.json) `[code](nope.md)`\n```\n[fenced](nope.md)\n```\n",
        "docs/b.md": "# b\n[dir](sub/) [up](../README.md)\n",
        "docs/new.md": "new\n",
        "docs/sub/c.md": "c\n",
        "docs/archive/2026-09-x/frozen.md": "[dead](gone.md)\n",
        "README.md": "root\n",
        "docs/doc_index.json": json.dumps({"moved": {"docs/old.md": "docs/new.md"}}),
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(links, "ROOT", tmp_path)
    monkeypatch.setattr(links, "DOC_INDEX", tmp_path / "docs" / "doc_index.json")
    monkeypatch.setattr(links, "BASELINE", tmp_path / "docs" / "link_check_baseline.json")
    monkeypatch.setattr(links, "tracked_files", lambda: sorted(
        str(p.relative_to(tmp_path)).replace("\\", "/") for p in tmp_path.rglob("*") if p.is_file()))
    return tmp_path


def test_finds_only_real_broken_links_and_hints_moves(tree: Path):
    broken = {(e["target"], e.get("moved_to")) for e in links.find_broken(links.tracked_files())}
    assert broken == {("docs/missing.md", None), ("docs/old.md", "docs/new.md")}


def test_baseline_tolerates_known_links_and_fails_on_new_ones(tree: Path, capsys):
    assert links.main([]) == 1
    assert links.main(["--update-baseline"]) == 0
    assert links.main([]) == 0
    (tree / "docs" / "d.md").write_text("[fresh](nowhere.md)\n", encoding="utf-8")
    assert links.main([]) == 1
    assert "docs/nowhere.md" in capsys.readouterr().out


def test_policy_verifier_resolves_moved_documents(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "merged.md").write_text("S1-3 stays here\n", encoding="utf-8")
    (tmp_path / "docs" / "doc_index.json").write_text(
        json.dumps({"moved": {"docs/original.md": "docs/merged.md"}}), encoding="utf-8")
    assert policy.file_at(tmp_path, "docs/original.md") == (tmp_path / "docs" / "merged.md").resolve()
    with pytest.raises(ValueError):
        policy.file_at(tmp_path, "docs/never_existed.md")


def test_repository_has_no_new_broken_markdown_links():
    assert links.main([]) == 0


def test_doc_index_targets_exist_and_do_not_chain():
    moved = json.loads((ROOT / "docs" / "doc_index.json").read_text(encoding="utf-8"))["moved"]
    assert moved
    for old, new in moved.items():
        assert (ROOT / new).is_file(), f"{old} -> {new} does not exist"
        assert new not in moved, f"{old} points at {new}, which was moved again; update the entry"


@pytest.mark.parametrize("text, target", [
    ("[guide][manual]\n\n[manual]: missing.md\n", "docs/missing.md"),
    ("[manual][]\n\n[manual]: missing.md 'title'\n", "docs/missing.md"),
    ("[manual]\n\n[manual]: <missing.md> \"title\"\n", "docs/missing.md"),
    ("[guide][MULTI  Word]\n\n[multi word]: missing.md\n", "docs/missing.md"),
    ("[guide][multi\n word]\n\n[multi word]: missing.md\n", "docs/missing.md"),
    ("![image][picture]\n\n[picture]: missing.png\n", "docs/missing.png"),
    ("[guide][manual]\n\n[manual]:\n  missing.md\n", "docs/missing.md"),
    ("[guide][manual]\n\n[manual]: missing%20file.md#section\n", "docs/missing file.md"),
    ("[guide](<missing file.md> \"title\")\n", "docs/missing file.md"),
    ("[guide](missing(file).md)\n", "docs/missing(file).md"),
    ("[`manual`][]\n\n[`manual`]: missing.md\n", "docs/missing.md"),
])
def test_reference_and_inline_destinations_are_checked(tree, text, target):
    source = tree / "docs/reference.md"
    source.write_text(text, encoding="utf-8")
    broken = [entry for entry in links.find_broken(links.tracked_files()) if entry["source"] == "docs/reference.md"]
    assert [(entry["line"], entry["target"]) for entry in broken] == [(1, target)]


@pytest.mark.parametrize("text", [
    "[guide][manual]\n[manual]: b.md\n",
    "[guide][manual]\n[manual]: b.md\n[MANUAL]: missing.md\n",
    "[guide][manual]\n[manual]: https://example.com/missing.md\n",
    "[guide][manual]\n[manual]: #local-anchor\n",
    "[guide][manual]\n[manual]: ../outputs/generated.md\n",
    "`[guide][manual]`\n[manual]: missing.md\n",
    "``[guide] `example` [manual]``\n[manual]: missing.md\n",
    "````md\n```\n[guide][manual]\n````\n[manual]: missing.md\n",
    "~~~md\n```\n[guide][manual]\n~~~\n[manual]: missing.md\n",
    "<!-- [guide][manual] -->\n[manual]: missing.md\n",
    "[ordinary bracketed prose]\n",
    "[unused]: missing.md\n",
    "[`other`]\n\n[`manual`]: missing.md\n",
])
def test_valid_references_and_nonprose_do_not_create_false_broken_links(tree, text):
    (tree / "docs/reference.md").write_text(text, encoding="utf-8")
    assert not [entry for entry in links.find_broken(links.tracked_files()) if entry["source"] == "docs/reference.md"]


def test_reference_destination_failure_reaches_cli_and_moved_hint(tree, capsys):
    (tree / "docs/a.md").write_text("[guide][manual]\n\n[manual]: old.md\n", encoding="utf-8")
    assert links.main([]) == 1
    assert "docs/a.md:1: broken link -> docs/old.md (moved to docs/new.md)" in capsys.readouterr().out
    (tree / "docs/a.md").write_text("[guide][manual]\n\n[manual]: new.md\n", encoding="utf-8")
    assert links.main([]) == 0


def test_indented_list_continuations_cannot_hide_reference_links(tree):
    (tree / "docs/reference.md").write_text("- item\n    [guide][manual]\n\n[manual]: missing.md\n", encoding="utf-8")
    broken = [entry for entry in links.find_broken(links.tracked_files()) if entry["source"] == "docs/reference.md"]
    assert [(entry["line"], entry["target"]) for entry in broken] == [(2, "docs/missing.md")]
