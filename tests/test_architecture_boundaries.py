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


# Each of these core subpackages is a facade (``__init__.py``) over private
# implementation modules. A member importing its own facade back is how an
# import cycle starts, so the direction is one-way by rule: facade -> member.
FACADE_PACKAGES = (
    "broker",
    "events",
    "exchange",
    "live_broker",
    "metrics",
    "risk",
)

# Two members implement a contract the facade declares, so they must import it.
# Both stay cycle-free because the facade never imports them back:
# - live_broker/safe.py subclasses LiveBroker
# - events/store.py implements the EventStore Protocol
FACADE_IMPORT_ALLOWED = {
    "core/live_broker/safe.py",
    "core/events/store.py",
}


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append((node.lineno, node.module))
    return modules


def test_core_subpackage_members_do_not_import_their_facade() -> None:
    violations = []
    for package in FACADE_PACKAGES:
        facade = f"core.{package}"
        directory = ROOT / "core" / package
        assert (directory / "__init__.py").is_file(), f"{facade} must stay a facade package"
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if path.name == "__init__.py" or relative in FACADE_IMPORT_ALLOWED:
                continue
            for line, module in _imported_modules(path):
                if module == facade:
                    violations.append(f"{relative}:{line} imports its own facade {facade}")
    assert not violations, "Facade import cycles:\n" + "\n".join(violations)
