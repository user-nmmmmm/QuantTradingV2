"""B5 (ADR 0019): every script has a lifecycle, and the maintained tool chain does not depend on one-off ones."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLASSIFICATION = json.loads((SCRIPTS / "classification.json").read_text(encoding="utf-8"))
LIFECYCLE = CLASSIFICATION["scripts"]


def _script_files() -> set[str]:
    return {p.name for p in SCRIPTS.iterdir() if p.suffix in (".py", ".ps1")}


def test_every_script_is_classified_exactly_once():
    assert set(LIFECYCLE) == _script_files(), set(LIFECYCLE) ^ _script_files()
    assert set(LIFECYCLE.values()) <= {"supported", "research", "historical"}


def test_classification_matches_the_readme_sections():
    index = (SCRIPTS / "README.md").read_text(encoding="utf-8")
    sections = re.split(r"^## ", index, flags=re.M)[1:]
    expected = {int(k): v for k, v in CLASSIFICATION["readme_sections"].items()}
    assert len(sections) == len(expected), "a README section was added or removed: update classification.json"
    for position, section in enumerate(sections):
        for name in re.findall(r"\[`([\w\-]+\.(?:py|ps1))`\]", section):
            if name in LIFECYCLE:
                wanted = CLASSIFICATION["overrides"].get(name, expected[position])
                assert LIFECYCLE[name] == wanted, f"{name}: README section says {wanted}, classification says {LIFECYCLE[name]}"


def _scripts_referenced_by(name: str) -> set[str]:
    source = (SCRIPTS / name).read_text(encoding="utf-8")
    modules = {f[:-3]: f for f in _script_files() if f.endswith(".py")}
    refs: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module == "scripts":
                refs |= {alias.name for alias in node.names}
            elif node.module and node.module.startswith("scripts."):
                refs.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            refs |= {a.name.split(".")[1] for a in node.names if a.name.startswith("scripts.")}
    refs |= set(re.findall(r"""["'](?:scripts/)?([\w\-]+)\.py["']""", source))
    return {modules[r] for r in refs if r in modules and modules[r] != name}


def test_the_maintained_tool_chain_does_not_depend_on_research_or_historical_scripts():
    allowed = {(a, b) for a, b, _ in CLASSIFICATION["allowed_dependencies"]}
    violations = []
    for name, lifecycle in LIFECYCLE.items():
        if lifecycle != "supported" or not name.endswith(".py"):
            continue
        for target in _scripts_referenced_by(name):
            if LIFECYCLE[target] != "supported" and (name, target) not in allowed:
                violations.append(f"{name} -> {target} ({LIFECYCLE[target]})")
    assert not violations, "supported scripts must not depend on one-off ones:\n" + "\n".join(violations)


def test_allowed_dependencies_are_real():
    for source, target, reason in CLASSIFICATION["allowed_dependencies"]:
        assert reason
        assert target in _scripts_referenced_by(source), f"{source} no longer references {target}; remove the exception"


def test_ci_and_dev_tasks_only_run_supported_scripts():
    text = (ROOT / "scripts" / "dev.py").read_text(encoding="utf-8") + (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    for name in set(re.findall(r"scripts/([\w\-]+\.(?:py|ps1))", text)):
        if name in LIFECYCLE:
            assert LIFECYCLE[name] == "supported", f"CI/dev runs {name} ({LIFECYCLE[name]})"
