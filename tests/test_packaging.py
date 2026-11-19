"""Guards for the installable-package layout (infra step 0.2)."""
from __future__ import annotations

import importlib
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# These replay or inspect a *different* source tree on purpose, so they must
# put that tree first on sys.path. Everything else imports the installed project.
SYS_PATH_ALLOWLIST = {
    "scripts/replay_strategy_review_controls.py",
    "scripts/run_portable_tests.py",
    "scripts/run_strategy_review_cross_market.py",
    "scripts/run_strategy_review_meta.py",
    "scripts/strategy_review_worker.py",
}
SYS_PATH_EDIT = re.compile(r"^\s*sys\.path\.(?:insert|append)\(", re.MULTILINE)


def _tracked_python_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT, check=True,
                         capture_output=True).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def test_no_new_sys_path_edits():
    offenders = []
    for name in _tracked_python_files():
        if name in SYS_PATH_ALLOWLIST or name == "tests/test_packaging.py":
            continue
        if SYS_PATH_EDIT.search((ROOT / name).read_text(encoding="utf-8")):
            offenders.append(name)
    assert not offenders, (
        "use `pip install -e .` and import packages directly instead of editing sys.path: "
        + ", ".join(offenders))


def test_console_script_targets_exist():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"].startswith(">=3.11")
    assert project["scripts"], "console scripts are part of the supported interface"
    for command, target in project["scripts"].items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{command} -> {target} is not callable"


def test_declared_packages_cover_top_level_source_directories():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = config["tool"]["setuptools"]["packages"]["find"]["include"]
    declared = {pattern.rstrip("*") for pattern in includes}
    on_disk = {path.parent.name for path in ROOT.glob("*/__init__.py")} - {"tests"}
    on_disk |= {"scripts"}  # namespace package without __init__.py
    assert on_disk <= declared, f"packages missing from pyproject: {sorted(on_disk - declared)}"
