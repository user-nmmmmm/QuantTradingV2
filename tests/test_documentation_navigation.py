"""Current navigation follows tracked layout and preserves old evidence destinations."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import architecture_inventory as inventory
from scripts import check_markdown_links as links

ROOT = Path(__file__).resolve().parents[1]


def test_readme_lists_every_direct_runtime_pin_including_excel():
    expected = dict(line.split("==") for line in (ROOT / "requirements.txt").read_text().splitlines() if "==" in line)
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = dict(re.findall(r"^\| `([^`]+)` \| ([\d.]+) \|", text, re.MULTILINE))
    assert documented == expected


def test_navigation_covers_current_packages_and_core_subpackages():
    assert inventory.navigation_problems() == []


@pytest.mark.parametrize("missing", ["ops/", "core/analytics/", "core/foundation/"])
def test_navigation_gate_rejects_omitting_a_real_package(tmp_path, monkeypatch, missing):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"]({missing})" in text
    (tmp_path / "README.md").write_text(text.replace(f"]({missing})", "](core/)"), encoding="utf-8")
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "tracked_python", lambda: ["ops/health.py", "core/analytics/indicators.py", "core/foundation/runtime.py"])
    monkeypatch.setattr(inventory, "_config", lambda: {"root_packages": ["ops", "core"]})
    assert any(missing.rstrip("/") in problem for problem in inventory.navigation_problems())


@pytest.mark.parametrize("text", [
    "no navigation\n",
    "<!-- module-navigation:end -->\n<!-- module-navigation:start -->",
    "<!-- module-navigation:start --><!-- module-navigation:start --><!-- module-navigation:end -->",
])
def test_navigation_gate_rejects_missing_or_duplicate_block(tmp_path, monkeypatch, text):
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    assert inventory.navigation_problems()


def test_navigation_gate_rejects_a_moved_source_path(tmp_path, monkeypatch):
    config, files = inventory._config(), inventory.tracked_python()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    text = text.replace("<!-- module-navigation:end -->", "[old](core/indicators.py)\n<!-- module-navigation:end -->")
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "_config", lambda: config)
    monkeypatch.setattr(inventory, "tracked_python", lambda: files)
    assert any("untracked source core/indicators.py" in problem for problem in inventory.navigation_problems())


def test_navigation_gate_rejects_a_new_undocumented_core_slice(monkeypatch):
    files = inventory.tracked_python()
    monkeypatch.setattr(inventory, "tracked_python", lambda: files + ["core/new_slice/module.py"])
    assert any("omits core/new_slice" in problem for problem in inventory.navigation_problems())


def test_navigation_gate_rejects_stale_module_document_headings(tmp_path, monkeypatch):
    config, files = inventory._config(), inventory.tracked_python()
    (tmp_path / "README.md").write_text((ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
    module_docs = tmp_path / "docs/modules"
    module_docs.mkdir(parents=True)
    (module_docs / "core.md").write_text("### `core/indicators.py` - moved\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "_config", lambda: config)
    monkeypatch.setattr(inventory, "tracked_python", lambda: files)
    assert any("heading points at missing core/indicators.py" in problem for problem in inventory.navigation_problems())


def test_old_report_names_have_usable_compatibility_links():
    moved = json.loads((ROOT / "docs/doc_index.json").read_text(encoding="utf-8"))["moved"]
    receipt = json.loads((ROOT / "docs/research/paper_application_acceptance_20261003.json").read_text(encoding="utf-8"))
    # The historical receipt still names its original report; restore navigation
    # without changing that evidence or the protected archive's source bytes.
    assert receipt["report"] == "paper_application_execution_20261003.md"
    old_paths = ["docs/research/" + receipt["report"], "docs/research/paper_continuation_20261003.md",
                 "docs/missing_capabilities_acceptance.md"]
    for old in old_paths:
        stub = ROOT / old
        assert stub.is_file(), old
        destinations = [target.split("#", 1)[0] for _, target in links.iter_links(stub.read_text(encoding="utf-8"))]
        assert any((stub.parent / target).resolve() == (ROOT / moved[old]).resolve() for target in destinations), old
        for _, target in links.iter_links(stub.read_text(encoding="utf-8")):
            path, separator, anchor = target.partition("#")
            if separator:
                headings = re.findall(r"^#{1,6} (.+)$", (stub.parent / path).read_text(encoding="utf-8"), re.MULTILINE)
                anchors = {re.sub(r"[^\w -]", "", title).lower().replace(" ", "-") for title in headings}
                assert anchor in anchors


def test_dashboard_links_use_current_section_names():
    text = (ROOT / "dashboard/README.md").read_text(encoding="utf-8")
    prose = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    assert "FRONTEND.md" not in prose and "DESIGN.md" not in prose
    index = (ROOT / "docs/modules/README.md").read_text(encoding="utf-8")
    assert "FRONTEND.md" not in index
