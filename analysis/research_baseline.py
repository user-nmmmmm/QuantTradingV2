"""Current-state, environment and baseline-diff capture for the paper research programme (PR25-01).

Records what is actually present (HEAD, uncommitted and untracked files, interpreter and numeric stack,
CI interpreter versions, source identity) without running trading or research code. A baseline is a
description of the starting point, not an acceptance result.
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from analysis.research_evidence import source_identity
from core.foundation.reproducibility import canonical_json, sha256_bytes

SCHEMA = "paper-research-baseline/v1"
PACKAGES = ("numpy", "pandas", "scipy", "scikit-learn", "torch", "pytest")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_state(root: Path) -> dict[str, Any]:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    tracked = sorted(line[3:] for line in status if not line.startswith("??"))
    untracked = sorted(line[3:] for line in status if line.startswith("??"))
    return {"head": _git(root, "rev-parse", "HEAD").strip(), "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "tracked_changes": tracked, "untracked_files": untracked}


def _package_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def ci_python_versions(root: Path) -> list[str]:
    path = root / ".github/workflows/tests.yml"
    if not path.is_file():
        return []
    return sorted(set(re.findall(r"""["'](3\.\d+)["']""", path.read_text(encoding="utf-8"))))


def environment_state(root: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"python": platform.python_version(), "implementation": platform.python_implementation(),
                             "executable": sys.executable, "platform": platform.platform(), "machine": platform.machine(),
                             "packages": {name: _package_version(name) for name in PACKAGES}, "blas": None,
                             "ci_python_versions": ci_python_versions(root)}
    try:
        import numpy

        config = numpy.__config__.CONFIG if hasattr(numpy.__config__, "CONFIG") else None
        deps = (config or {}).get("Build Dependencies", {})
        state["blas"] = {"name": deps.get("blas", {}).get("name"), "version": deps.get("blas", {}).get("version")}
    except Exception as exc:  # noqa: BLE001 - environment capture must not fail the whole run
        state["blas_error"] = repr(exc)
    local_minor = ".".join(state["python"].split(".")[:2])
    state["python_matches_ci"] = local_minor in state["ci_python_versions"] if state["ci_python_versions"] else None
    return state


def current_state(root: Path, *, known_failures: list[str] | None = None) -> dict[str, Any]:
    identity = source_identity(root)
    return {"schema": SCHEMA, "captured_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
            "git": git_state(root),
            "source_identity": {"file_count": len(identity), "sha256": sha256_bytes(canonical_json(identity).encode()),
                                "files": identity},
            "known_failures": list(known_failures or []),
            "interpretation": "starting-point description only; not an acceptance or admission result"}


def diff_states(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two ``current_state`` records; ``previous=None`` is reported, not treated as unchanged."""
    if previous is None:
        return {"schema": SCHEMA, "status": "no_previous_baseline"}
    old, new = previous["source_identity"]["files"], current["source_identity"]["files"]
    return {"schema": SCHEMA, "status": "compared",
            "head_changed": previous["git"]["head"] != current["git"]["head"],
            "source_added": sorted(set(new) - set(old)), "source_removed": sorted(set(old) - set(new)),
            "source_changed": sorted(p for p in set(old) & set(new) if old[p] != new[p]),
            "untracked_added": sorted(set(current["git"]["untracked_files"]) - set(previous["git"]["untracked_files"])),
            "untracked_removed": sorted(set(previous["git"]["untracked_files"]) - set(current["git"]["untracked_files"]))}
