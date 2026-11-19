"""Exact local source-byte identities shared across package layers.

This is distinct from the behavioral identity used by release/frozen namespaces.
The declared scope includes untracked research/runtime inputs and rejects escapes.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.foundation.reproducibility import sha256_file


_SOURCE_DIRECTORIES = (
    "analysis",
    "backtest",
    "cli",
    "composition",
    "config",
    "core",
    "data",
    "live_trading",
    "ops",
    "research",
    "router",
    "scripts",
    "signal_meta",
    "strategies",
    "tests",
)


_SOURCE_SUFFIXES = {
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".lock",
    ".sha256",
    ".joblib",
    ".pkl",
    ".pickle",
    ".pt",
    ".pth",
    ".onnx",
    ".bin",
    ".npz",
    ".npy",
}


_IGNORED_SOURCE_DIRECTORIES = {
    "__pycache__",
    ".git",
    ".venv",
    ".venv-linux",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _source_path(root, value):
    """Keep the declared path while refusing aliases that escape the root."""
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("source path must not contain parent traversal")
    path = path if path.is_absolute() else root / path
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("source path escapes its root")
    return path


def _source_walk_error(error):
    raise error


def _source_files(root, directory):
    if directory.resolve() != directory:
        raise ValueError("symbolic source directory requires its resolved path")
    for parent, dirs, files in os.walk(directory, followlinks=False, onerror=_source_walk_error):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_SOURCE_DIRECTORIES)
        for name in dirs:
            child = _source_path(root, Path(parent) / name)
            if child.resolve() != child:
                raise ValueError("symbolic source directory requires its resolved path")
        for name in sorted(files):
            yield _source_path(root, Path(parent) / name)


def _capture_sources(root, paths, *, allow_missing=False):
    selected: set[Path] = set()
    if paths is not None:
        for value in paths:
            path = _source_path(root, value)
            if path.is_dir():
                selected.update(_source_files(root, path))
            elif path.is_file():
                selected.add(path)
            elif not allow_missing:
                raise FileNotFoundError(f"declared source is missing: {value}")
    else:
        for name in _SOURCE_DIRECTORIES:
            directory = _source_path(root, name)
            if directory.is_dir():
                selected.update(
                    p
                    for p in _source_files(root, directory)
                    if p.suffix.lower() in _SOURCE_SUFFIXES
                    and (name != "data" or p.suffix.lower() == ".py")
                )
        selected.update(
            p
            for pattern in ("requirements*", "*lock*", "*.toml")
            for p in root.glob(pattern)
            if p.is_file()
        )
        selected.update(
            root / name
            for name in ("main.py", "run_live.py", "resolve_live_order.py", ".python-version")
            if (root / name).is_file()
        )
        workflows = _source_path(root, ".github/workflows")
        if workflows.is_dir():
            selected.update(
                p for p in _source_files(root, workflows) if p.suffix.lower() in {".yaml", ".yml"}
            )
        # Protocols are inputs; newly generated acceptance reports are outputs.
        protocols = _source_path(root, "docs/research")
        if protocols.is_dir():
            selected.update(
                p
                for p in _source_files(root, protocols)
                if ("protocol" in p.stem or "contract" in p.stem)
                and p.suffix.lower() in {".md", ".json", ".yaml", ".yml"}
            )
    return {
        p.relative_to(root).as_posix(): sha256_file(_source_path(root, p)) for p in sorted(selected)
    }


def source_identity(root=None, *, paths=None):
    """Hash the current research closure without relying on Git tracking.

    Explicit ``paths`` declare files or directories; directories include every
    non-cache file, including binary models and untracked sources. Missing
    paths and paths escaping the root refuse registration. The default covers
    research/runtime code, configuration, tests, protocols and dependency locks;
    it does not claim automatic dependency discovery or global search history.
    """
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    if not root.is_dir():
        raise ValueError("source root must be an existing directory")
    if isinstance(paths, (str, bytes, Path)):
        raise ValueError("source paths must be a sequence, not a single path")
    return _capture_sources(root, paths)
