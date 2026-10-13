"""D6: the architecture inventory is generated from the code, and the delivered diagram does not point at ghosts."""
from __future__ import annotations

import json
from pathlib import Path


from scripts import architecture_inventory as inventory

ROOT = Path(__file__).resolve().parents[1]


def test_committed_inventory_and_diagram_paths_are_current():
    assert inventory.check() == []


def test_package_tree_counts_modules_per_subpackage_and_ignores_init_files():
    files = ["core/__init__.py", "core/a.py", "core/market/__init__.py", "core/market/x.py", "core/market/y.py",
             "ops/z.py", "scripts/tool.py", "main.py"]
    assert inventory.package_tree(files, ["core", "ops"]) == {"core": {".": 1, "market": 2}, "ops": {".": 1}}


def test_layers_come_from_the_import_linter_contract():
    order = inventory.layers()
    assert order[0] == "dashboard" and order[-1] == "core"
    text = inventory.render()
    assert 'L0["dashboard"]' in text and "| `core` |" in text


def test_every_move_target_exists_and_no_move_shadows_a_live_path():
    moves = json.loads(inventory.MOVES.read_text(encoding="utf-8"))
    assert moves
    for old, new in moves.items():
        assert (ROOT / new).exists(), new
        assert not (ROOT / old).exists(), f"{old} exists again; drop it from path_moves.json"


def test_a_spec_path_without_a_move_is_reported(tmp_path, monkeypatch):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"components": [{"id": "x", "sources": [{"path": "core/gone.py"}, {"path": "main.py"}]}]}),
                    encoding="utf-8")
    moves = tmp_path / "moves.json"
    moves.write_text(json.dumps({"core/moved.py": "core/does_not_exist.py"}), encoding="utf-8")
    monkeypatch.setattr(inventory, "SPEC", spec)
    monkeypatch.setattr(inventory, "MOVES", moves)
    problems = inventory.diagram_problems()
    assert any("core/gone.py does not exist and has no entry" in p for p in problems)
    assert any("core/does_not_exist.py does not exist" in p for p in problems)
    assert not any("main.py" in p for p in problems)
