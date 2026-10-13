"""Generate and check the package inventory in ``docs/architecture_inventory.md`` (infrastructure roadmap D6).

The inventory is derived from the code, never edited by hand: the layer order comes from the import-linter
contract in ``pyproject.toml`` and the package tree from the git-tracked Python files.

  python scripts/architecture_inventory.py --write   # regenerate docs/architecture_inventory.md
  python scripts/architecture_inventory.py --check   # CI / dev.py check: the committed file is current, and the
                                                     # delivered diagram under 架构图/ still resolves to real files

The delivered diagram (``架构图/``) is a rendered snapshot of an earlier revision. It is not re-rendered here (the
renderer is an external tool and its visual-check receipt is bound to the HTML bytes). Instead ``--check`` verifies
that every source path named in its spec exists, or is listed in ``架构图/path_moves.json`` with a target that does.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "architecture_inventory.md"
DIAGRAM_DIR = ROOT / "架构图"
SPEC = DIAGRAM_DIR / "quant_trading.architecture.json"
MOVES = DIAGRAM_DIR / "path_moves.json"
TOP_LEVEL_FILES = ("main.py", "run_live.py", "resolve_live_order.py")


def _config() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["importlinter"]


def tracked_python() -> List[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT, capture_output=True, check=True).stdout
    return sorted(name for name in out.decode("utf-8").split("\0") if name)


def layers() -> List[str]:
    """Layer entries from highest to lowest, as written in the layered-architecture contract."""
    for contract in _config()["contracts"]:
        if contract.get("type") == "layers":
            return list(contract["layers"])
    raise SystemExit("no layers contract in pyproject.toml")


def package_tree(files: List[str], packages: List[str]) -> Dict[str, Dict[str, int]]:
    """package -> {sub-package or '.': module count}, ``__init__.py`` files not counted."""
    tree: Dict[str, Dict[str, int]] = {package: defaultdict(int) for package in packages}
    for name in files:
        parts = name.split("/")
        if parts[0] not in tree or parts[-1] == "__init__.py":
            continue
        tree[parts[0]]["." if len(parts) == 2 else parts[1]] += 1
    return {package: dict(sorted(counts.items())) for package, counts in tree.items()}


def render() -> str:
    config = _config()
    packages = list(config["root_packages"])
    files = tracked_python()
    tree = package_tree(files, packages)
    order = layers()
    lines = [
        "# 架构清单（自动生成）",
        "",
        "> 本页由 `python scripts/architecture_inventory.py --write` 从代码生成，CI 用 `--check` 保证它与代码一致，**不要手工编辑**。",
        "> 分层顺序来自 `pyproject.toml` 的 import-linter 契约（规则说明见[架构边界](architecture_review.md)），",
        "> 模块数为 Git 已跟踪的 `.py` 文件数（不含 `__init__.py`）。它不维护项目阶段、任务状态或实盘准入。",
        "> `架构图/` 里的交付图是早期版本的快照，路径变化见 `架构图/path_moves.json`。",
        "",
        "## 分层（从高到低；高层可导入低层，反之不行；`|` 两侧互相独立）",
        "",
        "```mermaid",
        "flowchart TB",
    ]
    for index, layer in enumerate(order):
        lines.append(f'  L{index}["{layer}"]')
        if index:
            lines.append(f"  L{index - 1} --> L{index}")
    lines += ["```", "", "## 包与模块数", "", "| 包 | 模块数 | 子包（模块数） |", "|---|---|---|"]
    for package, counts in tree.items():
        total = sum(counts.values())
        subs = ", ".join(f"`{name}` {count}" for name, count in counts.items() if name != ".")
        flat = counts.get(".", 0)
        detail = (f"平铺 {flat}" + ("；" if subs else "") if flat else "") + subs
        lines.append(f"| `{package}` | {total} | {detail or '—'} |")
    outside = sorted(name for name in files if "/" not in name)
    lines += ["", "顶层入口文件：" + "、".join(f"`{name}`" for name in outside) + "。", ""]
    return "\n".join(lines)


def diagram_problems() -> List[str]:
    """Spec source paths that neither exist nor have a recorded move to a file that does."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    moves: Dict[str, str] = json.loads(MOVES.read_text(encoding="utf-8")) if MOVES.is_file() else {}
    problems = []
    for old, new in sorted(moves.items()):
        if not (ROOT / new).exists():
            problems.append(f"{MOVES.name}: {old} -> {new}, but {new} does not exist")
    for component in spec["components"]:
        for source in component.get("sources", []):
            path = source["path"]
            if (ROOT / path).exists():
                continue
            target = moves.get(path)
            if target is None:
                problems.append(f"{component['id']}: {path} does not exist and has no entry in {MOVES.name}")
    return problems


def check() -> List[str]:
    problems = diagram_problems()
    expected = render()
    if not INVENTORY.is_file() or INVENTORY.read_text(encoding="utf-8").replace("\r\n", "\n") != expected:
        problems.append("docs/architecture_inventory.md is out of date; run `python scripts/architecture_inventory.py --write`")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        INVENTORY.write_text(render(), encoding="utf-8", newline="\n")
        print(f"wrote {INVENTORY.relative_to(ROOT).as_posix()}")
        return 0
    problems = check()
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if not problems:
        print("Architecture inventory is current; the delivered diagram's source paths resolve")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
