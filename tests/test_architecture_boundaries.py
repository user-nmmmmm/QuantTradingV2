"""Executable import-direction rules for the repository's Python packages."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "core": {
        "config",
        "router",
        "strategies",
        "backtest",
        "live_trading",
        "analysis",
        "dashboard",
    },
    "strategies": {"router", "backtest", "live_trading", "config"},
    "router": {"backtest", "live_trading", "config"},
}

EXEMPT_FILES: set[str] = set()


def _import_roots(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name.split(".", 1)[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module.split(".", 1)[0]))
    return imports


def test_package_import_boundaries() -> None:
    violations = []
    for package, forbidden_roots in FORBIDDEN.items():
        for path in sorted((ROOT / package).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT_FILES:
                continue
            for line, imported_root in _import_roots(path):
                if imported_root in forbidden_roots:
                    violations.append(f"{relative}:{line} imports {imported_root}")
    assert not violations, "Forbidden package dependencies:\n" + "\n".join(violations)
