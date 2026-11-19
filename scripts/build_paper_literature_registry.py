"""Build or check the paper literature registry from the Excel navigation workbook (PR25-00).

  python scripts/build_paper_literature_registry.py build --workbook <xlsx> --output <new json>
  python scripts/build_paper_literature_registry.py check --workbook <xlsx> --registry <json>

``build`` refuses to overwrite an existing file. ``check`` rebuilds from the workbook and fails when the
committed registry differs, or when workbook presence of L01-L08 differs from the recorded expectation.
The workbook itself is not stored in the repository; its SHA-256 is recorded in the registry.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analysis.literature_registry import build_registry, presence_differs_from_expected, read_workbook_rows


def render(registry) -> str:
    return json.dumps(registry, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--workbook", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    check = sub.add_parser("check")
    check.add_argument("--workbook", required=True, type=Path)
    check.add_argument("--registry", required=True, type=Path)
    args = parser.parse_args(argv)

    rows, source = read_workbook_rows(args.workbook)
    registry = build_registry(rows, source)
    drift = presence_differs_from_expected(registry)
    if drift:
        print("workbook presence differs from the recorded expectation:", *drift, sep="\n  ", file=sys.stderr)
        return 1
    text = render(registry)
    if args.command == "build":
        if args.output.exists():
            print(f"refusing to overwrite {args.output}", file=sys.stderr)
            return 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.output} ({len(registry['papers'])} papers)")
        return 0
    committed = args.registry.read_text(encoding="utf-8").replace("\r\n", "\n")
    if committed != text:
        print("registry differs from the workbook-derived registry", file=sys.stderr)
        return 1
    print("registry matches the workbook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
